# accounts/agent_engine_esign.py
"""
UN PASS — Agentic Flows: the cards, the checks, and the runner.

How a run moves
---------------
The graph is a set of cards joined by connections. A run walks the cards in
dependency order (a card never runs before the cards that lead into it), so a
card where two branches meet sees both branches' results. Branching cards —
Classify, Policy check, If, Human check — pick an exit; only the cards down
that exit run. Everything else is skipped.

Each card gets: the document's text, everything the Knowledge links read, the
results of earlier cards, the flow's standing instructions (the Trigger card's
context) and its own context box. Its result is stored on an AgentStep and is
available to later cards by name: {{Summarize}} in an email subject, a cover
page, a Teams post.

A Human check pauses the run and emails a link. Approving (optionally after
editing the AI's text) carries on down "Approved"; rejecting goes down
"Rejected".

Runs execute off the request: in a background thread by default (the same
approach the app already uses for email), or inline with AGENT_RUN_MODE =
"sync" for tests and debugging. Nothing here may break the envelope that
triggered it: every failure is written to the run, never raised.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from . import agent_ai_esign as A
from . import agent_docs_esign as D
from . import agent_offline_esign as O
from . import agent_policy_esign as PO

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# The catalogue
# ─────────────────────────────────────────────────────────────────────────────

GROUPS = [
    ("start", "Start & knowledge"),
    ("ai", "AI steps"),
    ("docs", "Documents"),
    ("decide", "Decide"),
    ("deliver", "Deliver"),
    ("other", "Finish & notes"),
]

CARD_TYPES = {
    "start": {"label": "Trigger", "icon": "bi-lightning-charge-fill", "color": "#0F766E", "ports": ["next"], "group": "start",
              "hint": "What starts the agent, and standing instructions for every AI card."},
    "policy": {"label": "Policy", "icon": "bi-journal-check", "color": "#9A3412",
               "ports": ["na", "complies", "warning", "breach"], "group": "start", "ai": True,
               "hint": "Upload a policy (Word, PDF, text). It becomes rules inside this agent; choose what happens on a breach."},
    "link": {"label": "Knowledge link", "icon": "bi-link-45deg", "color": "#0369A1", "ports": ["next"], "group": "start",
             "hint": "Read web pages or an uploaded file — a guideline, ToR — so later cards can use them."},
    "summarize": {"label": "Summarize", "icon": "bi-card-text", "color": "#7C3AED", "ports": ["next"], "group": "ai", "ai": True,
                  "hint": "A brief, bullets or an executive summary of each document."},
    "extract": {"label": "Extract facts", "icon": "bi-table", "color": "#6D28D9", "ports": ["next"], "group": "ai", "ai": True,
                "hint": "Pull out the values you name: parties, amounts, dates, references."},
    "classify": {"label": "Classify", "icon": "bi-tags", "color": "#9333EA", "ports": [], "group": "ai", "ai": True,
                 "hint": "Sort the document into your categories. Each category is its own path."},
    "check": {"label": "Policy check", "icon": "bi-shield-check", "color": "#B45309", "ports": ["pass", "issues"], "group": "ai",
              "ai": True, "hint": "Compare the document with the policy your Knowledge links read."},
    "translate": {"label": "Translate", "icon": "bi-translate", "color": "#0D9488", "ports": ["next"], "group": "ai", "ai": True,
                  "hint": "Into French, Arabic, Spanish, Portuguese — any language."},
    "redact": {"label": "Mask personal data", "icon": "bi-incognito", "color": "#475569", "ports": ["next"], "group": "ai",
               "hint": "Hide emails, phone numbers, ID and bank details (and names, with AI)."},
    "ask": {"label": "Custom AI step", "icon": "bi-stars", "color": "#C026D3", "ports": ["next"], "group": "ai", "ai": True,
            "hint": "Anything else: write the instruction in the context box."},
    "convert": {"label": "Convert", "icon": "bi-file-earmark-arrow-down", "color": "#2563EB", "ports": ["next"], "group": "docs",
                "hint": "To PDF, Word, Excel, CSV, text, Markdown or a web page."},
    "cover": {"label": "Summary cover page", "icon": "bi-file-earmark-richtext", "color": "#1D4ED8", "ports": ["next"],
              "group": "docs", "hint": "Put a one-page summary in front of the PDF."},
    "save": {"label": "Save to PDF tools", "icon": "bi-folder-plus", "color": "#334155", "ports": ["next"], "group": "docs",
             "hint": "Keep the PDFs this run made in your PDF tools workbench."},
    "condition": {"label": "If", "icon": "bi-signpost-split", "color": "#DB2777", "ports": ["yes", "no"], "group": "decide",
                  "hint": "Ask the AI a yes/no question, or test an earlier result."},
    "approval": {"label": "Human check", "icon": "bi-person-check", "color": "#D97706", "ports": ["approved", "rejected"],
                 "group": "decide", "hint": "A person approves (or edits) the AI's work before it goes on."},
    "email": {"label": "Email", "icon": "bi-envelope-paper", "color": "#0891B2", "ports": ["next"], "group": "deliver",
              "hint": "Send results to colleagues from the directory, or any email address."},
    "envnote": {"label": "Note on envelope", "icon": "bi-chat-square-quote", "color": "#0E7490", "ports": ["next"],
                "group": "deliver", "hint": "Post the result as a note on the envelope that started the run."},
    "webhook": {"label": "Teams / webhook", "icon": "bi-broadcast", "color": "#4F46E5", "ports": ["next"], "group": "deliver",
                "hint": "Post to a Microsoft Teams, Slack or any webhook."},
    "end": {"label": "Finish", "icon": "bi-flag-fill", "color": "#16A34A", "ports": [], "group": "other",
            "hint": "The end of a path. Optional — a path also ends where its connections stop."},
    "note": {"label": "Sticky note", "icon": "bi-sticky", "color": "#CA8A04", "ports": [], "group": "other",
             "hint": "A note for whoever reads the flow. It doesn't run."},
}

PORT_LABELS = {
    "next": "Next", "pass": "Passes", "issues": "Issues found", "yes": "Yes", "no": "No",
    "approved": "Approved", "rejected": "Rejected", "other": "Other",
    "na": "Doesn't apply", "complies": "Complies", "warning": "Warning", "breach": "Breach",
}

TRIGGERS = {
    "manual": "When I run it (upload a document or paste text)",
    "envelope_received": "A document comes to me for signature",
    "envelope_sent": "I send a document for signature",
    "envelope_completed": "A document I sent is fully signed",
}

LANGUAGES = ["English", "French", "Arabic", "Spanish", "Portuguese", "Russian", "Chinese", "Swahili", "German",
             "Italian", "Wolof", "Mandinka", "Fula", "Hausa", "Amharic"]

SUMMARY_LENGTHS = {"brief": "Brief", "standard": "Standard", "detailed": "Detailed"}
SUMMARY_STYLES = {"brief": "Executive brief", "bullets": "Bullet points", "paragraph": "One paragraph",
                  "action": "What I need to do before signing"}
CONDITION_OPS = {"contains": "contains", "not_contains": "does not contain", "equals": "is", "not_equals": "is not",
                 "gt": "is more than", "lt": "is less than", "empty": "is empty", "not_empty": "is not empty"}
REDACT_KINDS = {"emails": "Email addresses", "phones": "Phone numbers", "ids": "ID / passport numbers",
                "banking": "Bank and card details", "names": "People's names (needs AI)"}
NOTE_COLORS = ["#FEF3C7", "#DCFCE7", "#DBEAFE", "#FCE7F3", "#EDE9FE"]

SOURCE_LABELS = {"auto": "The previous card's result (or the document)", "document": "The document",
                 "previous": "The previous card's result"}

#: Cards whose result is something to read on. The rest decide, deliver or file.
CONTENT_TYPES = {"summarize", "extract", "translate", "redact", "ask", "check", "approval", "policy"}

MAX_CARDS = 60
MAX_CATEGORIES = 8
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AgentError(Exception):
    """A sentence a person can act on."""


class CardError(Exception):
    """One card failed; the run decides whether to stop or carry on."""


def ports_for(node):
    if (node or {}).get("type") == "classify":
        return [c["id"] for c in ((node.get("config") or {}).get("categories") or [])] + ["other"]
    return CARD_TYPES.get((node or {}).get("type"), {}).get("ports", [])


def port_label(node, port):
    if (node or {}).get("type") == "classify":
        for c in (node.get("config") or {}).get("categories") or []:
            if c["id"] == port:
                return c["label"]
    return PORT_LABELS.get(port, port)


# ─────────────────────────────────────────────────────────────────────────────
# Cleaning what the designer sends
# ─────────────────────────────────────────────────────────────────────────────

def _s(v, n):
    return str(v if v is not None else "")[:n]


def _num(v, default=0, lo=-5000, hi=20000):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _ids(values, limit=30):
    out = []
    for v in list(values or [])[:limit]:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _emails(values, limit=30):
    out = []
    for v in list(values or [])[:limit]:
        e = str(v or "").strip().lower()
        if _EMAIL_RE.match(e) and len(e) <= 254:
            out.append(e)
    return list(dict.fromkeys(out))


def _source(v, node_ids):
    v = str(v or "auto")
    return v if v in SOURCE_LABELS or v in node_ids else "auto"


def _clean_config(t, c, node_ids):
    c = c if isinstance(c, dict) else {}
    out = {"context": _s(c.get("context"), 4000)}
    if t not in ("start", "note", "end"):
        out["on_error"] = "continue" if c.get("on_error") == "continue" else "stop"
    if t == "start":
        out["trigger"] = c.get("trigger") if c.get("trigger") in TRIGGERS else "manual"
        out["language"] = _s(c.get("language"), 40).strip()
        out["mask_pii"] = bool(c.get("mask_pii"))
    elif t == "link":
        out["urls"] = [u for u in (_s(x, 500).strip() for x in list(c.get("urls") or [])[:10]) if u]
        out["text"] = _s(c.get("text"), 20000)
    elif t == "summarize":
        out["length"] = c.get("length") if c.get("length") in SUMMARY_LENGTHS else "standard"
        out["style"] = c.get("style") if c.get("style") in SUMMARY_STYLES else "brief"
        out["source"] = _source(c.get("source"), node_ids)
    elif t == "extract":
        out["fields"] = [f for f in (_s(x, 80).strip() for x in list(c.get("fields") or [])[:30]) if f]
        out["source"] = _source(c.get("source"), node_ids)
    elif t == "classify":
        cats, seen = [], set()
        for cat in list(c.get("categories") or [])[:MAX_CATEGORIES]:
            if not isinstance(cat, dict):
                continue
            cid = str(cat.get("id") or "")
            if not _ID_RE.match(cid) or cid in seen or cid == "other":
                cid = f"c{len(cats) + 1}"
                while cid in seen:
                    cid += "x"
            seen.add(cid)
            cats.append({"id": cid, "label": _s(cat.get("label"), 40).strip() or f"Category {len(cats) + 1}",
                         "hint": _s(cat.get("hint"), 200)})
        out["categories"] = cats
        out["source"] = _source(c.get("source"), node_ids)
    elif t == "check":
        out["strictness"] = "strict" if c.get("strictness") == "strict" else "standard"
        out["source"] = _source(c.get("source"), node_ids)
    elif t == "translate":
        out["language"] = _s(c.get("language"), 40).strip() or "French"
        out["source"] = _source(c.get("source"), node_ids)
    elif t == "redact":
        kinds = [k for k in list(c.get("kinds") or []) if k in REDACT_KINDS]
        out["kinds"] = kinds or ["emails", "phones", "ids", "banking"]
        out["source"] = _source(c.get("source"), node_ids)
    elif t == "ask":
        out["source"] = _source(c.get("source"), node_ids)
        out["max_words"] = int(_num(c.get("max_words"), 400, 20, 3000))
    elif t == "convert":
        out["target"] = c.get("target") if c.get("target") in D.TARGETS else "pdf"
        src = str(c.get("source") or "document")
        out["source"] = src if src in ("document", "previous") or src in node_ids else "document"
        out["filename"] = _s(c.get("filename"), 120).strip()
    elif t == "cover":
        out["source"] = _source(c.get("source"), node_ids)
        out["title"] = _s(c.get("title"), 150).strip()
    elif t == "save":
        pass
    elif t == "condition":
        out["mode"] = "rule" if c.get("mode") == "rule" else "ai"
        subj = str(c.get("subject") or "previous")
        out["subject"] = subj if subj in ("previous", "document") or subj in node_ids else "previous"
        out["op"] = c.get("op") if c.get("op") in CONDITION_OPS else "contains"
        out["value"] = _s(c.get("value"), 200)
    elif t == "approval":
        out["users"] = _ids(c.get("users"), 20)
        out["emails"] = _emails(c.get("emails"), 20)
        out["include_runner"] = bool(c.get("include_runner"))
        out["due_days"] = int(_num(c.get("due_days"), 0, 0, 60))
        out["allow_edit"] = c.get("allow_edit", True) is not False
        out["source"] = _source(c.get("source"), node_ids)
    elif t == "email":
        out["users"] = _ids(c.get("users"), 50)
        out["emails"] = _emails(c.get("emails"), 50)
        out["to_runner"] = bool(c.get("to_runner"))
        out["to_sender"] = bool(c.get("to_sender"))
        out["to_signers"] = bool(c.get("to_signers"))
        out["subject"] = _s(c.get("subject"), 200)
        inc = c.get("include")
        if isinstance(inc, list):
            out["include"] = [x for x in inc if x in node_ids][:20]
        else:
            out["include"] = inc if inc in ("previous", "all", "none") else "previous"
        out["attach_document"] = bool(c.get("attach_document"))
        out["attach_files"] = c.get("attach_files", True) is not False
        out["ai_write"] = bool(c.get("ai_write"))
    elif t == "envnote":
        out["visibility"] = "everyone" if c.get("visibility") == "everyone" else "internal"
        out["source"] = _source(c.get("source"), node_ids)
    elif t == "webhook":
        out["url"] = _s(c.get("url"), 500).strip()
        out["format"] = c.get("format") if c.get("format") in ("teams", "slack", "json") else "teams"
        out["source"] = _source(c.get("source"), node_ids)
    elif t == "policy":
        out.update(PO.clean_config(c))
        out["source"] = _source(c.get("source") or "document", node_ids)
    elif t == "note":
        out["color"] = c.get("color") if c.get("color") in NOTE_COLORS else NOTE_COLORS[0]
    return out


def clean_graph(graph):
    """Whatever the browser sends -> a graph the engine can trust."""
    graph = graph if isinstance(graph, dict) else {}
    nodes, seen = [], set()
    raw_nodes = [n for n in list(graph.get("nodes") or [])[:MAX_CARDS + 20] if isinstance(n, dict)]
    node_ids = {str(n.get("id")) for n in raw_nodes if _ID_RE.match(str(n.get("id") or ""))}
    has_start = False
    for n in raw_nodes:
        t = n.get("type")
        nid = str(n.get("id") or "")
        if t not in CARD_TYPES or not _ID_RE.match(nid) or nid in seen:
            continue
        if t == "start":
            if has_start:
                continue
            has_start = True
        seen.add(nid)
        nodes.append({
            "id": nid, "type": t,
            "label": _s(n.get("label"), 120).strip() or CARD_TYPES[t]["label"],
            "x": round(_num(n.get("x"), 40)), "y": round(_num(n.get("y"), 40)),
            "config": _clean_config(t, n.get("config"), node_ids - {nid}),
        })
    by_id = {n["id"]: n for n in nodes}
    edges, seen_e = [], set()
    for e in list(graph.get("edges") or [])[:400]:
        if not isinstance(e, dict):
            continue
        a, b, port = by_id.get(str(e.get("from"))), by_id.get(str(e.get("to"))), str(e.get("port") or "next")
        if not a or not b or a["id"] == b["id"] or b["type"] in ("start", "note") or port not in ports_for(a):
            continue
        key = (a["id"], port, b["id"])
        if key in seen_e:
            continue
        seen_e.add(key)
        eid = str(e.get("id") or "")
        edges.append({"id": eid if _ID_RE.match(eid) else f"e{len(edges) + 1}", "from": a["id"], "port": port, "to": b["id"]})
    return {"nodes": nodes, "edges": edges}


def start_node(graph):
    return next((n for n in (graph or {}).get("nodes") or [] if n["type"] == "start"), None)


def trigger_of(graph):
    s = start_node(graph)
    return (s or {}).get("config", {}).get("trigger") or "manual"


# ─────────────────────────────────────────────────────────────────────────────
# Checking a design
# ─────────────────────────────────────────────────────────────────────────────

def topo_order(graph):
    """Cards in the order they can run; raises AgentError on a loop."""
    nodes = [n for n in graph.get("nodes") or [] if n["type"] != "note"]
    ids = {n["id"] for n in nodes}
    indeg = {n["id"]: 0 for n in nodes}
    out = {n["id"]: [] for n in nodes}
    for e in graph.get("edges") or []:
        if e["from"] in ids and e["to"] in ids:
            indeg[e["to"]] += 1
            out[e["from"]].append(e["to"])
    pos = {n["id"]: (0 if n["type"] == "start" else 1, n.get("x", 0), n.get("y", 0)) for n in nodes}
    ready = sorted([i for i, d in indeg.items() if d == 0], key=lambda i: pos[i])
    order = []
    while ready:
        i = ready.pop(0)
        order.append(i)
        for j in out[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                ready.append(j)
                ready.sort(key=lambda k: pos[k])
    if len(order) != len(nodes):
        raise AgentError("The flow goes round in a circle. Agentic flows run forwards only — remove the connection that loops back.")
    return order


def validate_graph(graph):
    errors, warnings = [], []

    def err(node, text):
        errors.append({"node": node["id"] if node else "", "text": text})

    def warn(node, text):
        warnings.append({"node": node["id"] if node else "", "text": text})

    nodes = graph.get("nodes") or []
    start = start_node(graph)
    if not start:
        err(None, "Add a Trigger card — it is where every run starts.")
    try:
        topo_order(graph)
    except AgentError as exc:
        err(None, str(exc))

    reach = set()
    if start:
        stack = [start["id"]]
        while stack:
            cur = stack.pop()
            if cur in reach:
                continue
            reach.add(cur)
            stack.extend(e["to"] for e in graph.get("edges") or [] if e["from"] == cur)
    working = [n for n in nodes if n["type"] not in ("start", "note")]
    if start and not working:
        warn(start, "Drag some cards onto the canvas and connect them to the Trigger.")
    ids_with_knowledge_before = _with_upstream(graph, "link")
    trig = trigger_of(graph)

    for n in nodes:
        t, c = n["type"], n.get("config") or {}
        if t in ("note", "start"):
            continue
        if start and n["id"] not in reach:
            warn(n, f"“{n['label']}” isn't connected to the Trigger, so it never runs.")
        if t == "link" and not c.get("urls") and not (c.get("text") or "").strip():
            err(n, f"“{n['label']}”: add at least one web address, or paste the reference text.")
        for u in c.get("urls") or []:
            if not re.match(r"^https?://", u, re.I):
                err(n, f"“{n['label']}”: “{u[:60]}” must start with https://")
            elif not re.match(r"^https?://[^/\s]+\.[^/\s]+", u, re.I):
                err(n, f"“{n['label']}”: replace “{u[:60]}” with the full address of the page to read.")
        if t == "extract" and not c.get("fields"):
            err(n, f"“{n['label']}”: list the facts to extract, e.g. Vendor, Contract value, End date.")
        if t == "classify" and len(c.get("categories") or []) < 2:
            err(n, f"“{n['label']}”: give it at least two categories.")
        if t == "check" and n["id"] not in ids_with_knowledge_before and not (c.get("context") or "").strip():
            err(n, f"“{n['label']}”: put a Knowledge link before it, or write the rules in its context box.")
        if t == "ask" and not (c.get("context") or "").strip():
            err(n, f"“{n['label']}”: write what the AI should do in the context box.")
        if t == "condition":
            if c.get("mode") == "ai" and not (c.get("context") or "").strip():
                err(n, f"“{n['label']}”: write the yes/no question in the context box.")
            if c.get("mode") == "rule" and c.get("op") not in ("empty", "not_empty") and not (c.get("value") or "").strip():
                err(n, f"“{n['label']}”: give the value to compare with.")
        if t == "approval" and not (c.get("users") or c.get("emails") or c.get("include_runner")):
            err(n, f"“{n['label']}”: choose who checks the AI's work.")
        if t == "email" and not (c.get("users") or c.get("emails") or c.get("to_runner") or c.get("to_sender") or c.get("to_signers")):
            err(n, f"“{n['label']}”: choose who gets the email.")
        if t == "email" and trig == "manual" and (c.get("to_sender") or c.get("to_signers")) and not (c.get("users") or c.get("emails") or c.get("to_runner")):
            warn(n, f"“{n['label']}”: sender and signers only exist when a run starts from an envelope.")
        if t == "envnote" and trig == "manual":
            warn(n, f"“{n['label']}” only works when the run starts from an envelope; on manual runs it is skipped.")
        if t == "webhook":
            if not c.get("url"):
                err(n, f"“{n['label']}”: paste the webhook address.")
            elif not c["url"].lower().startswith("https://"):
                err(n, f"“{n['label']}”: the webhook address must start with https://")
        if t == "policy":
            perr, pwarn = PO.validate(n)
            for x in perr:
                err(n, x)
            for x in pwarn:
                warn(n, x)
            used = {e["port"] for e in graph.get("edges") or [] if e["from"] == n["id"]}
            for port in ("breach", "warning"):
                acts = ((c.get("outcomes") or {}).get(port) or {}).get("actions") or []
                if port not in used and not acts and c.get("rules"):
                    warn(n, f"“{n['label']}”: nothing happens on {PORT_LABELS[port]} — connect it or give it an action.")
        if t in ("classify", "check", "condition", "approval"):
            used = {e["port"] for e in graph.get("edges") or [] if e["from"] == n["id"]}
            missing = [port_label(n, p) for p in ports_for(n) if p not in used]
            if missing and len(missing) < len(ports_for(n)):
                warn(n, f"“{n['label']}”: nothing happens on {', '.join(missing)}.")
    has_policy_actions = any(a for n in nodes if n["type"] == "policy"
                             for o in (n["config"].get("outcomes") or {}).values() for a in o.get("actions") or [])
    if trig != "manual" and not has_policy_actions and \
            not any(n["type"] in ("email", "envnote", "webhook", "save", "approval") for n in nodes):
        warn(start, "This agent runs on its own but delivers nothing. Add an Email, Note or Teams card so someone sees the result.")
    return {"errors": errors, "warnings": warnings}


def _with_upstream(graph, kind):
    """Ids of cards that have a card of `kind` somewhere before them."""
    edges = graph.get("edges") or []
    by_id = {n["id"]: n for n in graph.get("nodes") or []}
    out = set()
    for n in graph.get("nodes") or []:
        stack, seen = [n["id"]], set()
        while stack:
            cur = stack.pop()
            for e in edges:
                if e["to"] == cur and e["from"] not in seen:
                    seen.add(e["from"])
                    if by_id.get(e["from"], {}).get("type") == kind:
                        out.add(n["id"])
                        stack = []
                        break
                    stack.append(e["from"])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Who may do what
# ─────────────────────────────────────────────────────────────────────────────

def _agency_id(user):
    from .studio_common_esign import user_agency_id

    return user_agency_id(user)


def usable_flows_q(user):
    """Flows a person may run: theirs, shared with them, their office or agency."""
    q = Q(created_by=user) | Q(shares__user=user)
    email = (getattr(user, "email", "") or "").strip()
    if email:
        q |= Q(shares__email__iexact=email)
    office_id = getattr(user, "country_office_id", None)
    agency_id = _agency_id(user)
    if office_id:
        q |= Q(share_scope="office", office_id=office_id)
        # Older rows without the office snapshot: fall back to the owner's office.
        q |= Q(share_scope="office", office_id__isnull=True, created_by__country_office_id=office_id)
    if agency_id:
        q |= Q(share_scope="agency", agency_id=agency_id)
    return q


def usable_flows(user):
    from .models_esign_agents import AgentFlow

    return AgentFlow.objects.filter(usable_flows_q(user), is_active=True).distinct()


def can_use_flow(user, flow) -> bool:
    if not getattr(user, "is_authenticated", False) or not flow:
        return False
    if flow.created_by_id == user.id:
        return True
    return usable_flows(user).filter(pk=flow.pk).exists()


def can_edit_flow(user, flow) -> bool:
    if not getattr(user, "is_authenticated", False) or not flow:
        return False
    if flow.created_by_id == user.id:
        return True
    q = Q(user=user)
    email = (getattr(user, "email", "") or "").strip()
    if email:
        q |= Q(email__iexact=email)
    return flow.shares.filter(q, role="edit").exists()


def visible_runs(user):
    from .models_esign_agents import AgentRun

    if not getattr(user, "is_authenticated", False):
        return AgentRun.objects.none()
    q = Q(run_as=user) | Q(flow__created_by=user)
    email = (getattr(user, "email", "") or "").strip()
    if email:
        q |= Q(steps__assignees__icontains=email)
    return AgentRun.objects.filter(q).distinct()


def can_view_run(user, run) -> bool:
    return visible_runs(user).filter(pk=run.pk).exists()


def can_manage_run(user, run) -> bool:
    return bool(getattr(user, "is_authenticated", False)
                and (run.run_as_id == user.id or (run.flow and run.flow.created_by_id == user.id)))


# ─────────────────────────────────────────────────────────────────────────────
# Starting a run
# ─────────────────────────────────────────────────────────────────────────────

def _site_url(path):
    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else path


def run_url(run):
    return _site_url(reverse("accounts:esign_agent_run", args=[run.pk]))


def check_url(step, email=""):
    from urllib.parse import quote

    path = reverse("accounts:esign_agent_check", args=[step.token])
    return _site_url(path + (f"?as={quote(email)}" if email else ""))


def runs_last_hour(user):
    from .models_esign_agents import AgentRun

    return AgentRun.objects.filter(run_as=user, created_at__gte=timezone.now() - timedelta(hours=1)).count()


def start_run(flow, user, *, agency, trigger="manual", document=None, document_name="", text="",
              envelope=None, dedupe_key="", is_test=False, graph=None, dispatch_now=True):
    """Create the run and set it going. Returns the AgentRun."""
    from .models_esign_agents import AgentRun

    limit = int(getattr(settings, "AGENT_MAX_RUNS_PER_HOUR", 120) or 0)
    if limit and runs_last_hour(user) >= limit:
        raise AgentError(f"You have started {limit} agent runs in the last hour, which is the limit. Try again later.")
    graph = clean_graph(graph if graph is not None else flow.graph)
    report = validate_graph(graph)
    if report["errors"]:
        raise AgentError("The flow has problems to fix first: " + "; ".join(i["text"] for i in report["errors"][:3]))
    if not document and not (text or "").strip() and not envelope:
        raise AgentError("Give the agent something to work on: upload a document or paste some text.")

    run = AgentRun(flow=flow, flow_name=flow.name, flow_version=flow.version, graph=graph, agency=agency, run_as=user,
                   trigger=trigger, envelope=envelope, dedupe_key=dedupe_key[:120], is_test=is_test,
                   document_name=(document_name or "")[:200], input_text=(text or "")[:200000],
                   provider=A.info()["label"][:80])
    if document:
        run.document.save(document_name or "document", ContentFile(document), save=False)
    run.save()
    if dispatch_now:
        dispatch(run)
    return run


def dispatch(run):
    """Run now (sync) or on a background thread once the transaction commits."""
    mode = str(getattr(settings, "AGENT_RUN_MODE", "thread")).lower()
    if mode == "sync":
        execute(run.pk)
        return
    pk = run.pk

    def _go():
        threading.Thread(target=_execute_in_thread, args=(pk,), name=f"agent-run-{pk}", daemon=True).start()

    transaction.on_commit(_go)


def _execute_in_thread(pk):
    close_old_connections()
    try:
        execute(pk)
    except Exception:  # noqa: BLE001
        logger.exception("Agent run %s crashed", pk)
    finally:
        close_old_connections()


# ─────────────────────────────────────────────────────────────────────────────
# The runner
# ─────────────────────────────────────────────────────────────────────────────

class Result:
    __slots__ = ("ports", "text", "data", "file", "message", "model", "tokens_in", "tokens_out", "wait", "status")

    def __init__(self, ports=None, text="", data=None, file=None, message="", model="", tokens_in=0, tokens_out=0,
                 wait=False, status="done"):
        self.ports = ports if ports is not None else ["next"]
        self.text, self.data, self.file, self.message = text or "", data or {}, file, message
        self.model, self.tokens_in, self.tokens_out, self.wait, self.status = model, tokens_in, tokens_out, wait, status


class Runtime:
    """Everything a card needs, read once per execution."""

    def __init__(self, run):
        self.run = run
        self.graph = run.graph or {}
        self.nodes = {n["id"]: n for n in self.graph.get("nodes") or []}
        self.edges = self.graph.get("edges") or []
        self.start = start_node(self.graph) or {"config": {}}
        self.ctx = dict(run.context or {})
        self.ctx.setdefault("outputs", {})
        self.ctx.setdefault("knowledge", [])
        self.ctx.setdefault("via", {})
        self.ctx.setdefault("files", [])
        self._raw = None
        self._text = None

    # — the document —
    def raw(self):
        if self._raw is None:
            self._raw = b""
            if self.run.document:
                f = self.run.document
                f.open("rb")
                try:
                    self._raw = f.read()
                finally:
                    f.close()
        return self._raw

    @property
    def doc_name(self):
        return self.run.document_name or (self.run.envelope.subject + ".pdf" if self.run.envelope_id else "Pasted text")

    def doc_text(self):
        if self._text is None:
            text = ""
            if self.raw():
                try:
                    text = D.read_text(self.raw(), self.doc_name)
                except D.DocError as exc:
                    raise CardError(str(exc))
            if self.run.input_text:
                text = (text + "\n\n" + self.run.input_text).strip() if text else self.run.input_text
            if (self.start.get("config") or {}).get("mask_pii"):
                text, _ = O.mask_personal_data(text)
            self._text = text
        return self._text

    def pdf_bytes(self):
        raw = self.raw()
        if raw:
            try:
                return D.to_pdf(raw, self.doc_name)
            except D.DocError as exc:
                raise CardError(str(exc))
        if self.run.input_text:
            return D.text_to_pdf(self.run.input_text, title="Pasted text")
        return None

    # — earlier results —
    def previous_id(self, nid):
        return self.ctx["via"].get(nid)

    def previous(self, nid):
        """
        Nearest earlier card on the path taken that produced *content*. Decision
        cards (Classify, If) are see-through: a Summarize after a Classify works
        on what the Classify read, not on the category name.
        """
        cur, guard = self.previous_id(nid), 0
        while cur and guard < 100:
            out = self.ctx["outputs"].get(cur)
            if out and out.get("text") and out.get("type") in CONTENT_TYPES:
                return cur, out
            cur, guard = self.previous_id(cur), guard + 1
        return None, None

    def source(self, node, key="source"):
        """-> (text, label, data). What this card should work on."""
        src = (node.get("config") or {}).get(key) or "auto"
        if src not in ("auto", "previous", "document"):
            out = self.ctx["outputs"].get(src)
            label = (self.nodes.get(src) or {}).get("label", "that card")
            if not out:
                raise CardError(f"“{label}” didn't run on the path this document took, so there is nothing to use.")
            if not out.get("text"):
                raise CardError(f"“{label}” produced no text to work on.")
            return out.get("text", ""), out.get("label", ""), out.get("data") or {}
        if src in ("auto", "previous"):
            pid, out = self.previous(node["id"])
            if out:
                return out["text"], out["label"], out.get("data") or {}
            if src == "previous":
                raise CardError("There is no earlier result to work on. Connect a card that produces text before this one.")
        text = self.doc_text()
        if not text.strip():
            raise CardError("The document has no readable text (it may be a scanned image without text).")
        return text, "document", {}

    def knowledge_text(self, limit):
        parts, used = [], 0
        for i, k in enumerate(self.ctx["knowledge"]):
            head = f"[Source {i + 1}: {k.get('title') or 'Reference'}{' — ' + k['url'] if k.get('url') else ''}]\n"
            body = k.get("text") or ""
            room = limit - used - len(head)
            if room <= 200:
                break
            if len(body) > room:
                body = body[:room] + "\n[… truncated …]"
            parts.append(head + body)
            used += len(head) + len(body)
        return "\n\n".join(parts)

    def earlier_text(self, nid, limit):
        parts, used = [], 0
        for oid, out in self.ctx["outputs"].items():
            if oid == nid or not out.get("text") or out.get("type") in ("link",):
                continue
            chunk = f"## {out.get('label')}\n{out['text']}"
            if used + len(chunk) > limit:
                chunk = chunk[: max(0, limit - used)]
            parts.append(chunk)
            used += len(chunk)
            if used >= limit:
                break
        return "\n\n".join(parts)

    def render(self, template):
        """{{document}}, {{sender}}, {{reference}}, {{today}}, {{flow}}, {{Card name}}."""
        if not template:
            return ""
        env = self.run.envelope if self.run.envelope_id else None
        builtins = {
            "document": D.stem(self.doc_name),
            "filename": self.doc_name,
            "subject": env.subject if env else self.doc_name,
            "sender": ((env.created_by.get_full_name() or env.created_by.username) if env and env.created_by_id else ""),
            "reference": self.run.reference,
            "envelope": env.short_id if env else "",
            "today": timezone.localdate().strftime("%d %b %Y"),
            "flow": self.run.flow_name,
            "me": (self.run.run_as.get_full_name() or self.run.run_as.username) if self.run.run_as_id else "",
        }
        by_label = {(o.get("label") or "").strip().lower(): o for o in self.ctx["outputs"].values()}

        def sub(m):
            key = m.group(1).strip()
            low = key.lower()
            if low in builtins:
                return str(builtins[low])
            if key in self.ctx["outputs"]:
                return self.ctx["outputs"][key].get("text", "")
            if low in by_label:
                return by_label[low].get("text", "")
            return m.group(0)

        return re.sub(r"\{\{\s*([^{}]{1,80}?)\s*\}\}", sub, template)

    # — AI —
    def system_prompt(self):
        cfg = self.start.get("config") or {}
        parts = []
        if (cfg.get("context") or "").strip():
            parts.append("Standing instructions from the person who built this flow:\n" + self.render(cfg["context"]).strip())
        if cfg.get("language"):
            parts.append(f"Write your answer in {cfg['language']} unless a step says otherwise.")
        return "\n\n".join(parts)

    def ai(self, node, task, *, text=None, label="document", json_expected=False, max_tokens=1500, with_knowledge=True,
           with_earlier=True):
        budget = int(getattr(settings, "AGENT_AI_MAX_INPUT_CHARS", 60000) or 60000)
        ctx_text = self.render((node.get("config") or {}).get("context") or "").strip()
        prompt = [task.strip()]
        if ctx_text:
            prompt.append(f"Instructions for this step from the person who built the flow:\n<step_instructions>\n{ctx_text}\n</step_instructions>")
        if with_knowledge and self.ctx["knowledge"]:
            prompt.append(f"<reference_material>\n{self.knowledge_text(int(budget * 0.3))}\n</reference_material>")
        if with_earlier:
            earlier = self.earlier_text(node["id"], int(budget * 0.12))
            if earlier and label == "document":
                prompt.append(f"<earlier_results>\n{earlier}\n</earlier_results>")
        if text is None:
            text = self.doc_text()
        room = int(budget * 0.58)
        if len(text) > room:
            text = text[:room] + "\n[… the rest of the document was cut to fit …]"
        name = self.doc_name if label == "document" else label
        prompt.append(f'<document name="{name}">\n{text}\n</document>')
        result = A.complete(self.system_prompt(), "\n\n".join(prompt), max_tokens=max_tokens, json_expected=json_expected)
        return result


def execute(run_id):
    """Claim the run and work through its cards until it finishes or waits."""
    from .models_esign_agents import AgentRun, AgentStep

    claimed = AgentRun.objects.filter(pk=run_id, status=AgentRun.STATUS_QUEUED).update(
        status=AgentRun.STATUS_RUNNING, updated_at=timezone.now())
    if not claimed:
        return
    run = AgentRun.objects.select_related("envelope", "envelope__created_by", "run_as", "flow").get(pk=run_id)
    if not run.started_at:
        run.started_at = timezone.now()
        run.save(update_fields=["started_at"])
    rt = Runtime(run)
    ctx = rt.ctx
    try:
        order = topo_order(rt.graph)
    except AgentError as exc:
        return _finish(run, ctx, AgentRun.STATUS_FAILED, str(exc))
    start = rt.start
    active = set(ctx.get("active") or ([start["id"]] if start.get("id") else []))
    done = set(ctx.get("done") or [])
    failed_stop = ""

    for nid in order:
        if ctx.get("stop"):
            break
        if nid in done or nid not in active:
            continue
        run.refresh_from_db(fields=["status"])
        if run.status == AgentRun.STATUS_CANCELLED:
            return
        node = rt.nodes[nid]
        t = node["type"]
        step = AgentStep.objects.create(run=run, node_id=nid, node_label=node["label"][:150], kind=t)
        began = time.monotonic()
        try:
            fn = EXECUTORS.get(t, _exec_passthrough)
            res = fn(rt, node, step)
        except (CardError, D.DocError, A.AIError) as exc:
            res = None
            message = str(exc)[:400]
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent run %s: card %s (%s) crashed", run.pk, nid, t)
            res = None
            message = f"Something went wrong in this card ({exc.__class__.__name__}). The details are in the server log."

        if res is None:
            step.status = AgentStep.STATUS_FAILED
            step.message = message
            step.finished_at = timezone.now()
            step.save()
            ctx["outputs"][nid] = {"label": node["label"], "type": t, "text": "", "failed": True}
            if (node.get("config") or {}).get("on_error") == "continue":
                ports = [_fallback_port(node)]
            else:
                failed_stop = f"“{node['label']}” failed: {message}"
                done.add(nid)
                break
        else:
            step.output_text = res.text[:200000]
            step.output_data = res.data
            step.message = res.message[:400]
            step.model, step.tokens_in, step.tokens_out = res.model[:80], res.tokens_in, res.tokens_out
            step.port = (res.ports[0] if res.ports else "")[:40]
            if res.file:
                fname, fbytes = res.file
                step.output_file_name = fname[:200]
                step.output_file.save(fname, ContentFile(fbytes), save=False)
            run.tokens_in += res.tokens_in
            run.tokens_out += res.tokens_out
            if res.wait:
                step.status = AgentStep.STATUS_WAITING
                step.port = ""
                step.save()
                ctx["active"], ctx["done"] = sorted(active), sorted(done)
                ctx["waiting"] = nid
                run.context = ctx
                run.status = AgentRun.STATUS_WAITING
                run.save(update_fields=["context", "status", "tokens_in", "tokens_out", "updated_at"])
                _notify_check(run, step, node)
                return
            step.status = res.status
            step.finished_at = timezone.now()
            step.save()
            if res.file:
                ctx["files"].append({"step": step.pk, "node": nid, "name": step.output_file_name})
            ctx["outputs"][nid] = {"label": node["label"], "type": t, "text": res.text[:60000], "data": res.data,
                                   "step": step.pk}
            ports = res.ports
        for e in rt.edges:
            if e["from"] == nid and e["port"] in ports:
                active.add(e["to"])
                ctx["via"][e["to"]] = nid
        done.add(nid)
        ctx["active"], ctx["done"] = sorted(active), sorted(done)
        run.context = ctx
        run.save(update_fields=["context", "tokens_in", "tokens_out", "updated_at"])
        logger.debug("Agent run %s: %s done in %.1fs", run.pk, nid, time.monotonic() - began)

    if failed_stop:
        return _finish(run, ctx, AgentRun.STATUS_FAILED, failed_stop)
    if ctx.get("stop"):
        return _finish(run, ctx, AgentRun.STATUS_STOPPED, ctx["stop"])
    return _finish(run, ctx, AgentRun.STATUS_COMPLETED, "")


def _fallback_port(node):
    t = node["type"]
    return {"classify": "other", "check": "issues", "condition": "no", "approval": "rejected",
            "policy": "warning"}.get(t, "next")


def _finish(run, ctx, status, error):
    from .models_esign_agents import AgentRun

    ctx.pop("waiting", None)
    run.context = ctx
    run.status = status
    run.error = (error or "")[:2000]
    run.finished_at = timezone.now()
    run.save(update_fields=["context", "status", "error", "finished_at", "tokens_in", "tokens_out", "updated_at"])
    if status == AgentRun.STATUS_FAILED and run.trigger != "manual" and getattr(settings, "AGENT_NOTIFY_FAILURES", True):
        _notify_failure(run)
    return run


# ─────────────────────────────────────────────────────────────────────────────
# The cards
# ─────────────────────────────────────────────────────────────────────────────

def _exec_passthrough(rt, node, step):
    return Result(message="Nothing to do.")


def _exec_start(rt, node, step):
    run = rt.run
    bits = [TRIGGERS.get(run.trigger, run.trigger)]
    if run.envelope_id:
        bits.append(f"Envelope “{run.envelope.subject}” ({run.envelope.short_id})")
    elif run.document_name:
        bits.append(run.document_name)
    elif run.input_text:
        bits.append(f"{len(run.input_text):,} characters of pasted text")
    info = A.info()
    return Result(text="", message=" · ".join(bits) + f" · AI: {info['label']}", data={"provider": info})


def _exec_link(rt, node, step):
    c = node["config"]
    read, problems = [], []
    about = rt.render(c.get("context") or "").strip()
    about = f"About this source (from the flow's author): {about}\n\n" if about else ""
    for url in c.get("urls") or []:
        try:
            page = D.fetch_url(url)
            rt.ctx["knowledge"].append({"title": page["title"], "url": url, "text": about + page["text"][:120000]})
            read.append({"title": page["title"], "url": url, "chars": len(page["text"])})
        except D.DocError as exc:
            problems.append(str(exc))
    if (c.get("text") or "").strip():
        rt.ctx["knowledge"].append({"title": node["label"], "url": "", "text": about + c["text"]})
        read.append({"title": "Pasted reference text", "url": "", "chars": len(c["text"])})
    if not read:
        raise CardError("Nothing could be read: " + " ".join(problems[:3]))
    lines = [f"• {r['title']}" + (f" — {r['url']}" if r["url"] else "") + f" ({r['chars']:,} characters)" for r in read]
    return Result(text="Read:\n" + "\n".join(lines), data={"sources": read, "problems": problems},
                  message=("Some links failed: " + " ".join(problems))[:400] if problems else f"Read {len(read)} source(s).")


def _ai_result(res: A.AIResult, text=None, **kw):
    return Result(text=text if text is not None else res.text, model=res.model, tokens_in=res.tokens_in,
                  tokens_out=res.tokens_out, **kw)


STYLE_TASKS = {
    "brief": ("an executive brief with these short sections: In short (one or two sentences on what this is and "
              "who it is between); Key points; Key facts (parties, amounts, dates, references — only those stated); "
              "Obligations and deadlines; Points to check before signing"),
    "bullets": "a list of bullet points, most important first",
    "paragraph": "one well-written paragraph",
    "action": ("what the reader must know or do before signing: what they are agreeing to, the money and dates "
               "involved, any risks or unusual terms, and questions worth asking the sender"),
}
LENGTH_TASKS = {"brief": "Keep it very short (under 80 words).", "standard": "Keep it under 220 words.",
                "detailed": "Be thorough but stay under 500 words."}


def _exec_summarize(rt, node, step):
    c = node["config"]
    text, label, _ = rt.source(node)
    if A.is_offline():
        out = O.summarize(text, length=c["length"], style=c["style"], title=rt.doc_name if label == "document" else label)
        return Result(text=out, model="built-in", message="Built-in summary (no AI provider) — the most informative sentences, with dates and amounts.")
    res = rt.ai(node, f"Summarise the document below as {STYLE_TASKS[c['style']]}. {LENGTH_TASKS[c['length']]} "
                      "Use plain text with simple '-' bullets and short headings ending with a colon. No Markdown tables.",
                text=text, label=label, max_tokens=1400)
    return _ai_result(res)


def _exec_extract(rt, node, step):
    fields = node["config"]["fields"]
    text, label, _ = rt.source(node)
    if A.is_offline():
        values = O.extract(text, fields)
        model, note = "built-in", "Built-in extraction: looks for “Label: value” lines and the kind of value asked for."
        tin = tout = 0
    else:
        res = rt.ai(node, "Extract these facts from the document. Use the exact wording or figure from the document. "
                          "If a fact is not stated, use null — never guess.\n"
                          f"Facts: {json.dumps(fields, ensure_ascii=False)}\n"
                          'Reply as {"fields": {"<fact>": "<value or null>", ...}, "notes": "<anything worth flagging, or empty>"}',
                    text=text, label=label, json_expected=True, max_tokens=1200)
        got = res.data.get("fields") if isinstance(res.data.get("fields"), dict) else {}
        values = {f: ("" if got.get(f) in (None, "null") else str(got.get(f)))[:500] for f in fields}
        model, note, tin, tout = res.model, str(res.data.get("notes") or "")[:300], res.tokens_in, res.tokens_out
    found = sum(1 for v in values.values() if v)
    lines = [f"{k}: {v or '— not stated —'}" for k, v in values.items()]
    return Result(text="\n".join(lines), data={"fields": values}, model=model, tokens_in=tin, tokens_out=tout,
                  message=(note + " " if note else "") + f"Found {found} of {len(fields)}.")


def _exec_classify(rt, node, step):
    cats = node["config"]["categories"]
    text, label, _ = rt.source(node)
    if A.is_offline():
        cid, why = O.classify(text, cats)
        model, tin, tout = "built-in", 0, 0
    else:
        menu = "\n".join(f"- {c['id']}: {c['label']}" + (f" — {c['hint']}" if c.get("hint") else "") for c in cats)
        res = rt.ai(node, "Classify the document into exactly one of these categories, or 'other' if none fits:\n"
                          f"{menu}\n- other: none of the above\n"
                          'Reply as {"category": "<id>", "confidence": <0-1>, "reason": "<one sentence>"}',
                    text=text, label=label, json_expected=True, max_tokens=300)
        cid = str(res.data.get("category") or "other")
        why = str(res.data.get("reason") or "")[:300]
        model, tin, tout = res.model, res.tokens_in, res.tokens_out
    valid = {c["id"]: c["label"] for c in cats}
    if cid not in valid:
        by_label = {c["label"].lower(): c["id"] for c in cats}
        cid = by_label.get(cid.lower(), "other")
    name = valid.get(cid, "Other")
    return Result(ports=[cid], text=name, data={"category": cid, "label": name, "reason": why}, model=model,
                  tokens_in=tin, tokens_out=tout, message=f"{name}" + (f" — {why}" if why else ""))


def _exec_check(rt, node, step):
    c = node["config"]
    text, label, _ = rt.source(node)
    rules = rt.render(c.get("context") or "")
    reference = "\n\n".join(k["text"] for k in rt.ctx["knowledge"])
    if not reference.strip() and not rules.strip():
        raise CardError("There is no policy to check against. Put a Knowledge link before this card or write the rules in its context box.")
    if A.is_offline():
        issues, checked = O.policy_check(text, reference + "\n" + rules)
        findings = [{"severity": "medium", "finding": i} for i in issues]
        summary = (f"Checked {checked} requirement sentence(s) in the reference material. "
                   + (f"{len(issues)} are not addressed by the document." if issues else "The document mentions all of them."))
        model, tin, tout = "built-in", 0, 0
    else:
        strict = "Be strict: flag anything unclear or missing." if c.get("strictness") == "strict" else \
            "Only flag real conflicts or clearly missing requirements, not matters of style."
        res = rt.ai(node, "Check the document against the policy in the reference material (and any rules in the step "
                          f"instructions). {strict}\n"
                          'Reply as {"verdict": "pass" or "issues", "summary": "<two sentences>", "issues": '
                          '[{"severity": "high|medium|low", "rule": "<the rule, quoted or paraphrased, with its source>", '
                          '"finding": "<what the document says or lacks>"}]}',
                    text=text, label=label, json_expected=True, max_tokens=1800)
        findings = [i for i in (res.data.get("issues") or []) if isinstance(i, dict)][:20]
        summary = str(res.data.get("summary") or "")[:600]
        if res.data.get("verdict") == "pass":
            findings = [f for f in findings if f.get("severity") == "high"]
        model, tin, tout = res.model, res.tokens_in, res.tokens_out
    lines = [summary] if summary else []
    for f in findings:
        sev = str(f.get("severity") or "").upper()
        rule = f" (rule: {f['rule']})" if f.get("rule") else ""
        lines.append(f"• {('[' + sev + '] ') if sev else ''}{f.get('finding', '')}{rule}")
    port = "issues" if findings else "pass"
    return Result(ports=[port], text="\n".join(lines) or "No issues found.", data={"issues": findings, "verdict": port},
                  model=model, tokens_in=tin, tokens_out=tout,
                  message=f"{len(findings)} issue(s) found." if findings else "No issues found.")


def _exec_translate(rt, node, step):
    lang = node["config"]["language"]
    text, label, _ = rt.source(node)
    if A.is_offline():
        return Result(text=text, status="skipped", message=f"Not translated: translating into {lang} needs an AI provider. "
                                                         "The text was passed on unchanged.")
    res = rt.ai(node, f"Translate the document into {lang}. Keep its structure, headings, lists, numbers, dates and names. "
                      "Reply with the translation only.", text=text, label=label, max_tokens=4000, with_earlier=False,
                with_knowledge=False)
    return _ai_result(res, message=f"Translated into {lang}.")


def _exec_redact(rt, node, step):
    kinds = node["config"]["kinds"]
    text, label, _ = rt.source(node)
    masked, counts = O.mask_personal_data(text, kinds)
    model, tin, tout, note = "built-in", 0, 0, ""
    if "names" in kinds:
        if A.is_offline():
            note = " Names were not masked: that needs an AI provider."
        else:
            res = rt.ai(node, "List every personal name of an individual person in the document (not organisations, not "
                              'places, not job titles). Reply as {"names": ["..."]}', text=masked, label=label,
                        json_expected=True, max_tokens=800, with_knowledge=False, with_earlier=False)
            names = sorted({str(n).strip() for n in (res.data.get("names") or []) if len(str(n).strip()) > 2}, key=len, reverse=True)
            n = 0
            for name in names[:200]:
                masked, k = re.subn(re.escape(name), "[NAME]", masked)
                n += k
            if n:
                counts["names"] = n
            model, tin, tout = res.model, res.tokens_in, res.tokens_out
    total = sum(counts.values())
    summary = ", ".join(f"{v} {REDACT_KINDS[k].split(' (')[0].lower()}" for k, v in counts.items()) or "nothing found"
    return Result(text=masked, data={"counts": counts}, model=model, tokens_in=tin, tokens_out=tout,
                  message=f"Masked {total}: {summary}.{note}")


def _exec_ask(rt, node, step):
    c = node["config"]
    text, label, _ = rt.source(node)
    if A.is_offline():
        return Result(text="", status="skipped", message="Skipped: custom AI steps need an AI provider. ICT can set one up.")
    res = rt.ai(node, f"Do what the step instructions ask, using the document and reference material. "
                      f"Answer in at most {c.get('max_words', 400)} words, in plain text.", text=text, label=label,
                max_tokens=min(4000, int(c.get("max_words", 400) * 2.2) + 200))
    return _ai_result(res)


def _exec_convert(rt, node, step):
    c = node["config"]
    target = c["target"]
    title = rt.render(c.get("filename") or "")
    if c["source"] == "document":
        raw = rt.raw()
        if raw:
            data, name, note = D.convert(target, raw=raw, name=rt.doc_name, title=title or D.stem(rt.doc_name))
        elif rt.run.input_text:
            data, name, note = D.convert(target, text=rt.run.input_text, title=title or "Pasted text")
        else:
            raise CardError("There is no document to convert.")
    else:
        text, label, extra = rt.source(node)
        intro = rt.render(c.get("context") or "").strip()
        if intro:
            text = intro + "\n\n" + text
        data, name, note = D.convert(target, text=text, data=extra,
                                     title=title or f"{D.stem(rt.doc_name)} — {label}")
    pretty = D.TARGETS[target][0]
    return Result(file=(name, data), text="", data={"format": target, "name": name, "bytes": len(data)},
                  message=(f"Made {pretty} file {name} ({len(data) // 1024 or 1} KB). " + (note or "")).strip())


def _exec_cover(rt, node, step):
    c = node["config"]
    text, label, _ = rt.source(node)
    run = rt.run
    meta = [("Document", rt.doc_name)]
    if run.envelope_id:
        env = run.envelope
        meta.append(("Envelope", env.short_id))
        if env.created_by_id:
            meta.append(("From", env.created_by.get_full_name() or env.created_by.username))
    meta += [("Prepared", timezone.localtime().strftime("%d %b %Y %H:%M")), ("Agent run", run.reference),
             ("Summary source", label)]
    original = rt.pdf_bytes()
    intro = rt.render(c.get("context") or "").strip()
    if intro:
        text = intro + "\n\n" + text
    pdf = D.cover_page(text, title=rt.render(c.get("title") or "") or f"Summary — {D.stem(rt.doc_name)}", meta=meta,
                       original_pdf=original)
    name = f"{D.stem(rt.doc_name)} - with summary.pdf"
    return Result(file=(name, pdf), data={"format": "pdf", "name": name, "bytes": len(pdf)},
                  message="Summary page added in front of the document." if original else "Made a summary sheet.")


def _exec_save(rt, node, step):
    from .models_esign_agents import AgentStep
    from .views_esign_studio import create_studio_file

    run = rt.run
    if not run.run_as_id:
        raise CardError("There is nobody to save the files for.")
    if rt.ctx.get("dry_run"):
        return Result(status="skipped", message="Dry run — would have saved this run's PDFs to your PDF tools.")
    saved = []
    for f in rt.ctx["files"]:
        s = AgentStep.objects.filter(pk=f.get("step"), run=run).first()
        if not s or not s.output_file or not s.output_file_name.lower().endswith(".pdf"):
            continue
        s.output_file.open("rb")
        try:
            raw = s.output_file.read()
        finally:
            s.output_file.close()
        sf = create_studio_file(run.run_as, run.agency, raw, s.output_file_name, origin="agent",
                                operation=f"Made by agent {run.flow_name} ({run.reference})")
        saved.append({"id": sf.pk, "name": sf.name})
    if not saved:
        pdf = rt.pdf_bytes()
        if not pdf:
            raise CardError("There is no PDF to save.")
        sf = create_studio_file(run.run_as, run.agency, pdf, f"{D.stem(rt.doc_name)}.pdf", origin="agent",
                                operation=f"Saved by agent {run.flow_name} ({run.reference})")
        saved.append({"id": sf.pk, "name": sf.name})
    return Result(data={"saved": saved}, message=f"Saved {len(saved)} file(s) to PDF tools: " + ", ".join(s["name"] for s in saved))


def _compare(op, subject, value):
    a, b = (subject or "").strip(), (value or "").strip()
    if op == "empty":
        return not a
    if op == "not_empty":
        return bool(a)
    if op == "contains":
        return b.lower() in a.lower()
    if op == "not_contains":
        return b.lower() not in a.lower()
    if op == "equals":
        return a.lower() == b.lower()
    if op == "not_equals":
        return a.lower() != b.lower()
    x, y = O._to_number(a), O._to_number(b)
    if x is None or y is None:
        return False
    return x > y if op == "gt" else x < y


def _exec_condition(rt, node, step):
    c = node["config"]
    if c["mode"] == "rule":
        if c["subject"] == "document":
            subject, name = rt.doc_text(), "The document"
        else:
            text, name, data = rt.source({"id": node["id"], "config": {"source": c["subject"]}})
            # A Classify result compares by its category name, not its explanation.
            subject = data.get("label", text) if data.get("category") else text
        ok = _compare(c["op"], subject, c.get("value", ""))
        return Result(ports=["yes" if ok else "no"], text="Yes" if ok else "No",
                      data={"answer": ok}, message=f"{name} {CONDITION_OPS[c['op']]} “{c.get('value', '')}” → {'Yes' if ok else 'No'}")
    question = rt.render(c.get("context") or "").strip()
    text, label, _ = rt.source(node)
    if A.is_offline():
        ok, why = O.yes_no(text, question)
        model, tin, tout = "built-in", 0, 0
        why = "Built-in check: " + why
    else:
        res = rt.ai({**node, "config": {**c, "context": ""}},
                    f"Answer this yes/no question about the document: {question}\n"
                    'Reply as {"answer": true or false, "reason": "<one sentence quoting the evidence>"}',
                    text=text, label=label, json_expected=True, max_tokens=300)
        raw = res.data.get("answer")
        ok = raw is True or str(raw).strip().lower() in ("true", "yes")
        why = str(res.data.get("reason") or "")[:300]
        model, tin, tout = res.model, res.tokens_in, res.tokens_out
    return Result(ports=["yes" if ok else "no"], text=("Yes" if ok else "No") + (f" — {why}" if why else ""),
                  data={"answer": ok, "question": question, "reason": why}, model=model, tokens_in=tin, tokens_out=tout,
                  message=f"{question} → {'Yes' if ok else 'No'}")


def _people_for(rt, users, emails, include_runner=False):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    out = []
    for u in User.objects.filter(pk__in=users or [], is_active=True):
        if u.email:
            out.append({"user_id": u.pk, "name": u.get_full_name() or u.username, "email": u.email.lower()})
    for e in emails or []:
        u = User.objects.filter(email__iexact=e, is_active=True).first()
        out.append({"user_id": u.pk if u else None, "name": (u.get_full_name() or u.username) if u else e, "email": e.lower()})
    if include_runner and rt.run.run_as_id and rt.run.run_as.email:
        u = rt.run.run_as
        out.append({"user_id": u.pk, "name": u.get_full_name() or u.username, "email": u.email.lower()})
    seen, uniq = set(), []
    for p in out:
        if p["email"] not in seen:
            seen.add(p["email"])
            uniq.append(p)
    return uniq


def _exec_approval(rt, node, step):
    c = node["config"]
    people = _people_for(rt, c.get("users"), c.get("emails"), c.get("include_runner"))
    if not people:
        raise CardError("Nobody with an email address was chosen to check this.")
    text, label, _ = rt.source(node)
    step.assignees = people
    step.output_text = text[:200000]
    step.output_data = {"source": label}
    if c.get("due_days"):
        step.due_at = timezone.now() + timedelta(days=c["due_days"])
    return Result(text=text, data={"source": label}, wait=True,
                  message=f"Waiting for {', '.join(p['name'] for p in people[:4])}" + (" and others" if len(people) > 4 else ""))


def _recipients(rt, c):
    people = _people_for(rt, c.get("users"), c.get("emails"), c.get("to_runner"))
    env = rt.run.envelope if rt.run.envelope_id else None
    extra = []
    if env and c.get("to_sender") and env.created_by_id and env.created_by.email:
        extra.append(env.created_by.email.lower())
    if env and c.get("to_signers"):
        extra += [r.email.lower() for r in env.signers() if r.email]
    emails = [p["email"] for p in people] + extra
    return list(dict.fromkeys(emails))[: int(getattr(settings, "AGENT_EMAIL_MAX_RECIPIENTS", 50))]


def _included(rt, node):
    inc = node["config"].get("include", "previous")
    outs = rt.ctx["outputs"]
    if inc == "none":
        return []
    if inc == "all":
        return [(o["label"], o["text"]) for o in outs.values() if o.get("text") and o.get("type") not in ("start",)]
    if isinstance(inc, list):
        return [(outs[i]["label"], outs[i]["text"]) for i in inc if i in outs and outs[i].get("text")]
    pid, out = rt.previous(node["id"])
    return [(out["label"], out["text"])] if out else []


def _attachments(rt, node):
    from .models_esign_agents import AgentStep

    c = node["config"]
    files, total = [], 0
    cap = int(getattr(settings, "AGENT_ATTACH_MAX_BYTES", 15 * 1024 * 1024))
    if c.get("attach_document"):
        raw = rt.raw()
        if raw:
            files.append((rt.doc_name, raw, "application/octet-stream"))
        elif rt.run.envelope_id:
            pdf = rt.pdf_bytes()
            if pdf:
                files.append((f"{D.stem(rt.doc_name)}.pdf", pdf, "application/pdf"))
    if c.get("attach_files"):
        for f in rt.ctx["files"]:
            s = AgentStep.objects.filter(pk=f.get("step"), run=rt.run).first()
            if not s or not s.output_file:
                continue
            s.output_file.open("rb")
            try:
                data = s.output_file.read()
            finally:
                s.output_file.close()
            ext = D._ext(s.output_file_name)
            files.append((s.output_file_name, data, D.TARGETS.get(ext, ("", "application/octet-stream"))[1]))
    out = []
    for name, data, mime in files:
        if total + len(data) > cap:
            continue
        total += len(data)
        out.append((name, data, mime))
    return out


def _exec_email(rt, node, step):
    c = node["config"]
    to = _recipients(rt, c)
    if not to:
        if rt.run.trigger == "manual" and (c.get("to_sender") or c.get("to_signers")):
            return Result(status="skipped", message="Skipped: there is no envelope, so no sender or signers to email.")
        raise CardError("None of the chosen people has an email address.")
    subject = rt.render(c.get("subject") or "") or f"{rt.run.flow_name}: {rt.doc_name}"
    parts = _included(rt, node)
    intro = rt.render(c.get("context") or "").strip()
    model, tin, tout = "", 0, 0
    if c.get("ai_write") and not A.is_offline() and parts:
        material = "\n\n".join(f"## {l}\n{t}" for l, t in parts)
        res = rt.ai(node, "Write the body of a short, professional email to colleagues that passes on the results below. "
                          "Follow the step instructions for tone and what to emphasise. Plain text, no subject line, no "
                          "sign-off name.", text=material[:30000], label="results to pass on", max_tokens=900,
                    with_knowledge=False, with_earlier=False)
        body_sections = [("", res.text)]
        model, tin, tout = res.model, res.tokens_in, res.tokens_out
    else:
        body_sections = ([("", intro)] if intro else []) + parts
    attachments = _attachments(rt, node)
    if rt.ctx.get("dry_run"):
        preview = "\n\n".join((f"{l}:\n{t}" if l else t) for l, t in body_sections)
        return Result(status="skipped", text="", data={"to": to, "subject": subject, "preview": preview[:20000],
                                                       "attachments": [a[0] for a in attachments]},
                      model=model, tokens_in=tin, tokens_out=tout,
                      message=f"Dry run — would have emailed {', '.join(to[:5])}" + ("…" if len(to) > 5 else "")
                              + (f" with {len(attachments)} attachment(s)" if attachments else "") + ".")
    _send(subject[:200], to, "accounts/esign/email/agent_message.html", {
        "headline": rt.run.flow_name,
        "subject_line": subject,
        "sections": body_sections,
        "reference": rt.run.reference,
        "document_name": rt.doc_name,
        "envelope": rt.run.envelope if rt.run.envelope_id else None,
        "run_link": run_url(rt.run),
        "attached": [a[0] for a in attachments],
        "provider": A.info()["label"],
    }, attachments=attachments)
    rt.ctx.setdefault("emails", []).append({"node": node["id"], "to": to, "subject": subject})
    return Result(text="", data={"to": to, "subject": subject, "attachments": [a[0] for a in attachments]},
                  model=model, tokens_in=tin, tokens_out=tout,
                  message=f"Emailed {len(to)} {'person' if len(to) == 1 else 'people'}: {', '.join(to[:5])}"
                          + ("…" if len(to) > 5 else "") + (f" · {len(attachments)} attachment(s)" if attachments else ""))


def _exec_envnote(rt, node, step):
    from .models_esign import EnvelopeComment
    from .utils_esign import log_event

    run = rt.run
    if not run.envelope_id:
        return Result(status="skipped", message="Skipped: this run didn't start from an envelope.")
    env = run.envelope
    if env.created_by_id != run.run_as_id:
        return Result(status="skipped", message="Skipped: only the sender's own agents can post notes on an envelope. "
                                                "Use an Email card to send the result to yourself instead.")
    text, label, _ = rt.source(node)
    intro = rt.render(node["config"].get("context") or "").strip()
    body = (intro + "\n\n" if intro else "") + text
    body = f"{body.strip()}\n\n— AI-generated by the agent “{run.flow_name}” ({run.reference}). Check it against the document."
    if rt.ctx.get("dry_run"):
        return Result(status="skipped", data={"preview": body[:20000]},
                      message="Dry run — would have posted this note on the envelope.")
    note = EnvelopeComment.objects.create(
        envelope=env, author_user=None, author_name=f"Agent · {run.flow_name}"[:150], text=body[:20000],
        is_internal=node["config"]["visibility"] != "everyone", revision=getattr(env, "revision", 1),
    )
    log_event(env, "commented", note=f"Agent note added by {run.flow_name} ({run.reference}).")
    return Result(data={"comment": note.pk}, message="Posted on the envelope "
                  + ("for everyone on it." if not note.is_internal else "as an internal note (sender's team only)."))


def _exec_webhook(rt, node, step):
    c = node["config"]
    url = c["url"]
    try:
        D.check_url(url)
    except D.DocError as exc:
        raise CardError(str(exc))
    text, label, _ = rt.source(node)
    intro = rt.render(c.get("context") or "").strip()
    title = f"{rt.run.flow_name}: {rt.doc_name}"
    body = (intro + "\n\n" if intro else "") + text
    link = run_url(rt.run)
    if c["format"] == "teams":
        payload = {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                        "version": "1.4", "body": [
                            {"type": "TextBlock", "text": title[:200], "weight": "Bolder", "size": "Medium", "wrap": True},
                            {"type": "TextBlock", "text": body[:12000], "wrap": True},
                            {"type": "TextBlock", "text": f"AI-generated · {rt.run.reference}", "isSubtle": True,
                             "size": "Small", "wrap": True}],
                        "actions": [{"type": "Action.OpenUrl", "title": "Open the run", "url": link}]}}]}
    elif c["format"] == "slack":
        payload = {"text": f"*{title}*\n{body[:12000]}\n_AI-generated · <{link}|{rt.run.reference}>_"}
    else:
        payload = {"flow": rt.run.flow_name, "reference": rt.run.reference, "document": rt.doc_name, "title": title,
                   "text": body, "result_of": label, "run_url": link,
                   "outputs": {o["label"]: o.get("text", "") for o in rt.ctx["outputs"].values() if o.get("text")}}
    if rt.ctx.get("dry_run"):
        host = re.sub(r"^https?://", "", url).split("/")[0]
        return Result(status="skipped", data={"preview": body[:20000], "host": host},
                      message=f"Dry run — would have posted to {host}.")
    opener = urllib.request.build_opener(D._SafeRedirect)
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "UN-PASS-Agent/1.0"})
    try:
        with opener.open(req, timeout=int(getattr(settings, "AGENT_LINK_TIMEOUT", 20))) as res:   # noqa: S310
            status = res.status
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "")
        raise CardError(f"The webhook didn't accept the post{(' (HTTP ' + str(code) + ')') if code else ''}.")
    host = re.sub(r"^https?://", "", url).split("/")[0]
    return Result(data={"status": status, "host": host}, message=f"Posted to {host} (HTTP {status}).")


# ─────────────────────────────────────────────────────────────────────────────
# Policy
# ─────────────────────────────────────────────────────────────────────────────

def _exec_policy(rt, node, step):
    cfg = node["config"]
    text, label, _ = rt.source(node)
    ev = PO.evaluate(rt, node, text)
    summary = PO.findings_text(cfg, ev)
    # Later AI cards can quote the policy too.
    rules_txt = "\n".join(f"- [{r['severity']}] {r['title']}: {r['text']}" for r in cfg["rules"] if r["enabled"])
    rt.ctx["knowledge"].append({"title": f"Policy: {cfg.get('title') or node['label']}", "url": "",
                                "text": (rules_txt + "\n\n" + (cfg.get("text") or ""))[:80000]})
    port = ev["outcome"]
    done, signoff, stop = _policy_actions(rt, node, ev, summary)
    data = {"policy": {"title": cfg.get("title"), "version": cfg.get("version"), "effective": cfg.get("effective")},
            "outcome": port, "outcome_label": PO.OUTCOMES[port], "results": ev["results"], "counts": ev["counts"],
            "scope_note": ev["scope_note"], "actions": done}
    msg = f"{PO.OUTCOMES[port]} · {ev['counts']['flag']} flagged, {ev['counts']['unclear']} unclear of {len(ev['results'])} rule(s)"
    msg += "".join(f" · {a['label']}" for a in done)[:300]
    common = dict(text=summary, model=ev["model"], tokens_in=ev["tokens_in"], tokens_out=ev["tokens_out"])
    if stop:
        rt.ctx["stop"] = stop
        return Result(ports=[], data=data, message=(msg + " · run stopped")[:400], **common)
    if signoff:
        step.assignees = signoff["people"]
        data["pending_port"] = port
        data["signoff_message"] = signoff["message"]
        return Result(ports=[port], data=data, wait=True, message=(msg + " · waiting for sign-off")[:400], **common)
    return Result(ports=[port], data=data, message=msg[:400], **common)


def _policy_actions(rt, node, ev, summary):
    """Run the outcome's actions (and flagged rules' own actions). -> (done[], signoff|None, stop_reason)."""
    cfg = node["config"]
    outcome = ev["outcome"]
    acts = [dict(a) for a in (cfg.get("outcomes") or {}).get(outcome, {}).get("actions") or []]
    by_id = {r["id"]: r for r in cfg["rules"]}
    for x in ev["results"]:
        then = (by_id.get(x["id"]) or {}).get("then") or {}
        if x["status"] == "flag" and then.get("type"):
            extra = {"type": then["type"], "id": then.get("id"), "rule": x["title"], "message": ""}
            if then["type"] == "signoff":
                base = next((a for a in acts if a["type"] == "signoff"), None)
                extra.update(users=(base or {}).get("users", []), emails=(base or {}).get("emails", []),
                             to_runner=(base or {}).get("to_runner", True) if base else True)
            acts.append(extra)
    title = cfg.get("title") or node["label"]
    dry = bool(rt.ctx.get("dry_run"))
    done, seen, signoff, stop = [], set(), None, ""
    for a in acts:
        key = (a["type"], a.get("id"))
        if key in seen and a["type"] in ("agent", "workflow", "stop", "signoff"):
            continue
        seen.add(key)
        why = f" (rule: {a['rule']})" if a.get("rule") else ""
        try:
            if a["type"] in ("inform", "warn"):
                done.append(_policy_email(rt, node, a, ev, summary, dry))
            elif a["type"] == "agent":
                done.append(_policy_start_agent(rt, a, dry, why))
            elif a["type"] == "workflow":
                done.append(_policy_start_workflow(rt, node, a, ev, summary, dry, why))
            elif a["type"] == "stop":
                stop = stop or f"Stopped by the policy “{title}” — {PO.OUTCOMES[outcome].lower()}{why}."
                done.append({"type": "stop", "ok": True, "label": "Run stopped" + why})
            elif a["type"] == "signoff" and not signoff:
                people = _people_for(rt, a.get("users"), a.get("emails"), a.get("to_runner") or not (a.get("users") or a.get("emails")))
                signoff = {"people": people, "message": rt.render(a.get("message") or "")}
                done.append({"type": "signoff", "ok": True, "label": "Sign-off requested from " + ", ".join(p["name"] for p in people[:3]) + why})
        except Exception as exc:  # noqa: BLE001 - one action failing must not lose the others
            logger.exception("Policy action %s failed on run %s", a["type"], rt.run.pk)
            done.append({"type": a["type"], "ok": False, "label": f"{PO.ACTIONS.get(a['type'], a['type'])} failed: {exc}"[:200]})
    if stop:
        signoff = None
    return done, signoff, stop


def _policy_email(rt, node, a, ev, summary, dry):
    cfg = node["config"]
    to = _recipients(rt, a)
    if a["type"] == "warn" and not to and rt.run.run_as_id and rt.run.run_as.email:
        to = [rt.run.run_as.email.lower()]
    warn = a["type"] == "warn"
    title = cfg.get("title") or node["label"]
    subject = (f"Policy warning: {D.stem(rt.doc_name)} — {title}" if warn
               else f"Policy check: {title} — {PO.OUTCOMES[ev['outcome']]} · {D.stem(rt.doc_name)}")
    intro = rt.render(a.get("message") or "") or (
        f"The agent “{rt.run.flow_name}” checked this document against “{title}”"
        + (" and found problems that need your attention before it goes further." if warn else "."))
    note = ""
    if warn and a.get("note") and rt.run.envelope_id and rt.run.envelope.created_by_id == rt.run.run_as_id and not dry:
        from .models_esign import EnvelopeComment

        EnvelopeComment.objects.create(
            envelope=rt.run.envelope, author_name=f"Agent · {rt.run.flow_name}"[:150], is_internal=a["note"] != "everyone",
            text=f"Policy warning — {title}\n\n{summary}\n\n— AI-generated by “{rt.run.flow_name}” ({rt.run.reference}).",
            revision=getattr(rt.run.envelope, "revision", 1))
        note = " and noted on the envelope"
    if not to:
        return {"type": a["type"], "ok": False, "label": f"{'Warning' if warn else 'Information'} not sent: nobody to send it to"}
    if dry:
        return {"type": a["type"], "ok": True, "dry": True, "to": to,
                "label": f"Dry run — would have {'warned' if warn else 'informed'} {', '.join(to[:4])}"}
    _send(subject, to, "accounts/esign/email/agent_message.html", {
        "headline": "Policy warning" if warn else "Policy check", "tone": "danger" if warn else "",
        "subject_line": subject, "sections": [("", intro), ("What the agent found", summary)],
        "reference": rt.run.reference, "document_name": rt.doc_name,
        "envelope": rt.run.envelope if rt.run.envelope_id else None, "run_link": run_url(rt.run),
        "provider": A.info()["label"],
    })
    rt.ctx.setdefault("emails", []).append({"node": node["id"], "to": to, "subject": subject})
    return {"type": a["type"], "ok": True, "to": to,
            "label": f"{'Warned' if warn else 'Informed'} {', '.join(to[:4])}{'…' if len(to) > 4 else ''}{note}"}


def _policy_start_agent(rt, a, dry, why):
    from .models_esign_agents import AgentFlow

    run = rt.run
    other = AgentFlow.objects.filter(pk=a.get("id"), is_active=True).first()
    if not other or not run.run_as_id or not can_use_flow(run.run_as, other):
        return {"type": "agent", "ok": False, "label": "The agent to start is not available" + why}
    chain = list((run.context or {}).get("chain") or []) + [run.flow_id]
    if other.pk in chain or len(chain) > 3:
        return {"type": "agent", "ok": False, "label": f"Not started “{other.name}”: agents may not start each other in a loop" + why}
    if dry:
        return {"type": "agent", "ok": True, "dry": True, "label": f"Dry run — would have started the agent “{other.name}”" + why}
    raw = rt.raw() or None
    child = start_run(other, run.run_as, agency=run.agency, trigger=run.trigger, document=raw,
                      document_name=run.document_name, text=run.input_text, envelope=run.envelope,
                      dedupe_key=f"chain:{run.pk}:{other.pk}", dispatch_now=False)
    child.context = {"chain": chain, "parent": run.reference}
    child.save(update_fields=["context"])
    dispatch(child)
    return {"type": "agent", "ok": True, "label": f"Started the agent “{other.name}” ({child.reference})" + why,
            "url": reverse("accounts:esign_agent_run", args=[child.pk]), "reference": child.reference}


def _policy_start_workflow(rt, node, a, ev, summary, dry, why):
    from . import workflow_engine_esign as WE
    from .models_esign_studio import DocumentWorkflow
    from .studio_common_esign import can_use_template

    run = rt.run
    wf = DocumentWorkflow.objects.filter(pk=a.get("id"), is_active=True).first()
    if not wf or not run.run_as_id or not can_use_template(run.run_as, wf):
        return {"type": "workflow", "ok": False, "label": "The workflow to start is not available" + why}
    if dry:
        return {"type": "workflow", "ok": True, "dry": True, "label": f"Dry run — would have started the workflow “{wf.name}”" + why}
    people = _people_for(rt, a.get("users"), a.get("emails"), False) or _people_for(rt, [], [], True)
    chosen = WE.people_from_payload([{"user_id": p["user_id"]} if p.get("user_id") else {"email": p["email"]} for p in people])
    # Every "chosen when the flow starts" role goes to these people (or to the person the agent runs for).
    slots = {s["key"]: chosen for s in WE.chosen_slots(WE.clean_graph(wf.graph))}
    pdf = rt.pdf_bytes()
    title = node["config"].get("title") or node["label"]
    try:
        wrun = WE.start_run(wf, run.run_as, agency=run.agency,
                            subject=f"{D.stem(rt.doc_name)} — {title}: {PO.OUTCOMES[ev['outcome']]}"[:200],
                            message=summary[:2000], pdf_bytes=pdf, document_name=f"{D.stem(rt.doc_name)}.pdf", slots=slots)
    except WE.WorkflowError as exc:
        return {"type": "workflow", "ok": False, "label": f"“{wf.name}” didn't start: {exc}"[:200]}
    return {"type": "workflow", "ok": True, "label": f"Started the workflow “{wf.name}” ({wrun.reference})" + why,
            "url": reverse("accounts:esign_run_detail", args=[wrun.pk]), "reference": wrun.reference}


def _exec_end(rt, node, step):
    return Result(ports=[], message="Finished this path.")


EXECUTORS = {
    "start": _exec_start, "link": _exec_link, "summarize": _exec_summarize, "extract": _exec_extract,
    "classify": _exec_classify, "check": _exec_check, "translate": _exec_translate, "redact": _exec_redact,
    "ask": _exec_ask, "convert": _exec_convert, "cover": _exec_cover, "save": _exec_save,
    "condition": _exec_condition, "approval": _exec_approval, "email": _exec_email, "envnote": _exec_envnote,
    "webhook": _exec_webhook, "end": _exec_end, "policy": _exec_policy,
}


# ─────────────────────────────────────────────────────────────────────────────
# Human check decisions, cancelling, re-running
# ─────────────────────────────────────────────────────────────────────────────

def decide(step, approve: bool, *, user=None, name="", comment="", edited_text=None):
    from .models_esign_agents import AgentRun, AgentStep

    with transaction.atomic():
        step = AgentStep.objects.select_for_update().select_related("run").get(pk=step.pk)
        run = step.run
        if step.status != AgentStep.STATUS_WAITING or run.status != AgentRun.STATUS_WAITING:
            raise AgentError("This check has already been decided.")
        node = run.node(step.node_id) or {}
        cfg = node.get("config") or {}
        port = "approved" if approve else "rejected"
        is_policy = node.get("type") == "policy"
        if is_policy:
            port = (step.output_data or {}).get("pending_port") or "breach"
        if approve and not is_policy and edited_text is not None and cfg.get("allow_edit", True) and edited_text.strip() != step.output_text.strip():
            step.output_data = {**(step.output_data or {}), "edited": True}
            step.output_text = edited_text[:200000]
        step.status = AgentStep.STATUS_DONE
        step.port = port
        step.decided_by = user if getattr(user, "is_authenticated", False) else None
        step.decided_name = (name or (user.get_full_name() or user.username if step.decided_by else ""))[:150]
        step.decision_comment = (comment or "")[:4000]
        step.decided_at = step.finished_at = timezone.now()
        if is_policy:
            step.port = port if approve else ""
        step.message = (f"{'Signed off' if is_policy and approve else 'Approved' if approve else 'Rejected'} by {step.decided_name or 'a reviewer'}"
                        + (" (text edited)" if (step.output_data or {}).get("edited") else "")
                        + (f": {comment[:200]}" if comment else ""))[:400]
        step.save()

        ctx = dict(run.context or {})
        ctx.setdefault("outputs", {})[step.node_id] = {
            "label": node.get("label", step.node_label), "type": "policy" if is_policy else "approval",
            "text": step.output_text[:60000] if (approve or is_policy) else (comment or "Rejected"),
            "data": {"decision": port, "by": step.decided_name, "comment": comment}, "step": step.pk}
        if is_policy and not approve:
            ctx["stop"] = (f"Stopped: {step.decided_name or 'the reviewer'} did not sign off "
                           f"“{cfg.get('title') or step.node_label}”" + (f" — {comment[:200]}" if comment else "") + ".")
        active, done = set(ctx.get("active") or []), set(ctx.get("done") or [])
        for e in (run.graph or {}).get("edges") or []:
            if e["from"] == step.node_id and e["port"] == port and (approve or not is_policy):
                active.add(e["to"])
                ctx.setdefault("via", {})[e["to"]] = step.node_id
        done.add(step.node_id)
        ctx["active"], ctx["done"] = sorted(active), sorted(done)
        ctx.pop("waiting", None)
        run.context = ctx
        run.status = AgentRun.STATUS_QUEUED
        run.save(update_fields=["context", "status", "updated_at"])
    dispatch(run)
    return step


def cancel(run, user=None):
    from .models_esign_agents import AgentRun, AgentStep

    if not run.is_open:
        raise AgentError("This run has already finished.")
    run.status = AgentRun.STATUS_CANCELLED
    run.finished_at = timezone.now()
    who = (user.get_full_name() or user.username) if user else "someone"
    run.error = f"Cancelled by {who}."
    run.save(update_fields=["status", "finished_at", "error", "updated_at"])
    run.steps.filter(status__in=(AgentStep.STATUS_WAITING, AgentStep.STATUS_RUNNING)).update(
        status=AgentStep.STATUS_SKIPPED, message="Cancelled.", finished_at=timezone.now())


def rerun(run, user):
    """Same flow (as it is now), same input."""
    from .models_esign_agents import AgentFlow

    flow = run.flow
    if not flow or not flow.is_active:
        raise AgentError("The flow this run used no longer exists.")
    raw, name = None, run.document_name
    if run.document:
        run.document.open("rb")
        try:
            raw = run.document.read()
        finally:
            run.document.close()
    return start_run(flow, user, agency=run.agency, trigger=AgentFlow.TRIGGER_MANUAL if not run.envelope_id else run.trigger,
                     document=raw, document_name=name, text=run.input_text, envelope=run.envelope, is_test=run.is_test)


# ─────────────────────────────────────────────────────────────────────────────
# Envelope triggers
# ─────────────────────────────────────────────────────────────────────────────

def _feature_on(user):
    try:
        from tenancy.services import has_feature

        return has_feature(user, "esign") and has_feature(user, "esign_agents")
    except Exception:  # noqa: BLE001 - a tenancy hiccup must not start runs it shouldn't
        logger.exception("Agentic flows: feature check failed")
        return False


def flows_for(user, trigger):
    """Enabled automatic flows that should run for this person on this trigger."""
    from .models_esign_agents import AgentFlow

    own = list(AgentFlow.objects.filter(created_by=user, trigger=trigger, is_enabled=True, is_active=True))
    subscribed = AgentFlow.objects.filter(subscriptions__user=user, subscriptions__is_active=True, trigger=trigger,
                                          is_enabled=True, is_active=True).exclude(created_by=user)
    return own + [f for f in subscribed if can_use_flow(user, f)]


def _envelope_pdf(envelope):
    from .pdf_tools_esign import merge

    parts = []
    for doc in envelope.documents.all():
        handle = doc.converted_pdf or doc.file
        if not handle:
            continue
        try:
            handle.open("rb")
            raw = handle.read()
        finally:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        if raw[:5] == b"%PDF-":
            parts.append(raw)
    if envelope.completed_pdf and envelope.status == "completed":
        envelope.completed_pdf.open("rb")
        try:
            done = envelope.completed_pdf.read()
        finally:
            envelope.completed_pdf.close()
        if done[:5] == b"%PDF-":
            return done
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else merge(parts)


def on_envelope_status(envelope_id, old, new):
    """Called after commit when an envelope is sent or completed. Never raises."""
    from django.contrib.auth import get_user_model

    from .models_esign import Envelope
    from .models_esign_agents import AgentRun

    try:
        env = Envelope.objects.select_related("created_by", "agency").get(pk=envelope_id)
    except Envelope.DoesNotExist:
        return []
    jobs = []
    if new == "sent" and old != "sent":
        if env.created_by_id:
            jobs.append(("envelope_sent", env.created_by))
        User = get_user_model()
        for r in env.signers():
            u = r.user if getattr(r, "user_id", None) else (User.objects.filter(email__iexact=r.email, is_active=True).first() if r.email else None)
            if u and u.pk != env.created_by_id:
                jobs.append(("envelope_received", u))
    elif new == "completed" and old != "completed" and env.created_by_id:
        jobs.append(("envelope_completed", env.created_by))
    if not jobs:
        return []

    started, pdf = [], None
    for trigger, user in jobs:
        try:
            if not _feature_on(user):
                continue
            for flow in flows_for(user, trigger):
                key = f"{flow.pk}:{env.pk}:r{getattr(env, 'revision', 1)}:{trigger}:{user.pk}"
                if AgentRun.objects.filter(dedupe_key=key).exists():
                    continue
                if pdf is None:
                    pdf = _envelope_pdf(env) or b""
                try:
                    run = start_run(flow, user, agency=env.agency, trigger=trigger, document=pdf or None,
                                    document_name=f"{env.subject[:150]}.pdf", envelope=env, dedupe_key=key,
                                    text="" if pdf else (env.message or env.subject))
                    started.append(run)
                except AgentError as exc:
                    logger.warning("Agentic flow %s not started for envelope %s: %s", flow.pk, env.pk, exc)
        except Exception:  # noqa: BLE001
            logger.exception("Agentic flows: trigger %s failed for envelope %s", trigger, envelope_id)
    return started


# ─────────────────────────────────────────────────────────────────────────────
# Emails
# ─────────────────────────────────────────────────────────────────────────────

def _send(subject, to, template, ctx, attachments=None):
    """Synchronous on purpose: runs already happen off the request."""
    from .asset_email import _send_email

    subject = re.sub(r"\s+", " ", str(subject or "")).strip()[:200] or "UN PASS agent"
    try:
        _send_email(subject=subject, to_emails=to, html_template=template,
                    context={"brand": getattr(settings, "ESIGN_BRAND", "NES"), "now": timezone.now(), "subject": subject, **ctx},
                    attachments=attachments)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Agentic flows: could not send '%s' to %s", subject, to)
        return False


def _notify_check(run, step, node):
    if (run.context or {}).get("dry_run"):
        return                       # a dry run decides on its own run page
    for p in step.assignees or []:
        link = check_url(step, p.get("email", ""))
        _send(f"Please check: {run.flow_name} — {run.subject}"[:200], [p["email"]], "accounts/esign/email/agent_message.html", {
            "headline": "A policy sign-off is needed" if node.get("type") == "policy" else "An agent needs your check",
            "subject_line": node.get("label") or "Human check",
            "recipient_name": p.get("name"),
            "sections": ([("", (step.output_data or {}).get("signoff_message") or
                           "A document was checked against a policy and needs your sign-off before the agent carries on. "
                           "Sign it off to accept it as it is, or decline to stop the agent."),
                          ("What the agent found", (step.output_text or "")[:6000])]
                         if node.get("type") == "policy" else
                         [("", (node.get("config") or {}).get("context") or
                           "Please review what the agent produced. Approve it to let the flow carry on, edit it first if "
                           "something is wrong, or reject it."),
                          ("What the agent produced", (step.output_text or "")[:6000])]),
            "reference": run.reference, "document_name": run.subject,
            "action_url": link, "action_label": "Review and decide",
            "due": step.due_at, "provider": A.info()["label"],
        })


def _notify_failure(run):
    if not run.run_as_id or not run.run_as.email:
        return
    _send(f"Agent didn't finish: {run.flow_name}"[:200], [run.run_as.email], "accounts/esign/email/agent_message.html", {
        "headline": "An agent run stopped", "subject_line": run.subject, "tone": "danger",
        "sections": [("What went wrong", run.error)], "reference": run.reference,
        "document_name": run.subject, "run_link": run_url(run), "action_label": "Open the run",
    })


def notify_shared(flow, by, people):
    link = _site_url(reverse("accounts:esign_agents") + "?tab=shared")
    for p in people:
        if not p.get("email"):
            continue
        _send(f"{by.get_full_name() or by.username} shared an agent with you: {flow.name}"[:200], [p["email"]],
              "accounts/esign/email/agent_message.html", {
                  "headline": "An agent was shared with you", "subject_line": flow.name,
                  "recipient_name": p.get("name"),
                  "sections": [("", (flow.description or "") + f"\n\nIt starts: {flow.trigger_label.lower()}. "
                                + ("You can run it on your own documents, or switch it on so it runs automatically for you."
                                   if flow.is_automatic else "You can run it on your own documents."))],
                  "reference": "", "action_url": link, "action_label": "Open Agentic flows",
              })


# ─────────────────────────────────────────────────────────────────────────────
# "Describe it and I'll build it"
# ─────────────────────────────────────────────────────────────────────────────

COMPOSE_TYPES = [t for t in CARD_TYPES if t not in ("start", "note", "end")]


def compose(description: str):
    """Plain English -> a graph. AI when available, the built-in parser otherwise."""
    description = (description or "").strip()[:3000]
    if not description:
        raise AgentError("Describe what the agent should do.")
    plan, how = None, "built-in"
    if not A.is_offline():
        try:
            guide = {
                "summarize": "config: length brief|standard|detailed, style brief|bullets|paragraph|action",
                "extract": "config: fields [list of names]",
                "classify": "config: categories [{id, label, hint}] (2-6)",
                "check": "context: the rules, if no link card",
                "translate": "config: language",
                "redact": "config: kinds subset of emails,phones,ids,banking,names",
                "ask": "context: the instruction",
                "link": "config: urls [https://...]",
                "convert": "config: target pdf|docx|xlsx|csv|txt|md|html, source document|previous",
                "cover": "", "save": "",
                "condition": "config: mode ai; context: the yes/no question",
                "approval": "config: emails [..] (who checks), include_runner true|false",
                "email": "config: emails [..], to_runner, to_sender, to_signers (booleans), subject",
                "envnote": "config: visibility internal|everyone",
                "webhook": "config: format teams|slack|json (leave url empty)",
                "policy": "an uploaded policy rulebook — leave rules empty, the person uploads the policy file. "
                          "config: title; outcomes {warning|breach: {actions: [{type: inform|warn|signoff|stop, to_runner: true}]}}. "
                          "Exits: na, complies, warning, breach",
            }
            res = A.complete(
                "You design automation flows for the UN PASS document platform.",
                "Turn this request into an ordered list of cards.\n"
                f"Request: {description}\n\n"
                f"Triggers: {json.dumps(TRIGGERS)}\n"
                f"Card types and settings: {json.dumps(guide)}\n"
                "Branching cards (classify, check, condition, approval) continue on their first exit "
                "(first category / pass / yes / approved / complies) unless the next card says \"via\": \"issues\" | \"no\" | "
                "\"rejected\" | \"other\" | \"breach\" | \"warning\". A card's config.source may be \"@<type>\" to read the latest earlier card of that type.\n"
                'Reply as {"trigger": "<key>", "name": "<short flow name>", "cards": [{"type": "...", "label": "<2-4 words>", '
                '"context": "<instructions for the card, may be empty>", "config": {...}}]}',
                json_expected=True, max_tokens=2000)
            if isinstance(res.data.get("cards"), list) and res.data["cards"]:
                plan, how = res.data, "ai"
        except A.AIError as exc:
            logger.info("Agent compose fell back to the built-in parser: %s", exc)
    if plan is None:
        plan = O.compose(description)
    return graph_from_plan(plan, description), how, str(plan.get("name") or "")[:150]


def graph_from_plan(plan, description=""):
    trigger = plan.get("trigger") if plan.get("trigger") in TRIGGERS else "manual"
    nodes = [{"id": "n_start", "type": "start", "label": "Trigger", "x": 40, "y": 200,
              "config": {"trigger": trigger, "context": "", "language": "", "mask_pii": False}}]
    edges, prev, x = [], nodes[0], 40
    for i, card in enumerate(list(plan.get("cards") or [])[:20]):
        t = card.get("type")
        if t not in COMPOSE_TYPES:
            continue
        x += 260
        cfg = dict(card.get("config") or {})
        cfg["context"] = str(card.get("context") or cfg.get("context") or "")
        if t == "classify":
            cfg["categories"] = [c if isinstance(c, dict) else {"label": str(c)} for c in cfg.get("categories") or []]
            for j, c in enumerate(cfg["categories"]):
                c.setdefault("id", f"c{j + 1}")
        for key in ("source", "subject"):
            want = str(cfg.get(key) or "")
            if want.startswith("@"):            # "@summarize" -> the latest Summarize card before this one
                match = [n for n in nodes if n["type"] == want[1:]]
                cfg[key] = match[-1]["id"] if match else "previous"
        node = {"id": f"n{i + 1}", "type": t, "label": str(card.get("label") or CARD_TYPES[t]["label"])[:60],
                "x": x, "y": 200, "config": cfg}
        nodes.append(node)
        tmp = clean_graph({"nodes": [prev, node], "edges": []})
        exits = ports_for(tmp["nodes"][0])
        if exits and card.get("via") in exits:
            edges.append({"id": f"e{i + 1}", "from": prev["id"], "port": card["via"], "to": node["id"]})
        elif prev["type"] == "policy":
            # Carry on from every outcome that doesn't stop the run.
            outs = (tmp["nodes"][0]["config"].get("outcomes") or {})
            for port in exits:
                if not any(a.get("type") == "stop" for a in (outs.get(port) or {}).get("actions") or []):
                    edges.append({"id": f"e{i + 1}{port}", "from": prev["id"], "port": port, "to": node["id"]})
        elif exits:
            edges.append({"id": f"e{i + 1}", "from": prev["id"], "port": exits[0], "to": node["id"]})
        prev = node
    if description:
        nodes.append({"id": "n_note", "type": "note", "label": "What this agent does", "x": 40, "y": 340,
                      "config": {"context": description[:1500], "color": NOTE_COLORS[0]}})
    return clean_graph({"nodes": nodes, "edges": edges})
