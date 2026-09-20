# accounts/module_events.py
"""
UN PASS — what the modules announce, and what they hand over.

An asset request ends when the requester confirms they have the thing. A
clearance ends when the last approver signs. Those endings are the natural place
to start something else: a handover form, an acceptance certificate, a
condition-of-issue undertaking, an accountability flow.

This file is the list of those moments, and — just as importantly — the list of
what each one is allowed to hand to whatever it starts. A trigger's mapping can
read only the tokens declared here. Nothing walks arbitrary attributes off a
model, so adding an event is a deliberate act with a reviewable diff.

Adding one:

    register(ModuleEvent(
        code="asset_return.received",
        module="Assets",
        label="A returned asset has been checked back in",
        model="accounts.AssetReturnRequest",
        tokens=[
            Token("asset_name", "Asset", "asset.name"),
            Token("returned_by", "Returned by", "requested_by.get_full_name"),
        ],
    ))

then call, at the point the thing actually happens:

    module_events.fire("asset_return.received", self, actor=by_user)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Tokens every event carries, so a mapping can always reach these.
COMMON_TOKENS = ("record_id", "record_label", "event_label", "event_at", "actor", "actor_email", "agency")


class Token:
    """One readable value a triggered form may borrow.

    `path` is a dotted walk from the record — attributes and no-argument
    callables only. It is written here by a developer, never by a user.
    """

    __slots__ = ("key", "label", "path", "kind", "help")

    def __init__(self, key, label, path, kind="text", help=""):
        self.key = key
        self.label = label
        self.path = path
        self.kind = kind
        self.help = help

    def as_dict(self):
        return {"key": self.key, "label": self.label, "type": self.kind, "help": self.help}


class ModuleEvent:
    __slots__ = ("code", "module", "label", "model", "tokens", "people", "description")

    def __init__(self, code, module, label, model, tokens, people=None, description=""):
        self.code = code
        self.module = module
        self.label = label
        self.model = model
        self.tokens = list(tokens)
        #: {"requester": "requester", "manager": "manager_approved_by", …}
        #: who the triggered form can be handed to, by dotted path to a user.
        self.people = dict(people or {})
        self.description = description

    def token_dicts(self):
        common = [
            {"key": "record_label", "label": "What it was", "type": "text"},
            {"key": "record_id", "label": "Record number", "type": "number"},
            {"key": "event_label", "label": "What happened", "type": "text"},
            {"key": "event_at", "label": "When it happened", "type": "date"},
            {"key": "actor", "label": "Who did it", "type": "text"},
            {"key": "actor_email", "label": "Their email", "type": "text"},
            {"key": "agency", "label": "Agency", "type": "text"},
        ]
        return [t.as_dict() for t in self.tokens] + common


_REGISTRY = {}


def register(event):
    _REGISTRY[event.code] = event
    return event


def get(code):
    return _REGISTRY.get(code)


def all_events():
    return sorted(_REGISTRY.values(), key=lambda e: (e.module, e.label))


def grouped():
    out = {}
    for event in all_events():
        out.setdefault(event.module, []).append(event)
    return out


def choices():
    return [(e.code, f"{e.module} — {e.label}") for e in all_events()]


# ─────────────────────────────────────────────────────────────────────────────
# Reading a value off a record
# ─────────────────────────────────────────────────────────────────────────────

def walk(record, path):
    """Follow a declared dotted path. Never raises; a dead end is blank."""
    value = record
    for part in str(path or "").split("."):
        if value is None:
            return ""
        try:
            value = getattr(value, part)
        except Exception:  # noqa: BLE001
            return ""
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001
                return ""
    return "" if value is None else value


def context_for(event, record, actor=None, extra=None):
    """{token: value} — everything a mapping on this event may read."""
    from django.utils import timezone

    values = {}
    for token in event.tokens:
        raw = walk(record, token.path)
        if hasattr(raw, "isoformat"):
            raw = raw.isoformat()[:10] if token.kind == "date" else raw.isoformat()
        values[token.key] = raw if isinstance(raw, (list, dict)) else str(raw)

    agency = walk(record, "agency.name") or walk(record, "agency.code")
    values.update({
        "record_id": str(getattr(record, "pk", "") or ""),
        "record_label": str(record)[:200],
        "event_label": event.label,
        "event_at": timezone.localdate().isoformat(),
        "actor": (actor.get_full_name() or actor.username) if actor else "",
        "actor_email": (actor.email or "") if actor else "",
        "agency": str(agency or ""),
    })
    values.update({k: str(v) for k, v in (extra or {}).items()})
    return values


def person_for(event, record, role):
    """The user behind one of the event's declared people roles."""
    path = event.people.get(role)
    if not path:
        return None
    value = walk(record, path)
    return value if hasattr(value, "pk") and hasattr(value, "email") else None


# ─────────────────────────────────────────────────────────────────────────────
# Announcing
# ─────────────────────────────────────────────────────────────────────────────

def fire(code, record, actor=None, request=None, extra=None):
    """
    Tell the automation layer that something finished.

    Never raises and never blocks the module that called it: an asset handover
    must not fail because a follow-on form is misconfigured.
    """
    event = get(code)
    if event is None:
        logger.warning("Module events: nothing registered under %s", code)
        return []
    try:
        from . import trigger_engine_esign

        return trigger_engine_esign.handle(event, record, actor=actor, request=request, extra=extra)
    except Exception:  # noqa: BLE001
        logger.exception("Module events: %s could not be handled for %s", code, getattr(record, "pk", "?"))
        return []


# ─────────────────────────────────────────────────────────────────────────────
# The events themselves
# ─────────────────────────────────────────────────────────────────────────────

register(ModuleEvent(
    code="asset_request.received",
    module="Assets",
    label="Asset issued and confirmed by the requester",
    model="accounts.AssetRequest",
    description="The end of the line for an asset request: ICT has issued the "
                "asset and the person has confirmed they have it.",
    tokens=[
        Token("asset_name", "Asset", "assigned_asset.name"),
        Token("asset_tag", "Asset tag", "assigned_asset.asset_tag"),
        Token("asset_serial", "Serial number", "assigned_asset.serial_number"),
        Token("asset_category", "Category", "category.name"),
        Token("requester_name", "Requester", "requester.get_full_name"),
        Token("requester_email", "Requester's email", "requester.email"),
        Token("requester_unit", "Unit", "unit.name"),
        Token("issued_by", "Issued by", "ict_assigned_by.get_full_name"),
        Token("issued_on", "Issued on", "ict_assigned_at", kind="date"),
        Token("confirmed_on", "Confirmed on", "requester_verified_at", kind="date"),
        Token("approved_by", "Approved by", "manager_approved_by.get_full_name"),
        Token("justification", "Why it was needed", "justification"),
    ],
    people={"requester": "requester", "issuer": "ict_assigned_by", "manager": "manager_approved_by"},
))

register(ModuleEvent(
    code="asset_request.assigned",
    module="Assets",
    label="Asset assigned by ICT, awaiting confirmation",
    model="accounts.AssetRequest",
    tokens=[
        Token("asset_name", "Asset", "assigned_asset.name"),
        Token("asset_tag", "Asset tag", "assigned_asset.asset_tag"),
        Token("asset_serial", "Serial number", "assigned_asset.serial_number"),
        Token("requester_name", "Requester", "requester.get_full_name"),
        Token("requester_email", "Requester's email", "requester.email"),
        Token("issued_by", "Issued by", "ict_assigned_by.get_full_name"),
        Token("issued_on", "Issued on", "ict_assigned_at", kind="date"),
    ],
    people={"requester": "requester", "issuer": "ict_assigned_by"},
))

register(ModuleEvent(
    code="asset_return.received",
    module="Assets",
    label="Returned asset checked back in",
    model="accounts.AssetReturnRequest",
    tokens=[
        Token("asset_name", "Asset", "asset.name"),
        Token("asset_tag", "Asset tag", "asset.asset_tag"),
        Token("returned_by", "Returned by", "requested_by.get_full_name"),
        Token("returned_by_email", "Their email", "requested_by.email"),
        Token("checked_by", "Checked in by", "verified_by.get_full_name"),
    ],
    people={"requester": "requested_by", "issuer": "verified_by"},
))

register(ModuleEvent(
    code="consumable_request.approved",
    module="Consumables",
    label="Consumable request approved",
    model="accounts.ConsumableRequest",
    tokens=[
        Token("requester_name", "Requester", "requester.get_full_name"),
        Token("requester_email", "Requester's email", "requester.email"),
        Token("requester_unit", "Unit", "unit.name"),
        Token("approved_by", "Approved by", "approved_by.get_full_name"),
        Token("linked_asset", "For asset", "linked_asset.name"),
    ],
    people={"requester": "requester", "manager": "approved_by"},
))

register(ModuleEvent(
    code="exit_request.completed",
    module="Staff exit",
    label="Exit clearance completed",
    model="accounts.ExitRequest",
    tokens=[
        Token("staff_name", "Departing staff", "user.get_full_name"),
        Token("staff_email", "Their email", "user.email"),
    ],
    people={"requester": "user"},
))

register(ModuleEvent(
    code="employee_id_card.issued",
    module="ID cards",
    label="Employee ID card issued",
    model="accounts.EmployeeIDCardRequest",
    tokens=[
        Token("holder_name", "Card holder", "for_user.get_full_name"),
        Token("holder_email", "Their email", "for_user.email"),
        Token("issued_by", "Issued by", "issued_by.get_full_name"),
    ],
    people={"requester": "for_user", "issuer": "issued_by"},
))

register(ModuleEvent(
    code="mobile_line.assigned",
    module="Mobile lines",
    label="Mobile line assigned to someone",
    model="accounts.MobileLine",
    tokens=[
        Token("holder_name", "Assigned to", "assigned_to.get_full_name"),
        Token("holder_email", "Their email", "assigned_to.email"),
        Token("custodian", "Custodian", "custodian.get_full_name"),
    ],
    people={"requester": "assigned_to", "issuer": "custodian"},
))
