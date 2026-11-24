# accounts/agent_policy_esign.py
"""
UN PASS — Agentic Flows: the Policy card.

Upload a policy once — Word, PDF, text, a web page, or pasted text — and it
becomes a *rulebook* that lives inside the agent: a title, version, scope, a
short summary and numbered rules, each with a severity and a way to check it.
The file itself is never kept. Share or export the agent and the policy goes
with it, so colleagues never need the original document.

On each run the card checks the document against every rule and leaves by one
of four exits:

    Doesn't apply   the policy (or every rule) is out of scope for this document
    Complies        nothing flagged
    Warning         only "should" rules flagged (or something unclear)
    Breach          at least one "must" rule flagged

Each outcome can also *do* things before it leaves: inform people, warn the
sender, hold for a sign-off, start another agent, start a Flow Studio
workflow, or stop the run. A rule can add its own action ("contracts over
50,000 → start the Contracts Committee workflow").

Rule checks
-----------
threshold   the document's largest amount (or a number you name) compared with
            a limit — exact and explainable, no AI needed
requires    the document must mention these words (any of a/b/c)
forbids     the document must not mention these words
judgement   anything else: the AI decides, quoting the evidence. Without a
            provider the built-in engine checks whether the rule's key terms
            are addressed, and says it is unsure when it can't tell.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from django.utils import timezone

from . import agent_ai_esign as A
from . import agent_offline_esign as O

logger = logging.getLogger(__name__)

SEVERITIES = {"must": "Must", "should": "Should", "info": "For information"}
KINDS = {
    "judgement": "AI judgement",
    "threshold": "Amount limit",
    "requires": "Must mention",
    "forbids": "Must not mention",
}
OPS = {"gt": "more than", "ge": "at least", "lt": "less than", "le": "at most"}
OUTCOMES = {"na": "Doesn't apply", "complies": "Complies", "warning": "Warning", "breach": "Breach"}
ACTIONS = {
    "inform": "Inform people",
    "warn": "Warn the people involved",
    "signoff": "Hold for a sign-off",
    "agent": "Run another agent",
    "workflow": "Start a Flow Studio workflow",
    "stop": "Stop the run",
}
RULE_THEN = {"": "Nothing extra", "signoff": "Hold for a sign-off", "agent": "Run another agent",
             "workflow": "Start a workflow", "stop": "Stop the run"}

MAX_RULES = 40
MAX_TEXT = 60000
_ID = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _s(v, n):
    return str(v if v is not None else "")[:n]


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _terms(v, limit=12):
    if isinstance(v, str):
        v = re.split(r"[,;\n]", v)
    out = []
    for t in list(v or [])[:limit]:
        t = _s(t, 80).strip()
        if t:
            out.append(t)
    return out


def _ints(v, limit=20):
    out = []
    for x in list(v or [])[:limit]:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return list(dict.fromkeys(out))


def _emails(v, limit=20):
    out = []
    for x in list(v or [])[:limit]:
        e = str(x or "").strip().lower()
        if _EMAIL.match(e):
            out.append(e)
    return list(dict.fromkeys(out))


# ─────────────────────────────────────────────────────────────────────────────
# Cleaning (called by the engine's clean_graph)
# ─────────────────────────────────────────────────────────────────────────────

def clean_rule(r, i):
    r = r if isinstance(r, dict) else {}
    rid = str(r.get("id") or "")
    out = {
        "id": rid if _ID.match(rid) else f"r{i + 1}",
        "title": _s(r.get("title"), 160).strip() or f"Rule {i + 1}",
        "text": _s(r.get("text"), 1500).strip(),
        "ref": _s(r.get("ref"), 40).strip(),
        "severity": r.get("severity") if r.get("severity") in SEVERITIES else "must",
        "kind": r.get("kind") if r.get("kind") in KINDS else "judgement",
        "enabled": r.get("enabled", True) is not False,
        "applies_if": _terms(r.get("applies_if")),
        "terms": _terms(r.get("terms")),
        "op": r.get("op") if r.get("op") in OPS else "gt",
        "value": _num(r.get("value")),
        "unit": _s(r.get("unit"), 12).strip(),
    }
    then = r.get("then") if isinstance(r.get("then"), dict) else {}
    t = then.get("type") if then.get("type") in RULE_THEN else ""
    out["then"] = {"type": t, "id": int(then["id"]) if str(then.get("id") or "").isdigit() else None} if t else {"type": ""}
    return out


def clean_action(a):
    a = a if isinstance(a, dict) else {}
    t = a.get("type")
    if t not in ACTIONS:
        return None
    out = {"type": t, "message": _s(a.get("message"), 1500)}
    if t in ("inform", "warn", "signoff"):
        out["users"] = _ints(a.get("users"))
        out["emails"] = _emails(a.get("emails"))
        out["to_runner"] = bool(a.get("to_runner"))
    if t in ("inform", "warn"):
        out["to_sender"] = bool(a.get("to_sender"))
        out["to_signers"] = bool(a.get("to_signers"))
    if t == "warn":
        out["note"] = a.get("note") if a.get("note") in ("", "internal", "everyone") else "internal"
    if t in ("agent", "workflow"):
        out["id"] = int(a["id"]) if str(a.get("id") or "").isdigit() else None
    if t == "workflow":
        out["users"] = _ints(a.get("users"))
        out["emails"] = _emails(a.get("emails"))
    return out


def clean_config(c):
    c = c if isinstance(c, dict) else {}
    src = c.get("origin") if isinstance(c.get("origin"), dict) else {}
    rules, seen = [], set()
    for i, r in enumerate(list(c.get("rules") or [])[:MAX_RULES]):
        rule = clean_rule(r, i)
        while rule["id"] in seen:
            rule["id"] += "x"
        seen.add(rule["id"])
        rules.append(rule)
    outcomes = {}
    raw = c.get("outcomes") if isinstance(c.get("outcomes"), dict) else {}
    for key in OUTCOMES:
        acts = [clean_action(a) for a in list((raw.get(key) or {}).get("actions") or [])[:6]]
        outcomes[key] = {"actions": [a for a in acts if a]}
    return {
        "title": _s(c.get("title"), 150).strip(),
        "version": _s(c.get("version"), 30).strip(),
        "effective": _s(c.get("effective"), 30).strip(),
        "owner": _s(c.get("owner"), 120).strip(),
        "summary": _s(c.get("summary"), 2000),
        "applies_to": _s(c.get("applies_to"), 400),
        "applies_keywords": _terms(c.get("applies_keywords"), 20),
        "rules": rules,
        "text": _s(c.get("text"), MAX_TEXT),
        "keep_text": c.get("keep_text", True) is not False,
        "strict": bool(c.get("strict")),
        "origin": {
            "name": _s(src.get("name"), 200), "kind": _s(src.get("kind"), 12), "url": _s(src.get("url"), 500),
            "chars": int(_num(src.get("chars")) or 0), "sha256": _s(src.get("sha256"), 64),
            "built_at": _s(src.get("built_at"), 40), "built_by": _s(src.get("built_by"), 120),
            "how": _s(src.get("how"), 12),
        },
        "outcomes": outcomes,
    }


def validate(node, flows_ok=None, workflows_ok=None):
    """-> (errors, warnings) as plain sentences."""
    c = node.get("config") or {}
    label = node.get("label") or "Policy"
    errors, warnings = [], []
    live = [r for r in c.get("rules") or [] if r.get("enabled")]
    if not c.get("rules"):
        errors.append(f"“{label}”: upload the policy (Word, PDF or text) or add rules — open the Policy studio.")
    elif not live:
        errors.append(f"“{label}”: every rule is switched off.")
    for r in live:
        if r["kind"] == "threshold" and r.get("value") is None:
            errors.append(f"“{label}”: rule “{r['title']}” needs the amount limit.")
        if r["kind"] in ("requires", "forbids") and not r.get("terms"):
            errors.append(f"“{label}”: rule “{r['title']}” needs the words to look for.")
    for key, o in (c.get("outcomes") or {}).items():
        for a in o.get("actions") or []:
            where = f"“{label}” → {OUTCOMES[key]}"
            if a["type"] in ("inform", "signoff") and not (a.get("users") or a.get("emails") or a.get("to_runner")
                                                          or a.get("to_sender") or a.get("to_signers")):
                errors.append(f"{where}: choose who to {'inform' if a['type'] == 'inform' else 'ask for the sign-off'}.")
            if a["type"] in ("agent", "workflow") and not a.get("id"):
                errors.append(f"{where}: choose which {'agent' if a['type'] == 'agent' else 'workflow'} to start.")
            if a["type"] == "agent" and a.get("id") and flows_ok is not None and a["id"] not in flows_ok:
                warnings.append(f"{where}: the agent it starts is no longer available to you.")
            if a["type"] == "workflow" and a.get("id") and workflows_ok is not None and a["id"] not in workflows_ok:
                warnings.append(f"{where}: the workflow it starts is no longer available to you.")
    for r in live:
        t = (r.get("then") or {})
        if t.get("type") in ("agent", "workflow") and not t.get("id"):
            errors.append(f"“{label}”: rule “{r['title']}” — choose which {'agent' if t['type'] == 'agent' else 'workflow'} to start.")
    return errors, warnings


# ─────────────────────────────────────────────────────────────────────────────
# Building a rulebook from a document
# ─────────────────────────────────────────────────────────────────────────────

_REF = re.compile(r"^\s*((?:section|article|clause|para(?:graph)?)\s*)?(\d+(?:\.\d+){0,3})[.)]?\s+", re.I)
_SHOULD = re.compile(r"\b(should|recommended|encouraged|preferably|where possible|ideally)\b", re.I)
_FORBID = re.compile(r"\b(must not|shall not|may not|cannot|is prohibited|are prohibited|prohibited|not (?:be )?(?:permitted|allowed)|never)\b", re.I)
_REQUIRE = re.compile(r"\b(must|shall|is required|are required|required to|mandatory|need to|needs to)\b", re.I)
_LIMIT = re.compile(r"\b(not exceed|exceeding|more than|above|over|in excess of|greater than|at least|minimum of|less than|below|under|maximum of|up to)\b"
                    r"[^.\d]{0,30}?((?:USD|US\$|EUR|GBP|GMD|\$|€|£)?\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|million|thousand)\b)?)", re.I)
_PERMISSION = re.compile(r"\bmay\b(?!\s+not)|\bcan\b(?!not)|\bis allowed\b|\bare allowed\b", re.I)


def _title(sentence):
    s = _REF.sub("", sentence).strip()
    s = re.sub(r"\s+", " ", s)
    words = s.split()
    t = " ".join(words[:12])
    return (t + ("…" if len(words) > 12 else "")).rstrip(",;:")


def unwrap(text):
    """PDFs break sentences across lines; join them back, keeping numbered clauses, bullets and headings apart."""
    out = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or re.match(r"^\[Page \d+\]$", s):
            out.append("")
            continue
        new_item = bool(_REF.match(s) or re.match(r"^[-•*▪]\s", s) or (s.isupper() and len(s) < 80))
        if out and out[-1] and not new_item and not re.search(r"[.:;!?]$", out[-1]) and s[:1].islower() | (len(out[-1]) > 50):
            out[-1] += " " + s
        else:
            out.append(s)
    return "\n".join(out)


def _offline_rules(text):
    rules, seen = [], set()
    text = unwrap(text)
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        ref_m = _REF.match(line)
        ref = ref_m.group(2) if ref_m else ""
        for sent in (O.sentences(line) or ([line] if 25 <= len(line) <= 600 else [])):
            if not (_REQUIRE.search(sent) or _FORBID.search(sent) or _SHOULD.search(sent) or _LIMIT.search(sent)):
                continue
            if _PERMISSION.search(sent) and not (_REQUIRE.search(sent) or _FORBID.search(sent) or _SHOULD.search(sent)):
                continue                                   # "may be approved by…" grants, it doesn't require
            key = re.sub(r"\W+", " ", sent.lower()).strip()[:120]
            if key in seen:
                continue
            seen.add(key)
            severity = "should" if _SHOULD.search(sent) and not _REQUIRE.search(sent) else "must"
            rule = {"title": _title(sent), "text": sent, "ref": ref, "severity": severity, "kind": "judgement"}
            m = _LIMIT.search(sent)
            if m and re.match(r"\s*(USD|US\$|EUR|GBP|GMD|\$|€|£)", m.group(2) or "", re.I):   # a money limit, not "10 percent"
                val = O._to_number(m.group(2))
                if val:
                    word = m.group(1).lower()
                    op = "lt" if word in ("at least", "minimum of") else "gt"
                    rule.update(kind="threshold", op=op, value=val,
                                unit=(re.match(r"\s*(USD|US\$|EUR|GBP|GMD|\$|€|£)", m.group(2) or "", re.I) or [None, ""])[1] or "")
            elif _FORBID.search(sent):
                # "must not X" -> X comes after; "X is prohibited / not allowed" -> X comes before.
                fm = _FORBID.search(sent)
                after = fm.group(1).lower() in ("must not", "shall not", "may not", "cannot", "never")
                part = sent[fm.end():] if after else sent[:fm.start()]
                words = [w for w in O.terms(part) if len(w) > 3][:2]
                phrase = " ".join(words)
                if len(words) == 2 and phrase in re.sub(r"\s+", " ", sent.lower()):
                    rule.update(kind="forbids", terms=[phrase])   # only a precise phrase; otherwise the AI/judgement decides
            rules.append(rule)
            if len(rules) >= 25:
                break
        if len(rules) >= 25:
            break
    return rules


def _offline_meta(text, name):
    text = unwrap(text)
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    title = next((l for l in lines[:6] if 4 <= len(l) <= 120 and not l.endswith(".")), "") or (name or "Policy")
    version = ""
    m = re.search(r"\b(?:version|ver\.?|v)\s*(\d+(?:\.\d+)*)", text or "", re.I)
    if m:
        version = "v" + m.group(1)
    eff = ""
    m = re.search(r"effective(?: date)?[:\s]+([^\n.]{4,30})", text or "", re.I)
    if m:
        eff = m.group(1).strip()
    scope = ""
    m = re.search(r"(?:this policy )?applies to ([^.\n]{5,200})", text or "", re.I)
    if m:
        scope = m.group(1).strip()
    summary = " ".join(s for s in O.top_sentences(text, 3) if len(s) < 400)
    return {"title": title[:150], "version": version, "effective": eff, "applies_to": scope,
            "applies_keywords": [], "summary": summary[:1500]}


def build(text, *, name="", hint="", user=None, kind="upload", url=""):
    """Policy text -> a cleaned rulebook config (without outcomes). Never raises on AI trouble."""
    text = (text or "").strip()
    if len(text) < 40:
        raise ValueError("The policy has too little readable text to build rules from (is it a scanned image?).")
    how, meta, rules = "built-in", None, None
    if not A.is_offline():
        try:
            res = A.complete(
                "You turn institutional policies into clear, checkable rules for an automated document checker.",
                "Read the policy below and produce its rulebook.\n"
                + (f"Guidance from the person uploading it: {hint}\n" if hint else "")
                + "Rules: only real obligations, prohibitions, limits and recommendations that a document (a contract, "
                  "request, memo, purchase order…) could comply with or break. Up to 25, most important first. Quote or "
                  "closely paraphrase the policy in `text`, and give the section number in `ref` when there is one.\n"
                  "severity: must (mandatory) | should (recommended) | info.\n"
                  "kind: threshold (an amount limit — give op gt|ge|lt|le, value as a number, unit like USD; the rule is "
                  "FLAGGED when the document's amount meets the condition) | requires (document must mention one of `terms`) "
                  "| forbids (document must not mention any of `terms`) | judgement (anything else).\n"
                  "applies_if: optional words that must appear in a document for the rule to apply.\n"
                  'Reply as {"title": "", "version": "", "effective": "", "applies_to": "<who/what the policy covers>", '
                  '"applies_keywords": ["..."], "summary": "<3 sentences>", "rules": [{"title": "<short>", "text": "", '
                  '"ref": "", "severity": "", "kind": "", "op": "", "value": null, "unit": "", "terms": [], "applies_if": []}]}\n\n'
                  f"<policy name=\"{name}\">\n{text[:50000]}\n</policy>",
                json_expected=True, max_tokens=6000)
            if isinstance(res.data.get("rules"), list) and res.data["rules"]:
                meta, rules, how = res.data, res.data["rules"], "ai"
        except A.AIError as exc:
            logger.info("Policy build fell back to the built-in reader: %s", exc)
    if rules is None:
        meta, rules = _offline_meta(text, name), _offline_rules(text)
        must = sum(1 for r in rules if r["severity"] == "must")
        meta["summary"] = (f"{meta['title']}{(' ' + meta['version']) if meta['version'] else ''}: {len(rules)} rule(s) found "
                           f"({must} must, {len(rules) - must} should)"
                           + (f", applying to {meta['applies_to']}" if meta["applies_to"] else "") + ". Read by the built-in "
                           "reader — check each rule's severity and how it is checked.")
    if not rules:
        raise ValueError("No rules were found in that document. Add them by hand in the Policy studio, or upload the policy itself.")
    cfg = clean_config({
        **{k: meta.get(k) for k in ("title", "version", "effective", "applies_to", "applies_keywords", "summary")},
        "rules": [{**r, "id": f"r{i + 1}"} for i, r in enumerate(rules) if isinstance(r, dict)],
        "text": text,
        "origin": {"name": name, "kind": kind, "url": url, "chars": len(text),
                   "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                   "built_at": timezone.now().isoformat(timespec="minutes"),
                   "built_by": ((user.get_full_name() or user.username) if user else ""), "how": how},
    })
    if not cfg["title"]:
        cfg["title"] = (name.rsplit(".", 1)[0] if name else "Policy")[:150]
    return cfg


def _norm(t):
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def merge(old_rules, new_rules):
    """Carry the user's choices (on/off, severity, extra action) to rebuilt rules with the same title."""
    by_title = {_norm(r.get("title")): r for r in old_rules or []}
    kept = 0
    for r in new_rules:
        o = by_title.get(_norm(r["title"]))
        if o:
            kept += 1
            r["enabled"] = o.get("enabled", True)
            r["severity"] = o.get("severity", r["severity"])
            if (o.get("then") or {}).get("type"):
                r["then"] = o["then"]
    new_titles = {_norm(r["title"]) for r in new_rules}
    return {"added": len(new_rules) - kept, "removed": len([t for t in by_title if t not in new_titles]), "kept": kept}


# ─────────────────────────────────────────────────────────────────────────────
# Checking a document
# ─────────────────────────────────────────────────────────────────────────────

def _present(term, low_text):
    alts = [a.strip().lower() for a in re.split(r"/|\bor\b", term) if a.strip()]
    for a in alts:
        if a in low_text or (a.endswith("s") and a[:-1] in low_text):
            return a
    return None


def _evidence(text, word):
    for s in O.sentences(text) + [l.strip() for l in text.splitlines() if l.strip()]:
        if word.lower() in s.lower():
            return s[:300]
    return ""


def _check_rule(rule, text, low, amounts):
    """Deterministic checks. -> (status, note, evidence) or None for judgement rules."""
    if rule["applies_if"] and not any(_present(t, low) for t in rule["applies_if"]):
        return "na", "None of its trigger words appear.", ""
    k = rule["kind"]
    if k == "threshold":
        if not amounts:
            return "unclear", "No amount was found in the document.", ""
        top = max(amounts)
        v = rule["value"]
        hit = {"gt": top > v, "ge": top >= v, "lt": top < v, "le": top <= v}[rule["op"]]
        unit = f" {rule['unit']}" if rule.get("unit") else ""
        return ("flag" if hit else "ok",
                f"Largest amount {top:,.0f}{unit} is {'' if hit else 'not '}{OPS[rule['op']]} {v:,.0f}{unit}.", "")
    if k == "requires":
        found = [t for t in rule["terms"] if _present(t, low)]
        if found:
            return "ok", f"Mentions {', '.join('“' + t + '”' for t in found)}.", _evidence(text, _present(found[0], low))
        return "flag", "Doesn't mention " + " or ".join(f"“{t}”" for t in rule["terms"]) + ".", ""
    if k == "forbids":
        found = [(t, _present(t, low)) for t in rule["terms"] if _present(t, low)]
        if found:
            return "flag", f"Mentions {', '.join('“' + t + '”' for t, _ in found)}.", _evidence(text, found[0][1])
        return "ok", "None of the forbidden words appear.", ""
    return None


def _offline_judgement(rule, text, low):
    words = [w for w in O.terms(rule["title"] + " " + rule["text"]) if len(w) > 4]
    key = list(dict.fromkeys(words))[:6]
    if not key:
        return "unclear", "Built-in check: the rule has no clear words to look for.", ""
    hits = [w for w in key if w in low or w.rstrip("s") in low]
    ratio = len(hits) / len(key)
    prohibitive = bool(_FORBID.search(rule["text"] or rule["title"]))
    if prohibitive:
        if ratio >= 0.6:
            return "flag", f"Built-in check: the document talks about what this rule forbids ({', '.join(hits[:4])}).", _evidence(text, hits[0])
        return "ok", "Built-in check: nothing the rule forbids was found.", ""
    if ratio >= 0.5:
        return "ok", f"Built-in check: the document addresses it ({', '.join(hits[:4])}).", _evidence(text, hits[0])
    if ratio == 0:
        return "flag", "Built-in check: the document doesn't address this at all.", ""
    return "unclear", f"Built-in check: only partly addressed ({', '.join(hits) or 'none'}). A person should look.", ""


def scope_applies(cfg, text, low):
    kws = cfg.get("applies_keywords") or []
    if kws:
        hit = [k for k in kws if _present(k, low)]
        return bool(hit), (f"Mentions {', '.join(hit[:3])}." if hit else "None of the policy's scope words appear.")
    return True, ""


def evaluate(rt, node, text):
    """-> dict(outcome, results[], summary, model, tokens_in, tokens_out)."""
    cfg = node["config"]
    low = (text or "").lower()
    amounts = [a for a in (O._to_number(x) for x in O.facts(text)["amounts"]) if a is not None]
    rules = [r for r in cfg["rules"] if r["enabled"]]
    applies, scope_note = scope_applies(cfg, text, low)
    results, todo = [], []
    model, tin, tout = "built-in", 0, 0
    if applies:
        for r in rules:
            got = _check_rule(r, text, low, amounts)
            if got:
                results.append({"id": r["id"], "status": got[0], "note": got[1], "evidence": got[2], "by": "rule"})
            else:
                todo.append(r)
    ai_scope = applies and not cfg.get("applies_keywords") and (cfg.get("applies_to") or "").strip()
    if applies and (todo or ai_scope):
        if A.is_offline():
            if ai_scope:
                want = [w for w in O.terms(cfg["applies_to"]) if len(w) > 4][:6]
                if want and not any(w in low for w in want):
                    applies, scope_note = False, f"Built-in check: none of “{cfg['applies_to']}” appears in the document."
            for r in todo:
                st, note, ev = _offline_judgement(r, text, low)
                results.append({"id": r["id"], "status": st, "note": note, "evidence": ev, "by": "built-in"})
        else:
            menu = "\n".join(f"- {r['id']} [{r['severity']}] {r['title']}: {r['text'] or r['title']}" for r in todo)
            res = rt.ai(
                {**node, "config": {**cfg, "context": cfg.get("context", "")}},
                f"You are checking a document against the policy “{cfg['title'] or 'Policy'}”.\n"
                + (f"The policy applies to: {cfg['applies_to']}. First decide whether it applies to this document.\n" if ai_scope else "")
                + "For each rule below decide: ok (complies), flag (breaks it or misses something it requires), na (the rule "
                  "doesn't apply to this document), or unclear. Quote the evidence from the document.\n"
                + f"Rules:\n{menu or '(none)'}\n"
                + 'Reply as {"applies": true|false, "scope_reason": "", "rules": [{"id": "", "status": "ok|flag|na|unclear", '
                  '"evidence": "<short quote or empty>", "note": "<one sentence>"}]}',
                text=text, json_expected=True, max_tokens=2500, with_knowledge=False)
            model, tin, tout = res.model, res.tokens_in, res.tokens_out
            if ai_scope and res.data.get("applies") is False:
                applies, scope_note = False, str(res.data.get("scope_reason") or "The AI judged the policy out of scope.")[:300]
            got = {str(x.get("id")): x for x in res.data.get("rules") or [] if isinstance(x, dict)}
            for r in todo:
                x = got.get(r["id"]) or {}
                st = x.get("status") if x.get("status") in ("ok", "flag", "na", "unclear") else "unclear"
                results.append({"id": r["id"], "status": st, "note": str(x.get("note") or "")[:300],
                                "evidence": str(x.get("evidence") or "")[:300], "by": "ai"})
    by_id = {r["id"]: r for r in rules}
    for x in results:
        r = by_id[x["id"]]
        x.update(title=r["title"], severity=r["severity"], ref=r["ref"], kind=r["kind"], text=r["text"])
    results.sort(key=lambda x: (["flag", "unclear", "ok", "na"].index(x["status"]), ["must", "should", "info"].index(x["severity"])))

    if not applies or not results or all(x["status"] == "na" for x in results):
        outcome = "na"
    elif any(x["status"] == "flag" and x["severity"] == "must" for x in results) or \
            (cfg.get("strict") and any(x["status"] == "unclear" and x["severity"] == "must" for x in results)):
        outcome = "breach"
    elif any(x["status"] == "flag" and x["severity"] == "should" for x in results) or \
            any(x["status"] == "unclear" and x["severity"] != "info" for x in results):
        outcome = "warning"
    else:
        outcome = "complies"
    counts = {s: sum(1 for x in results if x["status"] == s) for s in ("flag", "unclear", "ok", "na")}
    return {"outcome": outcome, "applies": applies, "scope_note": scope_note, "results": results, "counts": counts,
            "model": model, "tokens_in": tin, "tokens_out": tout}


def findings_text(cfg, ev):
    title = cfg.get("title") or "the policy"
    head = {
        "na": f"“{title}” doesn't apply to this document.",
        "complies": f"The document complies with “{title}”.",
        "warning": f"The document needs attention under “{title}”.",
        "breach": f"The document breaks “{title}”.",
    }[ev["outcome"]]
    lines = [head]
    if ev.get("scope_note") and ev["outcome"] == "na":
        lines.append(ev["scope_note"])
    mark = {"flag": "✗", "unclear": "?", "ok": "✓", "na": "–"}
    for x in ev["results"]:
        if x["status"] in ("flag", "unclear") or ev["outcome"] in ("complies",):
            ref = f" ({x['ref']})" if x.get("ref") else ""
            lines.append(f"{mark[x['status']]} [{x['severity'].upper()}] {x['title']}{ref} — {x['note']}"
                         + (f" Evidence: “{x['evidence']}”" if x.get("evidence") else ""))
    if ev["outcome"] != "na":
        c = ev["counts"]
        lines.append(f"Checked {len(ev['results'])} rule(s): {c['flag']} flagged, {c['unclear']} unclear, {c['ok']} fine, {c['na']} not applicable.")
    return "\n".join(lines)
