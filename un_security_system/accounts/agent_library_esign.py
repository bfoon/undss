# accounts/agent_library_esign.py
"""UN PASS — Agentic Flows: ready-made agents to start from."""

from __future__ import annotations

import copy


def _n(i, t, label, x, y, **config):
    return {"id": i, "type": t, "label": label, "x": x, "y": y, "config": config}


def _e(i, a, port, b):
    return {"id": i, "from": a, "port": port, "to": b}


AGENT_TEMPLATES = [
    {
        "key": "blank", "name": "Blank agent", "icon": "bi-plus-square-dotted",
        "description": "Just a Trigger. Drag cards on and connect them.",
        "graph": {"nodes": [_n("s", "start", "Trigger", 60, 200, trigger="manual", context="", language="")], "edges": []},
    },
    {
        "key": "incoming_summary", "name": "Summarize documents that come to me for signature", "icon": "bi-inbox",
        "description": "Every time someone sends you a document to sign, get an executive brief by email first.",
        "graph": {
            "nodes": [
                _n("s", "start", "Trigger", 40, 200, trigger="envelope_received",
                   context="I am a UN staff member. Point out anything that commits our office to money, dates or obligations."),
                _n("sum", "summarize", "Executive brief", 300, 200, length="standard", style="action",
                   context="Focus on what I am agreeing to by signing, the amounts, the dates, and anything unusual."),
                _n("mail", "email", "Email me the brief", 560, 200, to_runner=True, include="previous",
                   subject="Before you sign: {{subject}}",
                   context="Here is a summary of the document waiting for your signature."),
                _n("note", "note", "How it works", 40, 340, color="#FEF3C7",
                   context="Runs for every envelope where you are a signer. Switch it on from the Agentic flows page."),
            ],
            "edges": [_e("e1", "s", "next", "sum"), _e("e2", "sum", "next", "mail")],
        },
    },
    {
        "key": "policy_gate", "name": "Check documents against a policy before signing", "icon": "bi-shield-check",
        "description": "Reads your policy page, checks each incoming document against it, and tells you what to look at.",
        "graph": {
            "nodes": [
                _n("s", "start", "Trigger", 40, 200, trigger="envelope_received", context=""),
                _n("link", "link", "Read the policy", 300, 200, urls=["https://"],
                   context="This is the policy that applies to the documents I sign."),
                _n("chk", "check", "Policy check", 560, 200, strictness="standard",
                   context="Pay special attention to approval thresholds, payment terms and contract duration."),
                _n("bad", "email", "Email me the issues", 820, 120, to_runner=True, include="previous",
                   subject="Check before signing: {{subject}}",
                   context="The agent found points in this document that may not follow the policy."),
                _n("ok", "email", "Email me: looks fine", 820, 300, to_runner=True, include="previous",
                   subject="Policy check passed: {{subject}}"),
            ],
            "edges": [_e("e1", "s", "next", "link"), _e("e2", "link", "next", "chk"),
                      _e("e3", "chk", "issues", "bad"), _e("e4", "chk", "pass", "ok")],
        },
    },
    {
        "key": "contract_intake", "name": "Contract intake with a human check", "icon": "bi-briefcase",
        "description": "Extract the key terms to Excel, ask the AI if the value is above the threshold, and get Operations to confirm.",
        "graph": {
            "nodes": [
                _n("s", "start", "Trigger", 40, 220, trigger="manual", context=""),
                _n("ext", "extract", "Key contract terms", 300, 220,
                   fields=["Vendor", "Contract value", "Currency", "Start date", "End date", "Payment terms", "Signatories"]),
                _n("xls", "convert", "Terms to Excel", 560, 120, target="xlsx", source="previous", filename="{{document}} - terms"),
                _n("big", "condition", "Above 50,000 USD?", 560, 320, mode="ai",
                   context="Is the total contract value more than 50,000 USD (or the equivalent)?"),
                _n("hc", "approval", "Operations check", 820, 260, include_runner=True, allow_edit=True, due_days=2,
                   context="Confirm the extracted terms are right before procurement is told."),
                _n("mail", "email", "Tell procurement", 1080, 220, to_runner=True, include="all", attach_files=True,
                   subject="New contract: {{document}}"),
            ],
            "edges": [_e("e1", "s", "next", "ext"), _e("e2", "ext", "next", "xls"), _e("e3", "ext", "next", "big"),
                      _e("e4", "big", "yes", "hc"), _e("e5", "big", "no", "mail"), _e("e6", "hc", "approved", "mail")],
        },
    },
    {
        "key": "policy_guard", "name": "Policy guard for documents I'm asked to sign", "icon": "bi-journal-check",
        "description": "A built-in sample rulebook checks every document that comes for your signature. Complies: you "
                       "get a note. Warning: you're informed. Breach: you're warned and asked to sign off before anything else.",
        "graph": {
            "nodes": [
                _n("s", "start", "Trigger", 40, 240, trigger="envelope_received",
                   context="Be precise and quote the document when you flag something."),
                _n("pol", "policy", "Procurement essentials", 320, 220, source="document",
                   title="Procurement essentials (sample — replace with your policy)", version="v1",
                   applies_to="Contracts, purchase orders and agreements with vendors",
                   applies_keywords=["contract", "agreement", "purchase order", "vendor", "contractor"],
                   summary="A short sample rulebook. Open the Policy studio and upload your own policy (Word, PDF or text) to replace it.",
                   rules=[
                       {"id": "r1", "title": "Contracts above USD 50,000 need Contracts Committee review", "severity": "must",
                        "kind": "threshold", "op": "gt", "value": 50000, "unit": "USD", "ref": "4.1",
                        "text": "Any contract with a total value above USD 50,000 must be reviewed by the Contracts Committee before signature."},
                       {"id": "r2", "title": "A performance guarantee is required", "severity": "must", "kind": "requires",
                        "terms": ["performance guarantee/performance security/performance bond"], "ref": "5.3",
                        "text": "Contracts for goods and works must include a performance guarantee."},
                       {"id": "r3", "title": "No advance payments", "severity": "must", "kind": "forbids",
                        "terms": ["advance payment/payment in advance/prepayment"], "ref": "6.2",
                        "text": "Advance payments are not allowed without written Treasury approval."},
                       {"id": "r4", "title": "Termination conditions should be stated", "severity": "should", "kind": "requires",
                        "terms": ["terminate/termination"], "ref": "7.1",
                        "text": "Contracts should state the conditions under which either party may terminate."},
                   ],
                   outcomes={
                       "complies": {"actions": [{"type": "inform", "to_runner": True,
                                                 "message": "Good news: this document follows the procurement essentials."}]},
                       "warning": {"actions": [{"type": "inform", "to_runner": True}]},
                       "breach": {"actions": [{"type": "warn", "to_runner": True, "note": "internal"},
                                              {"type": "signoff", "to_runner": True,
                                               "message": "Please confirm you have read the findings before you sign."}]},
                   }),
                _n("sum", "summarize", "Brief for the signer", 640, 340, style="action", length="brief", source="document"),
                _n("mail", "email", "Email me the brief", 900, 340, to_runner=True, include="previous",
                   subject="Before you sign: {{subject}}"),
                _n("note", "note", "How it works", 320, 470, color="#FDE68A",
                   context="The Policy card holds the rules — no file needed. Breach → you're warned and asked to sign "
                           "off; once you do, the brief is emailed. Warning and Complies just inform you."),
            ],
            "edges": [_e("e1", "s", "next", "pol"), _e("e2", "pol", "breach", "sum"), _e("e3", "pol", "warning", "sum"),
                      _e("e4", "pol", "complies", "sum"), _e("e5", "sum", "next", "mail")],
        },
    },
    {
        "key": "translate_convert", "name": "Translate and send as Word", "icon": "bi-translate",
        "description": "Translate a document into French, turn it into a Word file and email it.",
        "graph": {
            "nodes": [
                _n("s", "start", "Trigger", 40, 200, trigger="manual", context="Use formal UN register."),
                _n("tr", "translate", "Translate to French", 300, 200, language="French"),
                _n("doc", "convert", "Make a Word file", 560, 200, target="docx", source="previous",
                   filename="{{document}} (FR)"),
                _n("mail", "email", "Email the translation", 820, 200, to_runner=True, include="none", attach_files=True,
                   subject="French version: {{document}}", context="The French translation is attached as a Word document."),
            ],
            "edges": [_e("e1", "s", "next", "tr"), _e("e2", "tr", "next", "doc"), _e("e3", "doc", "next", "mail")],
        },
    },
    {
        "key": "sent_cover", "name": "Add a summary for my signers", "icon": "bi-file-earmark-richtext",
        "description": "When you send a document, the agent posts a plain-language summary on the envelope for every signer.",
        "graph": {
            "nodes": [
                _n("s", "start", "Trigger", 40, 200, trigger="envelope_sent", context=""),
                _n("sum", "summarize", "Summary for signers", 300, 200, length="brief", style="bullets",
                   context="Write it for the people who will sign: what it is and what it commits them to."),
                _n("note", "envnote", "Post on the envelope", 560, 200, visibility="everyone",
                   context="Summary prepared by the sender's assistant:"),
            ],
            "edges": [_e("e1", "s", "next", "sum"), _e("e2", "sum", "next", "note")],
        },
    },
    {
        "key": "triage", "name": "Sort and route incoming documents", "icon": "bi-tags",
        "description": "Classify each document and send it to the right team, with personal data masked first.",
        "graph": {
            "nodes": [
                _n("s", "start", "Trigger", 40, 240, trigger="manual", context=""),
                _n("mask", "redact", "Mask personal data", 300, 240, kinds=["emails", "phones", "ids", "banking"]),
                _n("sum", "summarize", "Short summary", 560, 240, length="brief", style="bullets"),
                _n("cls", "classify", "What kind of document?", 820, 240, source="mask", categories=[
                    {"id": "hr", "label": "HR", "hint": "contracts of staff, leave, recruitment"},
                    {"id": "proc", "label": "Procurement", "hint": "purchase orders, bids, vendor contracts"},
                    {"id": "fin", "label": "Finance", "hint": "invoices, payments, budgets"}]),
                _n("hr", "email", "To HR", 1120, 60, include="previous", attach_document=True, subject="[HR] {{document}}"),
                _n("pr", "email", "To Procurement", 1120, 180, include="previous", attach_document=True,
                   subject="[Procurement] {{document}}"),
                _n("fi", "email", "To Finance", 1120, 300, include="previous", attach_document=True,
                   subject="[Finance] {{document}}"),
                _n("me", "email", "Couldn't sort: tell me", 1120, 420, to_runner=True, include="all",
                   subject="Please route by hand: {{document}}"),
                _n("note", "note", "Before you use it", 300, 400, color="#DBEAFE",
                   context="Choose the people for HR, Procurement and Finance — click each Email card."),
            ],
            "edges": [_e("e1", "s", "next", "mask"), _e("e2", "mask", "next", "sum"), _e("e3", "sum", "next", "cls"),
                      _e("e4", "cls", "hr", "hr"), _e("e5", "cls", "proc", "pr"), _e("e6", "cls", "fin", "fi"),
                      _e("e7", "cls", "other", "me")],
        },
    },
]


def agent_template(key):
    for t in AGENT_TEMPLATES:
        if t["key"] == key:
            return copy.deepcopy(t)
    return copy.deepcopy(AGENT_TEMPLATES[0])
