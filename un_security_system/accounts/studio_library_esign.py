# accounts/studio_library_esign.py
"""
UN PASS — eSign Studio starter library.

Ready-made forms and flows people can start from instead of a blank canvas.
Everything here is plain data run through the same cleaning as the designers,
so a template can never produce something the designer could not.

Add your own by appending to FORM_TEMPLATES / FLOW_TEMPLATES.
"""

from copy import deepcopy

UN_BLUE = "#009EDB"
UN_DARK = "#005A8B"


def _h(eid, text, bg="#EAF6FC", color=UN_DARK, size="md"):
    return {"id": eid, "type": "heading", "text": text, "width": 12,
            "style": {"size": size, "color": color, "bg": bg, "align": "left"}}


def _f(eid, type_, label, key, width=6, required=False, **extra):
    el = {"id": eid, "type": type_, "label": label, "key": key, "width": width,
          "required": required, "fill_by": "submitter"}
    el.update(extra)
    return el


def _p(eid, text, color="#475569", size="sm", bg=""):
    return {"id": eid, "type": "paragraph", "text": text, "width": 12,
            "style": {"color": color, "size": size, "bg": bg, "align": "left"}}


def _sig(eid, label, role, width=6):
    return {"id": eid, "type": "signature", "label": label, "role": role,
            "step": "", "width": width, "show_date": True}


def _theme(accent=UN_BLUE, header=UN_DARK, font="helvetica"):
    return {"accent": accent, "header_bg": header, "header_text": "#FFFFFF",
            "label_color": "#334155", "font": font, "density": "comfortable", "rounded": True}


_REQUESTER = [
    _h("req_h", "Requester"),
    _f("req_name", "text", "Full name", "full_name", 6, True, prefill="user.full_name"),
    _f("req_email", "email", "Email", "email", 6, True, prefill="user.email"),
    _f("req_title", "text", "Job title", "job_title", 6, prefill="user.job_title"),
    _f("req_agency", "text", "Agency / unit", "agency", 6, prefill="user.agency"),
]


FORM_TEMPLATES = [
    {
        "key": "blank",
        "name": "Blank form",
        "category": "General",
        "icon": "bi-file-earmark",
        "description": "Start from an empty page.",
        "schema": {
            "theme": _theme(),
            "header": {"title": "Untitled form", "subtitle": "", "align": "left", "show_reference": True},
            "elements": [
                _f("name", "text", "Full name", "full_name", 6, True, prefill="user.full_name"),
                _f("date", "date", "Date", "date", 6, prefill="today"),
            ],
        },
    },
    {
        "key": "ict_access",
        "name": "ICT access request",
        "category": "ICT",
        "icon": "bi-shield-lock",
        "description": "Accounts, VPN, shared drives and system roles, with supervisor sign-off.",
        "schema": {
            "theme": _theme(),
            "header": {"title": "ICT Access Request", "subtitle": "Complete all sections. ICT will not process incomplete requests.",
                       "align": "left", "show_reference": True},
            "elements": _REQUESTER + [
                _f("sup_email", "email", "Supervisor email", "supervisor_email", 6, True,
                   help="Your supervisor approves this request."),
                _f("start", "date", "Needed from", "needed_from", 6, True, prefill="today"),
                _h("acc_h", "Access requested"),
                _f("systems", "checkboxes", "Systems", "systems", 12, True,
                   options=["Email account", "VPN", "Shared drive", "Quantum / ERP", "Wi-Fi (staff)", "Printing"]),
                _f("level", "radio", "Access level", "access_level", 6, True,
                   options=["Standard user", "Power user", "Administrator"]),
                _f("duration", "select", "Duration", "duration", 6, True,
                   options=["Permanent", "Until end of contract", "Temporary (30 days)", "Temporary (90 days)"]),
                _f("justification", "textarea", "Business justification", "justification", 12, True, rows=4),
                _h("ict_h", "For ICT use", bg="#F1F5F9", color="#334155", size="sm"),
                _f("ict_user", "text", "Username created", "ict_username", 6),
                _f("ict_ticket", "text", "Ticket number", "ict_ticket", 6),
                _p("policy", "By signing, the requester confirms they have read and accept the ICT Acceptable Use Policy."),
                _sig("sig_req", "Requester", "Requester"),
                _sig("sig_sup", "Supervisor", "Supervisor"),
            ],
        },
    },
    {
        "key": "asset_handover",
        "name": "Asset handover",
        "category": "Assets",
        "icon": "bi-laptop",
        "description": "Equipment issued to a staff member, itemised, signed by both sides.",
        "schema": {
            "theme": _theme("#0E9F6E", "#065F46"),
            "header": {"title": "Asset Handover Form", "subtitle": "Record of equipment issued to a staff member",
                       "align": "left", "show_reference": True},
            "elements": [
                _h("to_h", "Issued to", bg="#ECFDF5", color="#065F46"),
                _f("to_name", "text", "Staff member", "staff_name", 6, True),
                _f("to_index", "text", "Index / staff number", "staff_number", 6),
                _f("to_unit", "text", "Unit", "unit", 6),
                _f("to_date", "date", "Handover date", "handover_date", 6, True, prefill="today"),
                _h("items_h", "Items", bg="#ECFDF5", color="#065F46"),
                _f("items", "table", "Equipment", "items", 12, True, rows=4, show_total=False, allow_add=True,
                   columns=[{"key": "description", "label": "Description", "kind": "text", "width": 4},
                            {"key": "tag", "label": "Asset tag", "kind": "text", "width": 2},
                            {"key": "serial", "label": "Serial number", "kind": "text", "width": 2},
                            {"key": "condition", "label": "Condition", "kind": "text", "width": 2}]),
                _f("accessories", "textarea", "Accessories and notes", "notes", 12, rows=3),
                _p("terms", "The recipient accepts responsibility for the equipment above and will return it "
                            "on request or on separation.", bg="#F8FAFC"),
                _sig("sig_ict", "Issued by", "ICT"),
                _sig("sig_staff", "Received by", "Staff member"),
            ],
        },
    },
    {
        "key": "leave",
        "name": "Leave request",
        "category": "HR",
        "icon": "bi-calendar2-week",
        "description": "Annual, sick or special leave with supervisor approval.",
        "schema": {
            "theme": _theme("#7C3AED", "#4C1D95"),
            "header": {"title": "Leave Request", "subtitle": "", "align": "left", "show_reference": True},
            "elements": _REQUESTER[:3] + [
                _f("sup_email", "email", "Supervisor email", "supervisor_email", 6, True),
                _h("leave_h", "Leave details", bg="#F5F3FF", color="#4C1D95"),
                _f("type", "select", "Type of leave", "leave_type", 6, True,
                   options=["Annual leave", "Sick leave", "Home leave", "Paternity / maternity", "Special leave", "Uncertified sick leave"]),
                _f("days", "number", "Working days", "days", 6, True, min=0.5, max=60, decimals=1),
                _f("from", "date", "From", "date_from", 6, True),
                _f("to", "date", "To (inclusive)", "date_to", 6, True),
                _f("cover", "text", "Colleague covering", "cover", 6),
                _f("contact", "text", "Contact while away", "contact", 6),
                _f("remarks", "textarea", "Remarks", "remarks", 12, rows=3),
                _sig("sig_staff", "Staff member", "Requester"),
                _sig("sig_sup", "Supervisor", "Supervisor"),
            ],
        },
    },
    {
        "key": "travel",
        "name": "Travel request",
        "category": "Operations",
        "icon": "bi-airplane",
        "description": "Mission travel with itinerary, cost estimate and approvals.",
        "schema": {
            "theme": _theme("#EA580C", "#9A3412"),
            "header": {"title": "Travel Request", "subtitle": "Submit at least 14 days before departure.",
                       "align": "left", "show_reference": True},
            "elements": _REQUESTER + [
                _f("purpose", "textarea", "Purpose of travel", "purpose", 12, True, rows=3),
                _h("itin_h", "Itinerary", bg="#FFF7ED", color="#9A3412"),
                _f("itinerary", "table", "Legs", "itinerary", 12, True, rows=2,
                   columns=[{"key": "from", "label": "From", "kind": "text", "width": 2},
                            {"key": "to", "label": "To", "kind": "text", "width": 2},
                            {"key": "date", "label": "Date", "kind": "date", "width": 2},
                            {"key": "mode", "label": "Mode", "kind": "text", "width": 2}]),
                _h("cost_h", "Estimated cost (USD)", bg="#FFF7ED", color="#9A3412"),
                _f("costs", "table", "Costs", "costs", 12, True, rows=3, show_total=True,
                   columns=[{"key": "item", "label": "Item", "kind": "text", "width": 4},
                            {"key": "amount", "label": "Amount", "kind": "number", "width": 2}]),
                _f("amount", "number", "Total estimate (USD)", "amount", 4, True, min=0, decimals=2,
                   help="Used to route large requests to the head of office."),
                _f("budget", "text", "Budget code / project", "budget_code", 4, True),
                _f("sup_email", "email", "Supervisor email", "supervisor_email", 4, True),
                _sig("sig_trav", "Traveller", "Requester", 4),
                _sig("sig_sup", "Supervisor", "Supervisor", 4),
                _sig("sig_head", "Head of office", "Head of office", 4),
            ],
        },
    },
    {
        "key": "purchase",
        "name": "Purchase request",
        "category": "Procurement",
        "icon": "bi-cart3",
        "description": "Goods or services, itemised with a running total.",
        "schema": {
            "theme": _theme("#0891B2", "#164E63"),
            "header": {"title": "Purchase Request", "subtitle": "In line with the procurement rules of your office",
                       "align": "left", "show_reference": True},
            "elements": _REQUESTER[:3] + [
                _f("needed", "date", "Required by", "required_by", 6, True),
                _h("goods_h", "Goods and services", bg="#ECFEFF", color="#164E63"),
                _f("lines", "table", "Line items", "lines", 12, True, rows=4, show_total=True,
                   columns=[{"key": "description", "label": "Description", "kind": "text", "width": 5},
                            {"key": "qty", "label": "Qty", "kind": "number", "width": 1},
                            {"key": "unit", "label": "Unit price", "kind": "number", "width": 2},
                            {"key": "total", "label": "Line total", "kind": "number", "width": 2}]),
                _f("amount", "number", "Total amount (USD)", "amount", 6, True, min=0, decimals=2),
                _f("method", "select", "Procurement method", "method", 6, True,
                   options=["Micro-purchase", "Request for quotation", "Invitation to bid", "LTA call-off"]),
                _f("justification", "textarea", "Justification", "justification", 12, True, rows=3),
                _sig("sig_req", "Requested by", "Requester", 4),
                _sig("sig_budget", "Budget holder", "Budget holder", 4),
                _sig("sig_ops", "Approved by", "Operations", 4),
            ],
        },
    },
    {
        "key": "visitor",
        "name": "Visitor access request",
        "category": "Security",
        "icon": "bi-person-badge",
        "description": "Pre-register visitors for the compound gate.",
        "schema": {
            "theme": _theme("#DC2626", "#7F1D1D"),
            "header": {"title": "Visitor Access Request", "subtitle": "Security clearance for the UN compound",
                       "align": "left", "show_reference": True},
            "elements": _REQUESTER[:2] + [
                _f("date", "date", "Visit date", "visit_date", 6, True),
                _f("time", "text", "Arrival time", "arrival_time", 6, True, placeholder="09:30"),
                _h("vis_h", "Visitors", bg="#FEF2F2", color="#7F1D1D"),
                _f("visitors", "table", "Visitor list", "visitors", 12, True, rows=3,
                   columns=[{"key": "name", "label": "Name", "kind": "text", "width": 3},
                            {"key": "org", "label": "Organisation", "kind": "text", "width": 3},
                            {"key": "id", "label": "ID / passport", "kind": "text", "width": 2}]),
                _f("vehicle", "yesno", "Bringing a vehicle?", "vehicle", 6),
                _f("plate", "text", "Plate number", "plate", 6),
                _f("purpose", "textarea", "Purpose of visit", "purpose", 12, True, rows=2),
                _sig("sig_host", "Host", "Requester"),
                _sig("sig_sec", "Security", "Security"),
            ],
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Flows
# ─────────────────────────────────────────────────────────────────────────────

def _n(nid, type_, x, y, label, **config):
    return {"id": nid, "type": type_, "x": x, "y": y, "label": label, "config": config}


def _e(eid, a, port, b):
    return {"id": eid, "from": a, "port": port, "to": b}


def _chosen(slot):
    return {"mode": "chosen", "slot": slot, "users": [], "emails": [], "field": ""}


INITIATOR = {"mode": "initiator", "slot": "", "users": [], "emails": [], "field": ""}


FLOW_TEMPLATES = [
    {
        "key": "blank",
        "name": "Blank flow",
        "icon": "bi-bounding-box",
        "description": "Just a start and an end. Drag steps in between.",
        "graph": {
            "nodes": [_n("start", "start", 60, 220, "Start"),
                      _n("done", "end", 520, 220, "Completed", outcome="completed")],
            "edges": [_e("e1", "start", "next", "done")],
        },
    },
    {
        "key": "single_approval",
        "name": "Single approval",
        "icon": "bi-patch-check",
        "description": "One person approves or rejects. The initiator is told either way.",
        "graph": {
            "nodes": [
                _n("start", "start", 60, 200, "Start"),
                _n("approve", "approval", 300, 180, "Approval", assign=_chosen("Approver"), rule="all",
                   instructions="Review the document and approve or reject.", due_days=3),
                _n("ok", "end", 600, 120, "Approved", outcome="completed"),
                _n("no", "end", 600, 300, "Rejected", outcome="rejected"),
            ],
            "edges": [_e("e1", "start", "next", "approve"), _e("e2", "approve", "approved", "ok"),
                      _e("e3", "approve", "rejected", "no")],
        },
    },
    {
        "key": "approve_then_sign",
        "name": "Approve, then sign",
        "icon": "bi-pen",
        "description": "A supervisor approves, an authorised signatory signs, the initiator gets the signed PDF.",
        "graph": {
            "nodes": [
                _n("start", "start", 40, 220, "Start"),
                _n("sup", "approval", 250, 200, "Supervisor approval", assign=_chosen("Supervisor"),
                   rule="all", instructions="", due_days=3),
                _n("sign", "signature", 520, 200, "Signature", assign=_chosen("Authorised signatory"),
                   order="sequential", placement="auto", message="Please sign the attached document."),
                _n("tell", "notify", 790, 200, "Tell the initiator", assign=INITIATOR,
                   message="Your document has been approved and signed.", attach_pdf=True),
                _n("done", "end", 1040, 220, "Completed", outcome="completed"),
                _n("rej", "end", 1040, 420, "Rejected", outcome="rejected"),
            ],
            "edges": [_e("e1", "start", "next", "sup"), _e("e2", "sup", "approved", "sign"),
                      _e("e3", "sup", "rejected", "rej"), _e("e4", "sign", "signed", "tell"),
                      _e("e5", "sign", "declined", "rej"), _e("e6", "tell", "next", "done")],
        },
    },
    {
        "key": "threshold",
        "name": "Amount threshold",
        "icon": "bi-diamond",
        "description": "Large amounts need the head of office as well. Built for a form with an amount field.",
        "graph": {
            "nodes": [
                _n("start", "start", 40, 240, "Start"),
                _n("sup", "approval", 230, 220, "Supervisor approval", assign=_chosen("Supervisor"),
                   rule="all", instructions="", due_days=3),
                _n("big", "condition", 480, 220, "Over 5,000?", field="amount", op="gt", value="5000"),
                _n("head", "approval", 720, 100, "Head of office", assign=_chosen("Head of office"),
                   rule="all", instructions="Amount is above the delegated threshold.", due_days=5),
                _n("sign", "signature", 960, 220, "Signature", assign=_chosen("Authorised signatory"),
                   order="sequential", placement="auto", message=""),
                _n("done", "end", 1200, 240, "Completed", outcome="completed"),
                _n("rej", "end", 1200, 430, "Rejected", outcome="rejected"),
            ],
            "edges": [_e("e1", "start", "next", "sup"), _e("e2", "sup", "approved", "big"),
                      _e("e3", "sup", "rejected", "rej"), _e("e4", "big", "yes", "head"),
                      _e("e5", "big", "no", "sign"), _e("e6", "head", "approved", "sign"),
                      _e("e7", "head", "rejected", "rej"), _e("e8", "sign", "signed", "done"),
                      _e("e9", "sign", "declined", "rej")],
        },
    },
    {
        "key": "parallel_review",
        "name": "Parallel review",
        "icon": "bi-diagram-3",
        "description": "Finance and legal review at the same time; signature once both are done.",
        "graph": {
            "nodes": [
                _n("start", "start", 40, 240, "Start"),
                _n("split", "parallel", 230, 240, "Send to both"),
                _n("fin", "review", 420, 120, "Finance review", assign=_chosen("Finance reviewer"),
                   rule="all", instructions="", due_days=3),
                _n("leg", "review", 420, 340, "Legal review", assign=_chosen("Legal reviewer"),
                   rule="all", instructions="", due_days=3),
                _n("join", "join", 680, 240, "Both reviewed"),
                _n("sign", "signature", 880, 220, "Signature", assign=_chosen("Authorised signatory"),
                   order="sequential", placement="auto", message=""),
                _n("done", "end", 1120, 240, "Completed", outcome="completed"),
                _n("rej", "end", 1120, 400, "Declined", outcome="rejected"),
            ],
            "edges": [_e("e1", "start", "next", "split"), _e("e2", "split", "next", "fin"),
                      _e("e3", "split", "next", "leg"), _e("e4", "fin", "next", "join"),
                      _e("e5", "leg", "next", "join"), _e("e6", "join", "next", "sign"),
                      _e("e7", "sign", "signed", "done"), _e("e8", "sign", "declined", "rej")],
        },
    },
    {
        "key": "ict_request",
        "name": "Request, approve, fulfil",
        "icon": "bi-tools",
        "description": "Supervisor approves, ICT completes its section of the form, the requester confirms.",
        "graph": {
            "nodes": [
                _n("start", "start", 40, 240, "Start"),
                _n("sup", "approval", 230, 220, "Supervisor approval",
                   assign={"mode": "field", "field": "supervisor_email", "slot": "", "users": [], "emails": []},
                   rule="all", instructions="", due_days=2),
                _n("ict", "fill", 480, 220, "ICT completes", assign=_chosen("ICT focal point"),
                   rule="any", instructions="Create the account and fill in the ICT section."),
                _n("ack", "review", 730, 220, "Requester confirms", assign=INITIATOR,
                   rule="all", instructions="Confirm you have received access.", due_days=5),
                _n("done", "end", 970, 240, "Completed", outcome="completed"),
                _n("rej", "end", 970, 430, "Rejected", outcome="rejected"),
            ],
            "edges": [_e("e1", "start", "next", "sup"), _e("e2", "sup", "approved", "ict"),
                      _e("e3", "sup", "rejected", "rej"), _e("e4", "ict", "next", "ack"),
                      _e("e5", "ack", "next", "done")],
        },
    },
]


COLUMN_GAP = 270      # node width (200) + room for port labels and connectors


def _columns(graph):
    """
    Place each step one column to the right of the furthest step leading into
    it, keeping the template's vertical positions. Coordinates written by hand
    drift into overlaps; this keeps every starter flow reading left to right
    with the same spacing as the designer's Tidy up.
    """
    nodes = {n["id"]: n for n in graph["nodes"]}
    level = {nid: 0 for nid in nodes}
    for _ in range(len(nodes)):
        changed = False
        for e in graph["edges"]:
            if e["from"] in level and e["to"] in level and level[e["to"]] < level[e["from"]] + 1 <= len(nodes):
                level[e["to"]] = level[e["from"]] + 1
                changed = True
        if not changed:
            break
    last = max(level.values() or [0])
    for nid, n in nodes.items():
        col = last if (n["type"] == "end") else level[nid]
        n["x"] = 40 + col * COLUMN_GAP
    return graph


for _t in FLOW_TEMPLATES:
    _columns(_t["graph"])


def form_template(key):
    for t in FORM_TEMPLATES:
        if t["key"] == key:
            return deepcopy(t)
    return deepcopy(FORM_TEMPLATES[0])


def flow_template(key):
    for t in FLOW_TEMPLATES:
        if t["key"] == key:
            return deepcopy(t)
    return deepcopy(FLOW_TEMPLATES[0])
