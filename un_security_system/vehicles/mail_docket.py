# vehicles/mail_docket.py
"""
The Mail docket: the Flow Studio form that carries a package.

Each agency gets two dockets (incoming and outgoing). They are ordinary
FormTemplates, so the owner can open them in the Form designer, restyle them,
add signature boxes or extra sections — as long as the field keys below stay,
the mailroom keeps filling them in.

This module also builds the two starter flows offered on the Mail flows page.
They are real DocumentWorkflow graphs, opened straight in the Flow Studio
designer so the agency can change them.
"""

from django.db import transaction

# Field keys the mailroom writes. Flow Studio conditions, routes and
# "assign from a form field" read these names.
INCOMING_KEYS = (
    "tracking_code", "logged_at", "item_type", "description",
    "sender_name", "sender_type", "sender_org", "sender_contact", "sender_email",
    "destination_agency", "dest_focal_email", "for_recipient", "notes",
)
OUTGOING_KEYS = (
    "tracking_code", "logged_at", "item_type", "description",
    "sender_name", "sender_org", "sender_contact", "sender_email",
    "for_recipient", "destination_agency", "recipient_org", "recipient_address", "recipient_email",
    "notes", "courier", "courier_reference", "dispatched_on",
)

#: Node ids the starter flows use. The docket's signature box and dispatch
#: section point at them, so a starter flow works on the first run.
RECEIPT_STEP = "sign_receipt"
DISPATCH_STEP = "record_dispatch"

SENDER_TYPE_LABELS = [
    "Government / Law Enforcement", "Private Sector", "NGO", "Non-Profit", "Individual", "Other",
]
COURIERS = ["UN Pouch", "DHL", "FedEx", "Postal service", "Hand delivery", "Other"]


def _field(eid, ftype, key, label, width=6, **extra):
    el = {"id": eid, "type": ftype, "key": key, "label": label, "width": width,
          "required": False, "fill_by": "inherit"}
    el.update(extra)
    return el


def _heading(eid, text, fill_by="submitter"):
    return {"id": eid, "type": "heading", "text": text, "width": 12, "fill_by": fill_by,
            "style": {"size": "md", "color": "#0F766E", "align": "left"}}


def incoming_schema():
    return {
        "theme": {"accent": "#0F766E", "header_bg": "#0F766E", "header_text": "#FFFFFF",
                  "label_color": "#334155", "font": "helvetica", "density": "compact", "rounded": True},
        "header": {"title": "Incoming mail docket", "subtitle": "Packages & Mailroom · UN PASS",
                   "align": "left", "show_reference": True},
        "elements": [
            _heading("h_item", "Item"),
            _field("f_tracking", "text", "tracking_code", "Tracking code"),
            _field("f_logged", "text", "logged_at", "Logged on"),
            _field("f_item", "text", "item_type", "Item type"),
            _field("f_desc", "textarea", "description", "Description", 12, rows=2),
            _heading("h_sender", "Sender"),
            _field("f_sname", "text", "sender_name", "Sender"),
            _field("f_stype", "select", "sender_type", "Sender type", options=SENDER_TYPE_LABELS),
            _field("f_sorg", "text", "sender_org", "Organisation"),
            _field("f_scontact", "text", "sender_contact", "Contact"),
            _field("f_semail", "email", "sender_email", "Sender email"),
            _heading("h_dest", "Destination"),
            _field("f_dest", "text", "destination_agency", "Destination agency"),
            _field("f_focal", "email", "dest_focal_email", "Agency focal point email"),
            _field("f_recipient", "text", "for_recipient", "Named recipient"),
            _field("f_notes", "textarea", "notes", "Gate / reception notes", 12, rows=2),
            _heading("h_receipt", "Receipt"),
            {"id": "sig_receipt", "type": "signature", "width": 6, "label": "Received by",
             "role": "Recipient", "step": RECEIPT_STEP, "show_date": True},
        ],
    }


def outgoing_schema():
    return {
        "theme": {"accent": "#15803D", "header_bg": "#15803D", "header_text": "#FFFFFF",
                  "label_color": "#334155", "font": "helvetica", "density": "compact", "rounded": True},
        "header": {"title": "Outgoing mail docket", "subtitle": "Packages & Mailroom · UN PASS",
                   "align": "left", "show_reference": True},
        "elements": [
            _heading("h_item", "Item"),
            _field("f_tracking", "text", "tracking_code", "Tracking code"),
            _field("f_logged", "text", "logged_at", "Registered on"),
            _field("f_item", "text", "item_type", "Item type"),
            _field("f_desc", "textarea", "description", "Description", 12, rows=2),
            _heading("h_origin", "Originator"),
            _field("f_sname", "text", "sender_name", "Originator"),
            _field("f_sorg", "text", "sender_org", "Unit / project"),
            _field("f_scontact", "text", "sender_contact", "Phone"),
            _field("f_semail", "email", "sender_email", "Originator email"),
            _heading("h_recipient", "Recipient"),
            _field("f_recipient", "text", "for_recipient", "Recipient name"),
            _field("f_dest", "text", "destination_agency", "Organisation / agency"),
            _field("f_rorg", "text", "recipient_org", "Department"),
            _field("f_remail", "email", "recipient_email", "Recipient email"),
            _field("f_raddr", "textarea", "recipient_address", "Delivery address", 12, rows=2),
            _field("f_notes", "textarea", "notes", "Notes", 12, rows=2),
            _heading("h_dispatch", "Dispatch", fill_by=DISPATCH_STEP),
            _field("f_courier", "select", "courier", "Sent by", 4, options=COURIERS),
            _field("f_cref", "text", "courier_reference", "Courier / pouch reference", 4),
            _field("f_dispatched", "date", "dispatched_on", "Dispatched on", 4),
        ],
    }


def schema_for(direction):
    from accounts.form_pdf_esign import clean_schema

    return clean_schema(outgoing_schema() if direction == "outgoing" else incoming_schema())


def docket_for(agency, direction):
    """The agency's docket for this direction, or None."""
    from .models_mailflow import MailDocket

    if agency is None:
        return None
    return MailDocket.objects.select_related("form").filter(agency=agency, direction=direction).first()


@transaction.atomic
def ensure_docket(agency, direction, user):
    """Return the docket, creating its form the first time it is needed."""
    from accounts.models_esign_studio import FormTemplate

    from .models_mailflow import MailDocket

    existing = docket_for(agency, direction)
    if existing:
        return existing

    label = "Outgoing" if direction == "outgoing" else "Incoming"
    form = FormTemplate.objects.create(
        agency=agency,
        created_by=user,
        office_id=getattr(user, "country_office_id", None),
        name=f"Mail docket — {label.lower()}",
        description=(f"{label} mail docket used by Packages & Mailroom. Keep the field keys; "
                     "everything else (look, extra sections, signature boxes) is yours to change."),
        category="Mailroom",
        schema=schema_for(direction),
        share_scope="agency",
        access="internal",
        is_published=False,          # filled by the mailroom, never by hand
        owner_sees_submissions=True,
        reference_prefix="PKG",
        submit_message="",
    )
    return MailDocket.objects.create(agency=agency, direction=direction, form=form, created_by=user)


def missing_keys(schema, direction):
    """Docket keys a customised docket has lost — shown as a warning."""
    have = {el.get("key") for el in (schema or {}).get("elements") or []}
    wanted = OUTGOING_KEYS if direction == "outgoing" else INCOMING_KEYS
    return [k for k in wanted if k not in have]


# ─────────────────────────────────────────────────────────────────────────────
# Starter flows
# ─────────────────────────────────────────────────────────────────────────────

def _node(nid, ntype, label, x, y, **config):
    return {"id": nid, "type": ntype, "label": label, "x": x, "y": y, "config": config}


def _assign_slot(slot):
    return {"mode": "chosen", "slot": slot, "users": [], "emails": []}


def _assign_field(field):
    return {"mode": "field", "field": field, "users": [], "emails": []}


def _edge(a, port, b):
    return {"id": f"e_{a}_{port}_{b}"[:40], "from": a, "port": port, "to": b}


def incoming_starter_graph():
    """
    Logged → Registry receives → Government sender?
        yes → Focal point clears it → Recipient signs the docket
        no  →                         Recipient signs the docket
    → Thank the sender → Delivered
    """
    nodes = [
        _node("start", "start", "Logged at the mailroom", 40, 200),
        _node("registry_receive", "review", "Registry receives it", 240, 200,
              assign=_assign_slot("Registry"), rule="any", due_days=1, allow_return=True,
              instructions="Confirm the item is in hand and the docket is right."),
        _node("is_gov", "condition", "From government?", 460, 200,
              field="sender_type", op="eq", value="Government / Law Enforcement"),
        _node("focal_clear", "approval", "Focal point clears it", 680, 90,
              assign=_assign_field("dest_focal_email"), rule="any", due_days=2, allow_return=True,
              instructions="Official correspondence — confirm it can be handed over."),
        _node(RECEIPT_STEP, "signature", "Recipient signs for it", 900, 200,
              assign=_assign_slot("Recipient"), order="sequential", placement="auto",
              message="Please sign to confirm you have received this item."),
        _node("thank_sender", "notify", "Tell the sender", 1120, 200,
              assign=_assign_field("sender_email"), attach_pdf=False,
              message="Your item has been received and handed over."),
        _node("delivered", "end", "Delivered", 1320, 200, outcome="completed"),
        _node("returned", "end", "Returned to sender", 900, 40, outcome="rejected"),
    ]
    edges = [
        _edge("start", "next", "registry_receive"),
        _edge("registry_receive", "next", "is_gov"),
        _edge("is_gov", "yes", "focal_clear"),
        _edge("is_gov", "no", RECEIPT_STEP),
        _edge("focal_clear", "approved", RECEIPT_STEP),
        _edge("focal_clear", "rejected", "returned"),
        _edge(RECEIPT_STEP, "signed", "thank_sender"),
        _edge("thank_sender", "next", "delivered"),
    ]
    return {"nodes": nodes, "edges": edges}


def outgoing_starter_graph():
    """Registered → Supervisor approves → Registry records dispatch → Tell the recipient → Dispatched"""
    nodes = [
        _node("start", "start", "Item registered", 40, 200),
        _node("supervisor_ok", "approval", "Supervisor approves sending", 260, 200,
              assign=_assign_slot("Supervisor"), rule="any", due_days=2, allow_return=True,
              instructions="Check the item may leave the office."),
        _node(DISPATCH_STEP, "fill", "Registry records dispatch", 500, 200,
              assign=_assign_slot("Registry"), rule="any", due_days=1, allow_return=True,
              instructions="Fill in how it was sent and the courier reference."),
        _node("tell_recipient", "notify", "Tell the recipient", 740, 200,
              assign=_assign_field("recipient_email"), attach_pdf=False,
              message="An item is on its way to you."),
        _node("tell_originator", "notify", "Tell the originator", 740, 320,
              assign={"mode": "initiator", "users": [], "emails": []}, attach_pdf=True,
              message="Your item has been dispatched — the docket is attached."),
        _node("dispatched", "end", "Dispatched", 980, 200, outcome="completed"),
        _node("not_sent", "end", "Not sent", 500, 60, outcome="rejected"),
    ]
    edges = [
        _edge("start", "next", "supervisor_ok"),
        _edge("supervisor_ok", "approved", DISPATCH_STEP),
        _edge("supervisor_ok", "rejected", "not_sent"),
        _edge(DISPATCH_STEP, "next", "tell_recipient"),
        _edge("tell_recipient", "next", "tell_originator"),
        _edge("tell_originator", "next", "dispatched"),
    ]
    return {"nodes": nodes, "edges": edges}


@transaction.atomic
def create_starter_flow(agency, direction, user):
    """Create a Flow Studio flow wired to the docket. Returns the DocumentWorkflow."""
    from accounts.models_esign_studio import DocumentWorkflow
    from accounts.workflow_engine_esign import clean_graph

    docket = ensure_docket(agency, direction, user)
    graph = clean_graph(outgoing_starter_graph() if direction == "outgoing" else incoming_starter_graph())
    label = "Outgoing" if direction == "outgoing" else "Incoming"
    return DocumentWorkflow.objects.create(
        agency=agency,
        created_by=user,
        office_id=getattr(user, "country_office_id", None),
        name=f"{label} mail — standard",
        description=f"Starter mail flow from Packages & Mailroom. Runs on the {label.lower()} mail docket.",
        graph=graph,
        form=docket.form,
        share_scope="agency",
        monitor_runs=True,
        is_active=True,
    )
