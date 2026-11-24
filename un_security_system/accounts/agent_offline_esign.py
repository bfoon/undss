# accounts/agent_offline_esign.py
"""
UN PASS — Agentic Flows: the built-in engine used when no AI provider is set.

Deterministic, explainable, and fast. It never pretends to be a model: every
result it produces is labelled "built-in" on the run page and in emails.

  summarize   extractive — the sentences that carry the most of the document's
              vocabulary, plus the dates, amounts, deadlines and obligations
  extract     "Label: value" lines first, then patterns for the kind of thing
              asked for (a date, an amount, an email, a reference…)
  classify    keyword scoring against each category's name and hints
  yes/no      key terms of the question present in the document; "more than
              50,000" style questions compare against amounts found
  policy      requirement sentences in the reference material ("must",
              "shall", "not exceed"…) whose key terms the document never mentions
  redact      always local — see mask_personal_data(); also used before text
              is sent to a provider when a flow asks for it
"""

from __future__ import annotations

import math
import re
from collections import Counter

STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each few for from further had has have having he her here hers
herself him himself his how i if in into is it its itself just me more most my myself no nor not now of off on once
only or other our ours ourselves out over own same she should so some such than that the their theirs them themselves
then there these they this those through to too under until up very was we were what when where which while who whom
why will with would you your yours yourself yourselves shall may must within upon per via herein thereof hereby
whereas therefore however etc also please dear one two three four five six seven eight nine ten
""".split())

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"(])")
_ABBREV = re.compile(r"\b(Mr|Mrs|Ms|Dr|Prof|No|Nos|Art|Ref|St|vs|etc|Inc|Ltd|Co|Jr|Sr|Gov|Hon|e\.g|i\.e)\.(?=\s)", re.I)
_KV = re.compile(r"^[A-Z][\w/&().'’-]*(?: [\w/&().'’-]+){0,4}:\s*\S.{0,160}$")
_BULLET = re.compile(r"^\s*(?:[-•*▪]|\d+[.)]|\([a-z0-9]\))\s+")
_WORD = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’-]{2,}")

MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
          "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
DATE_RE = re.compile(
    rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\.?,?\s+\d{{4}}|(?:{MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    r"|\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\b", re.I)
MONEY_RE = re.compile(
    r"(?:(?<![A-Za-z])(?:USD|US\$|EUR|GBP|GMD|CHF|XOF|NGN|KES|ZAR|\$|€|£|(?-i:D)(?=\s?\d))\s?\d[\d,]*(?:\.\d+)?"
    r"(?:\s?(?:million|bn|k)\b)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|GMD|CHF|XOF|dollars|euros|dalasi)\b)", re.I)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<![\w/-])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?){2,4}\d{2,4}(?![\w/-])")
OBLIGATION_RE = re.compile(r"\b(shall|must|is required to|are required to|undertakes? to|agrees? to|will be responsible|"
                           r"no later than|deadline|due (?:on|by)|within \d+ (?:days|weeks|months))\b", re.I)
REQUIREMENT_RE = re.compile(r"\b(must|shall|is required|are required|required to|may not|must not|shall not|prohibited|"
                            r"not (?:be )?(?:permitted|allowed)|not exceed|no more than|at least|maximum|minimum|mandatory|"
                            r"only (?:be )?(?:approved|authori[sz]ed))\b", re.I)


def _units(text: str):
    """Paragraphs of prose, and the document's own labelled lines ("Vendor: Acme")."""
    paras, kv, cur = [], [], []

    def flush():
        if cur:
            paras.append(" ".join(cur))
            cur.clear()

    for line in (text or "").splitlines():
        s = re.sub(r"[ \t]+", " ", line).strip()
        if not s or re.match(r"^\[Page \d+\]$", s):
            flush()
            continue
        if _KV.match(s) and len(s) <= 200:
            flush()
            kv.append(s)
            continue
        if _BULLET.match(s):
            flush()
            paras.append(_BULLET.sub("", s))
            continue
        if s.isupper() and len(s) < 90:          # a heading
            flush()
            continue
        cur.append(s)
    flush()
    return paras, kv


def labelled_lines(text: str, limit=8):
    return _uniq(_units(text)[1], limit)


def sentences(text: str):
    out = []
    for para in _units(text)[0]:
        safe = _ABBREV.sub(lambda m: m.group(1) + "§", para)
        for raw in _SENT_SPLIT.split(safe):
            sent = raw.replace("§", ".").strip(" -•*\t")
            if 25 <= len(sent) <= 600 and len(_WORD.findall(sent)) >= 4:
                out.append(sent)
    return out


def terms(text: str):
    return [w.lower().strip("'’-") for w in _WORD.findall(text or "") if w.lower() not in STOPWORDS]


def key_terms(text: str, n=12):
    return [w for w, _ in Counter(terms(text)).most_common(n)]


def _uniq(items, limit):
    seen, out = set(), []
    for item in items:
        k = re.sub(r"\s+", " ", item.strip()).lower()
        if k and k not in seen:
            seen.add(k)
            out.append(re.sub(r"\s+", " ", item.strip()))
        if len(out) >= limit:
            break
    return out


def facts(text: str):
    return {
        "dates": _uniq(DATE_RE.findall(text or ""), 8),
        "amounts": _uniq([m.group(0) for m in MONEY_RE.finditer(text or "")], 8),
        "emails": _uniq(EMAIL_RE.findall(text or ""), 8),
    }


def top_sentences(text: str, count: int):
    sents = sentences(text)
    if not sents:
        return []
    freq = Counter(terms(text))
    if not freq:
        return sents[:count]
    peak = max(freq.values())
    scored = []
    for i, s in enumerate(sents):
        words = terms(s)
        if not words:
            continue
        score = sum(freq[w] / peak for w in words) / math.sqrt(len(words))
        if i < 3:
            score *= 1.25                       # openings usually say what it is
        if DATE_RE.search(s) or MONEY_RE.search(s):
            score *= 1.15
        if OBLIGATION_RE.search(s):
            score *= 1.1
        scored.append((score, i, s))
    best = sorted(scored, reverse=True)[:count]
    return [s for _, _, s in sorted(best, key=lambda x: x[1])]


# ─────────────────────────────────────────────────────────────────────────────
# The routines the cards call
# ─────────────────────────────────────────────────────────────────────────────

def summarize(text: str, *, length="standard", style="brief", title="") -> str:
    n = {"brief": 3, "standard": 5, "detailed": 9}.get(length, 5)
    picks = top_sentences(text, n)
    if not picks:
        return "The document has no readable text to summarise (it may be a scanned image)."
    f = facts(text)
    obligations = [s for s in sentences(text) if OBLIGATION_RE.search(s)][: (2 if length == "brief" else 4)]

    if style == "paragraph":
        return " ".join(picks)
    if style == "bullets":
        return "\n".join(f"• {s}" for s in picks)

    lines = []
    if title and title.lower() not in ("pasted text", "document"):
        lines.append(f"What it is: {title}")
    lines.append("In short: " + picks[0])
    details = labelled_lines(text, 8 if length == "detailed" else 6)
    if details and style != "action":
        lines.append("")
        lines.append("Key details:")
        lines.extend(f"• {d}" for d in details)
    points = [s for s in picks[1:] if s not in obligations or style not in ("brief", "action")]
    if points:
        lines.append("")
        lines.append("Key points:")
        lines.extend(f"• {s}" for s in points)
    if f["amounts"] or f["dates"]:
        lines.append("")
        lines.append("Key facts:")
        if f["amounts"]:
            lines.append("• Amounts: " + ", ".join(f["amounts"][:5]))
        if f["dates"]:
            lines.append("• Dates: " + ", ".join(f["dates"][:5]))
    if obligations and style in ("brief", "action"):
        lines.append("")
        lines.append("Obligations and deadlines:")
        lines.extend(f"• {s}" for s in obligations)
    if style == "action":
        lines.append("")
        lines.append("Before signing: check the amounts and dates above against your own records.")
    return "\n".join(lines)


def _kind_of(label: str) -> str:
    l = label.lower()
    if any(k in l for k in ("email", "e-mail")):
        return "email"
    if any(k in l for k in ("date", "deadline", "expir", "start", "end", "valid", "until", "effective", "due")):
        return "date"
    if any(k in l for k in ("amount", "value", "cost", "price", "total", "budget", "fee", "salary", "sum")):
        return "money"
    if any(k in l for k in ("phone", "tel", "mobile")):
        return "phone"
    if any(k in l for k in ("reference", "ref", "number", "no.", "id", "contract no", "po")):
        return "ref"
    return "text"


def extract(text: str, fields):
    """{label: value} — blank when not found. Nothing is guessed."""
    text = text or ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    f = facts(text)
    out = {}
    for label in fields:
        label = str(label).strip()
        if not label:
            continue
        value = ""
        words = [w for w in re.findall(r"[a-z]{3,}", label.lower()) if w not in STOPWORDS] or [label.lower()]
        # 1. "Label: value" or "Label - value" on one line
        for line in lines:
            low = line.lower()
            m = re.match(r"^([^:–\-]{2,60})\s*[:–-]\s*(.{1,200})$", line)
            if m and all(w in m.group(1).lower() for w in words[:2]):
                value = m.group(2).strip()
                break
            if not value and all(w in low for w in words[:2]) and ":" in line:
                value = line.split(":", 1)[1].strip()
                if value:
                    break
        kind = _kind_of(label)
        # 2. By the kind of thing asked for, near the label's words if possible
        if not value:
            near = ""
            low = text.lower()
            pos = low.find(words[0]) if words else -1
            if pos >= 0:
                near = text[pos: pos + 400]
            pool = near or text
            if kind == "date":
                m = DATE_RE.search(pool)
                value = m.group(0) if m else ""
            elif kind == "money":
                m = MONEY_RE.search(pool)
                value = m.group(0).strip() if m else ""
            elif kind == "email":
                m = EMAIL_RE.search(pool)
                value = m.group(0) if m else (f["emails"][0] if f["emails"] else "")
            elif kind == "phone":
                m = PHONE_RE.search(pool)
                value = m.group(0).strip() if m else ""
            elif kind == "ref" and near:
                m = re.search(r"[A-Z]{1,6}[-/ ]?\d[\w/-]{2,}", near)
                value = m.group(0) if m else ""
            elif near:
                m = re.search(r"(?:is|are|:|–|-)\s+([^.;\n]{2,120})", near[len(words[0]):])
                value = m.group(1).strip() if m else ""
        out[label] = value[:300]
    return out


def classify(text: str, categories):
    """categories: [{id, label, hint}] -> (id or 'other', reason)."""
    doc = Counter(terms(text))
    best, best_score, why = None, 0.0, ""
    for c in categories:
        words = terms(c.get("label", "") + " " + (c.get("hint") or ""))
        words += [w.rstrip("s") for w in words]
        hits = {w: doc.get(w, 0) + doc.get(w + "s", 0) for w in set(words)}
        score = sum(math.log1p(v) for v in hits.values())
        if score > best_score:
            best, best_score = c, score
            found = [w for w, v in sorted(hits.items(), key=lambda x: -x[1]) if v][:4]
            why = "Mentions " + ", ".join(f"“{w}”" for w in found) if found else ""
    if not best or best_score < 0.7:
        return "other", "None of the categories' words appear often enough."
    return best["id"], why


_NUM = re.compile(r"(\d[\d,.]*)\s*(k|m|million|thousand)?\b", re.I)


def _to_number(s):
    m = _NUM.search(s or "")
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", "").rstrip("."))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    return n * (1000 if unit in ("k", "thousand") else 1_000_000 if unit in ("m", "million") else 1)


def yes_no(text: str, question: str):
    """-> (bool, reason). Plain and cautious."""
    q = question.lower()
    cmp = re.search(r"(more than|over|above|exceed[s]?|greater than|at least|less than|under|below|fewer than)\s+([^\s?]+(?:\s?(?:k|m|million|thousand))?)", q)
    if cmp:
        limit = _to_number(cmp.group(2))
        amounts = [a for a in (_to_number(x) for x in facts(text)["amounts"]) if a is not None]
        if limit is not None and amounts:
            top = max(amounts)
            above = cmp.group(1) in ("more than", "over", "above", "exceed", "exceeds", "greater than", "at least")
            result = (top >= limit if cmp.group(1) == "at least" else top > limit) if above else min(amounts) < limit
            return result, f"Largest amount found is {top:,.0f}; the question's limit is {limit:,.0f}."
    want = [w for w in terms(question) if w not in ("document", "documents", "letter", "file", "this", "does", "contain", "mention")]
    if not want:
        return False, "The question has no words to look for."
    doc = set(terms(text))
    present = [w for w in want if w in doc or w.rstrip("s") in doc]
    ratio = len(present) / len(want)
    negative = bool(re.search(r"\b(no|not|without|missing|lack)\b", q))
    answer = ratio >= 0.6
    if negative:
        answer = not answer
    return answer, (f"Found {len(present)} of {len(want)} key words ({', '.join(present[:5]) or 'none'}).")


def policy_check(text: str, reference: str):
    """-> (issues[list of str], checked[int])."""
    rules = [s for s in sentences(reference) if REQUIREMENT_RE.search(s)][:40]
    doc = set(terms(text))
    issues = []
    for rule in rules:
        key = [w for w in terms(rule) if len(w) > 4][:6]
        if not key:
            continue
        hit = sum(1 for w in key if w in doc or w.rstrip("s") in doc)
        if hit / len(key) < 0.34:
            issues.append(f"Not addressed in the document: {rule}")
    return issues[:12], len(rules)


# ─────────────────────────────────────────────────────────────────────────────
# Personal data
# ─────────────────────────────────────────────────────────────────────────────

ID_RE = re.compile(r"\b(?:[A-Z]{1,2}\d{6,9}|\d{3}-\d{2}-\d{4})\b")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
ACCOUNT_RE = re.compile(r"\b(?:account|acct|a/c)\s*(?:no\.?|number|#)?\s*[:#]?\s*\d(?:[\d -]{4,20}\d)", re.I)

KINDS = {
    "emails": (EMAIL_RE, "[EMAIL]"),
    "phones": (PHONE_RE, "[PHONE]"),
    "ids": (ID_RE, "[ID NUMBER]"),
    "banking": (None, "[BANK DETAILS]"),
}


def mask_personal_data(text: str, kinds=("emails", "phones", "ids", "banking")):
    """-> (masked_text, {kind: count}). Order matters: long patterns first."""
    counts = {}
    out = text or ""
    if "banking" in kinds:
        n = 0
        for rx in (IBAN_RE, ACCOUNT_RE, CARD_RE):
            out, k = rx.subn("[BANK DETAILS]", out)
            n += k
        counts["banking"] = n
    if "emails" in kinds:
        out, counts["emails"] = EMAIL_RE.subn("[EMAIL]", out)
    if "ids" in kinds:
        out, counts["ids"] = ID_RE.subn("[ID NUMBER]", out)
    if "phones" in kinds:
        def _phone(m):
            digits = re.sub(r"\D", "", m.group(0))
            return "[PHONE]" if 7 <= len(digits) <= 15 and not DATE_RE.fullmatch(m.group(0).strip()) else m.group(0)
        already = out.count("[PHONE]")
        out = PHONE_RE.sub(_phone, out)
        counts["phones"] = out.count("[PHONE]") - already
    return out, {k: v for k, v in counts.items() if v}


# ─────────────────────────────────────────────────────────────────────────────
# "Describe it and I'll build it" — without a model
# ─────────────────────────────────────────────────────────────────────────────

URL_RE = re.compile(r"https?://[^\s,;)\"'<>]+")
LANGS = ["French", "English", "Arabic", "Spanish", "Portuguese", "Russian", "Chinese", "Wolof", "Swahili", "German",
         "Italian", "Mandinka", "Fula", "Hausa", "Amharic"]


def compose(description: str):
    """
    Plain English -> {"trigger", "cards": [{type, label, context, config}]}
    in the order the sentence mentions them. Good enough to start from; the
    designer is where it gets finished.
    """
    d = description or ""
    low = d.lower()
    trigger = "manual"
    if re.search(r"(come|comes|coming|arriv\w*|sent|send)\s+(to me\s+)?(in\s+)?for (my )?signature|i (need|have) to sign|to me for signing|waiting for my signature", low):
        trigger = "envelope_received"
    if re.search(r"\bi send\b|when i send|i sent|send out for signature", low):
        trigger = "envelope_sent"
    if re.search(r"fully signed|completed envelope|once signed|after (it is|it's) signed|when .* is signed", low):
        trigger = "envelope_completed"

    clauses = [c.strip() for c in re.split(r"(?<=[.;!?])\s+|\bthen\b|,\s*(?:and\s+)?(?=(?:summari[sz]|translat|convert|email|send|check|extract|classif|sort|mask|redact|post|add|save|if\b|ask\b|get\b))", d, flags=re.I) if c and c.strip()]
    cards = []
    pending_via = {}

    def add(t, label, context="", **config):
        card = {"type": t, "label": label[:60], "context": context.strip()[:1500], "config": config}
        if pending_via.get("port"):
            card["via"] = pending_via.pop("port")
        cards.append(card)

    def ref(clause_low):
        """'convert the summary' -> the Summarize card, and so on."""
        for word, kind in (("summar", "summarize"), ("translat", "translate"), ("fact", "extract"), ("term", "extract"),
                           ("finding", "check"), ("issue", "check")):
            if word in clause_low and any(c["type"] == kind for c in cards):
                return "@" + kind
        return None

    for clause in clauses:
        c = clause.lower()
        urls = URL_RE.findall(clause)
        if urls:
            add("link", "Read the policy" if "polic" in c else "Read reference pages",
                "Use these pages as the rules to check against." if "polic" in c else "", urls=urls[:10])
        if re.search(r"\b(mask|redact|anonymi[sz]e|hide personal)", c):
            add("redact", "Mask personal data", kinds=["emails", "phones", "ids", "banking"])
        if re.search(r"\bsummari[sz]|\bgive me a summary|\bwrite a summary", c):
            style = "bullets" if "bullet" in c else "paragraph" if "paragraph" in c else \
                "action" if re.search(r"before (i )?sign|what i need to", c) else "brief"
            length = "brief" if re.search(r"short|brief|one.?line|quick", c) else "detailed" if "detail" in c else "standard"
            add("summarize", "Summarize", "", length=length, style=style)
        if re.search(r"\b(extract|pull out|capture|list the|get the)\b", c):
            m = re.search(r"(?:such as|like|including|:)\s+(.+)$", clause, re.I)
            fields = [f.strip(" .") for f in re.split(r",|\band\b", m.group(1)) if f.strip(" .")] if m else []
            add("extract", "Extract key facts", "", fields=fields[:12] or ["Parties", "Amount", "Start date", "End date"])
        if re.search(r"\b(classif|categori|sort (it|them)? ?into|label it)", c):
            m = re.search(r"(?:into|as|:)\s+(.+)$", clause, re.I)
            names = [f.strip(" .") for f in re.split(r",|\bor\b|\band\b|/", m.group(1)) if f.strip(" .")] if m else []
            add("classify", "Classify", "", categories=[{"id": f"c{i+1}", "label": n[:40], "hint": ""} for i, n in enumerate(names[:8])]
                or [{"id": "c1", "label": "Contract", "hint": ""}, {"id": "c2", "label": "Policy", "hint": ""}])
        if re.search(r"\b(polic|complian|comply|check (it )?against|against (our|the))", c) and not urls:
            m = re.search(r"\b(?:our|the|its)\s+([a-z][a-z \-]{2,40}?)\s+polic", c)
            name = (m.group(1).strip().title() + " policy") if m else "Policy"
            stop = bool(re.search(r"\b(stop|block|halt)\b", c))
            add("policy", name, "", title=name, outcomes={
                "warning": {"actions": [{"type": "inform", "to_runner": True}]},
                "breach": {"actions": [{"type": "warn", "to_runner": True, "note": "internal"}]
                           + ([{"type": "stop"}] if stop else [])},
            })
        elif re.search(r"\b(polic|complian|comply|check (it )?against)", c):
            add("check", "Policy check", "Flag anything in the document that breaks or ignores these rules.")
        lang = next((L for L in LANGS if re.search(rf"\b(in|into|to)\s+{L.lower()}\b", c)), None)
        if re.search(r"\btranslat", c):
            add("translate", f"Translate to {lang or 'French'}", language=lang or "French")
        m = re.search(r"\b(?:convert|turn|save|export)\b.*?\b(word|docx|pdf|excel|xlsx|csv|text|txt|markdown|html)\b", c)
        if m:
            target = {"word": "docx", "excel": "xlsx", "text": "txt", "markdown": "md"}.get(m.group(1), m.group(1))
            pretty = {"docx": "Word", "xlsx": "Excel", "txt": "text", "md": "Markdown", "pdf": "PDF", "csv": "CSV",
                      "html": "a web page"}[target]
            add("convert", f"Convert to {pretty}", target=target,
                source=ref(c) or ("previous" if cards and cards[-1]["type"] in ("summarize", "translate", "extract", "ask") else "document"))
        if "cover page" in c:
            add("cover", "Add summary cover page")
        prev = cards[-1]["type"] if cards else ""
        bad_words = r"\bif (there are |it finds |it has )?(any )?(issues|problems|findings|breach\w*)|\bif it (fails|breaks|breaches|violates|doesn'?t comply|is not compliant)"
        ok_words = r"\bif (it )?(passes|is fine|is ok|complies|is compliant)"
        after_check = prev in ("check", "policy") and re.search(bad_words, c)
        after_check_ok = prev in ("check", "policy") and re.search(ok_words, c)
        if after_check or after_check_ok:
            if not (prev == "policy" and re.search(r"\b(stop|block|halt)\b", c)):
                good, badp = ("pass", "issues") if prev == "check" else ("complies", "breach")
                pending_via["port"] = badp if after_check else good
        elif re.search(r"\bif\b|\bonly when\b|\bonly if\b", c) and not re.search(r"\bif (it )?(is )?(approved|rejected)", c):
            q = re.split(r"\b(?:ask|send|email|then|,)\b", re.sub(r"^.*?\b(?:if|only when|only if)\b\s*", "", clause, flags=re.I), 1)[0]
            q = q.strip(" ,.?")
            add("condition", ("If " + q)[:40] + ("…" if len(q) > 37 else "?"), (q[:1].upper() + q[1:] + "?") if q else "", mode="ai")
        if re.search(r"\b(?:approv|human|review(?:ed)? by|sign.?off|validat|check(?:ed)? by)", c) and "policy" not in c:
            add("approval", "Human check", "Check the AI's work before it goes any further.",
                emails=EMAIL_RE.findall(clause)[:5])
        if re.search(r"\b(teams|slack|webhook|channel)\b", c):
            add("webhook", "Post to Teams", format="teams")
        if re.search(r"\b(note|comment)\b.*\benvelope\b|\benvelope\b.*\b(note|comment)\b", c):
            add("envnote", "Note on the envelope", visibility="everyone")
        if re.search(r"\b(email|e-mail|notify|send (it|this|the \w+)? ?to|let .* know|tell)\b", c):
            emails = EMAIL_RE.findall(clause)
            add("email", "Email the result", "", emails=emails[:10],
                to_runner=bool(re.search(r"\b(me|myself)\b", c)) or not emails,
                to_sender="sender" in c, to_signers="signer" in c)
        if re.search(r"\b(save|keep) (it )?(to|in) (my )?(pdf tools|workbench|files)\b", c):
            add("save", "Save to my PDF tools")
    if not cards:
        add("summarize", "Summarize", "", length="standard", style="brief")
        add("email", "Email me the summary", to_runner=True)
    return {"trigger": trigger, "cards": cards}
