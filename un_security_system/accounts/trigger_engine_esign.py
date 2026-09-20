# accounts/trigger_engine_esign.py
"""
UN PASS — turning "the module finished" into "a form is waiting for you".

module_events.fire() lands here. For each trigger listening to that moment we
check its conditions, work out the answers from the record, and then either
leave a part-filled form for someone, submit it outright, or start a workflow.

Nothing in here is allowed to break the module that called it. Every path is
wrapped, and a failure is written to ModuleTriggerLog rather than raised.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import form_formula_esign as FX
from . import module_events

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Conditions
# ─────────────────────────────────────────────────────────────────────────────

OPS = {
    "eq": lambda a, b: FX.to_text(a).strip().casefold() == FX.to_text(b).strip().casefold(),
    "ne": lambda a, b: FX.to_text(a).strip().casefold() != FX.to_text(b).strip().casefold(),
    "contains": lambda a, b: FX.to_text(b).strip().casefold() in FX.to_text(a).casefold(),
    "filled": lambda a, b: bool(FX.to_text(a).strip()),
    "empty": lambda a, b: not FX.to_text(a).strip(),
    "gt": lambda a, b: (FX.to_number(a, strict=True) or 0) > (FX.to_number(b, strict=True) or 0),
    "lt": lambda a, b: (FX.to_number(a, strict=True) or 0) < (FX.to_number(b, strict=True) or 0),
}

OP_LABELS = [
    ("eq", "is"), ("ne", "is not"), ("contains", "contains"),
    ("filled", "has something in it"), ("empty", "is empty"),
    ("gt", "is more than"), ("lt", "is less than"),
]


def conditions_hold(trigger, context):
    for rule in trigger.conditions or []:
        if not isinstance(rule, dict):
            continue
        token = str(rule.get("token") or "").strip()
        op = OPS.get(str(rule.get("op") or "eq"))
        if not token or op is None:
            continue
        try:
            if not op(context.get(token, ""), rule.get("value", "")):
                return False
        except Exception:  # noqa: BLE001
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Filling the form in
# ─────────────────────────────────────────────────────────────────────────────

def build_values(trigger, context):
    """The answers to write into the target form. Problems are collected, not raised."""
    from .docgen_esign import _coerce, _target_element

    schema = (trigger.target_form.schema or {}) if trigger.target_form_id else {}
    values, problems = {}, []
    resolve = _resolver(context)

    for mapping in trigger.field_map or []:
        target = str((mapping or {}).get("target") or "").strip()
        expr = str((mapping or {}).get("expr") or "").strip()
        if not target or not expr:
            continue
        element = _target_element(schema, target)
        if element is None:
            problems.append(f"the form has no question called “{target}”")
            continue
        try:
            values[target] = _coerce(element, FX.evaluate(expr, resolve))
        except FX.FormulaError as exc:
            problems.append(f"{target}: {exc}")

    # Whatever the form works out for itself runs now, so its totals are right.
    values, _errors = FX.compute_values(schema, values)
    return values, problems


def _resolver(context):
    def resolve(name):
        name = str(name).strip()
        if name.startswith("@"):
            key = name[1:].lower()
            if key == "today":
                return timezone.localdate().isoformat()
            return context.get(key, "")
        return context.get(name, "")
    return resolve


def _recipient(trigger, event, record, actor):
    if trigger.assign_to == trigger.WHO_FIXED:
        return trigger.assignee
    if trigger.assign_to == trigger.WHO_ACTOR:
        return actor
    return module_events.person_for(event, record, trigger.assign_to)


def _reference(form):
    from .models_esign_studio import FormSubmission

    prefix = (form.reference_prefix or "FRM").strip() if form else "FRM"
    stamp = timezone.now()
    base = f"{prefix}-{stamp:%Y%m%d%H%M%S}"
    candidate, n = base, 2
    while FormSubmission.objects.filter(reference=candidate).exists() and n < 50:
        candidate = f"{base}-{n}"
        n += 1
    return candidate[:40]


# ─────────────────────────────────────────────────────────────────────────────
# Doing it
# ─────────────────────────────────────────────────────────────────────────────

def handle(event, record, actor=None, request=None, extra=None):
    """Every trigger listening to this event. Returns the logs written."""
    from .models_esign_triggers import ModuleTrigger

    triggers = (ModuleTrigger.objects
                .filter(event_code=event.code, is_active=True)
                .select_related("target_form", "target_workflow", "assignee")
                .order_by("order", "id"))

    agency_id = getattr(record, "agency_id", None)
    context = module_events.context_for(event, record, actor=actor, extra=extra)
    written = []

    for trigger in triggers:
        if trigger.agency_id and agency_id and trigger.agency_id != agency_id:
            continue
        try:
            written.append(run_one(trigger, event, record, context, actor=actor, request=request))
        except Exception:  # noqa: BLE001
            logger.exception("Trigger %s failed on %s", trigger.pk, event.code)
            written.append(_log(trigger, event, record, status="failed",
                                note="Something went wrong — see the server log."))
    return [w for w in written if w]


def run_one(trigger, event, record, context, actor=None, request=None):
    from .models_esign_studio import FormSubmission
    from .models_esign_triggers import ModuleTriggerLog

    record_model = event.model
    record_id = str(getattr(record, "pk", "") or "")

    if trigger.once_per_record and ModuleTriggerLog.objects.filter(
        trigger=trigger, record_model=record_model, record_id=record_id,
        status=ModuleTriggerLog.STATUS_STARTED,
    ).exists():
        return None

    if not conditions_hold(trigger, context):
        return _log(trigger, event, record, status="skipped", note="Its conditions were not met.")

    if not trigger.target_form_id:
        return _log(trigger, event, record, status="failed", note="No form is set on this trigger.")

    values, problems = build_values(trigger, context)
    recipient = _recipient(trigger, event, record, actor)
    form = trigger.target_form

    from .form_pdf_esign import clean_schema

    with transaction.atomic():
        submission = FormSubmission.objects.create(
            form=form,
            form_name=form.name,
            schema=clean_schema(form.schema),
            values=values,
            reference=_reference(form),
            agency=getattr(record, "agency", None) or form.agency,
            submitted_by=recipient,
            submitter_name=(recipient.get_full_name() or recipient.username) if recipient else "",
            submitter_email=(recipient.email or "") if recipient else "",
            status=(FormSubmission.STATUS_SUBMITTED if trigger.action != trigger.ACTION_DRAFT
                    else FormSubmission.STATUS_DRAFT),
        )

    run = None
    if trigger.action in (trigger.ACTION_SUBMIT, trigger.ACTION_FLOW):
        run = _start_flow(trigger, form, submission, recipient or actor, request)

    log = _log(
        trigger, event, record, status="started", submission=submission, run=run,
        assigned_to=recipient,
        note=("; ".join(problems))[:300] if problems else f"{len(values)} answer(s) carried over.",
    )

    if trigger.notify and recipient and trigger.action == trigger.ACTION_DRAFT:
        _invite(trigger, submission, recipient, context, request)
    return log


def _start_flow(trigger, form, submission, actor, request):
    from . import workflow_engine_esign as E

    workflow = trigger.target_workflow or (form.workflow if form.workflow_id else None)
    if workflow is None or not workflow.is_active:
        return None
    try:
        return E.start_run(
            workflow, actor, agency=submission.agency,
            subject=f"{form.name} — {submission.reference}",
            submission=submission, slots={}, request=request,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Trigger %s: the flow would not start for %s", trigger.pk, submission.reference)
        return None


def _invite(trigger, submission, recipient, context, request):
    from django.conf import settings

    from .asset_email import send_email_async

    try:
        path = reverse("accounts:esign_form_fill", args=[submission.form_id])
        link = request.build_absolute_uri(path) if request else path
    except Exception:  # noqa: BLE001
        link = ""

    subject = (trigger.email_subject or f"Please complete: {submission.form_name}")[:200]
    try:
        send_email_async(
            subject=subject,
            to_emails=[recipient.email],
            html_template="accounts/esign/email/workflow_update.html",
            context={
                "brand": getattr(settings, "ESIGN_BRAND", "UNDP eSign"),
                "now": timezone.now(),
                "subject": subject,
                "subject_line": submission.form_name,
                "headline": trigger.name,
                "detail": trigger.email_message or
                          f"This follows on from {context.get('record_label', 'a completed request')}. "
                          "Some of it has been filled in for you already.",
                "reference": submission.reference,
                "recipient_name": recipient.get_full_name() or recipient.username,
                "tone": "info",
                "action_label": "Open the form",
                "run_link": link,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("Trigger %s: could not invite %s", trigger.pk, recipient.email)


def _log(trigger, event, record, *, status, note="", submission=None, run=None, assigned_to=None):
    from .models_esign_triggers import ModuleTriggerLog

    return ModuleTriggerLog.objects.create(
        trigger=trigger,
        event_code=event.code,
        record_model=event.model,
        record_id=str(getattr(record, "pk", "") or ""),
        record_label=str(record)[:200],
        submission=submission,
        run=run,
        assigned_to=assigned_to,
        status=status,
        note=note[:300],
    )


# ─────────────────────────────────────────────────────────────────────────────
# For the editor
# ─────────────────────────────────────────────────────────────────────────────

def preview(trigger, record=None):
    """What this trigger would write, against a real record if one exists."""
    event = trigger.event
    if event is None:
        return {"ok": False, "error": "That event no longer exists in this build."}

    if record is None:
        from django.apps import apps

        try:
            app_label, model_name = event.model.split(".")
            record = apps.get_model(app_label, model_name).objects.order_by("-pk").first()
        except Exception:  # noqa: BLE001
            record = None
    if record is None:
        return {"ok": False, "error": "There are no records of that kind yet to try this on."}

    context = module_events.context_for(event, record)
    values, problems = build_values(trigger, context)

    from .form_pdf_esign import display_value

    rows = []
    for element in (trigger.target_form.schema or {}).get("elements") or []:
        key = element.get("key")
        if not key:
            continue
        rows.append({
            "key": key,
            "label": element.get("label") or key,
            "value": display_value(element, values.get(key)),
            "mapped": any(str(m.get("target")) == key for m in (trigger.field_map or [])),
        })
    return {
        "ok": True,
        "record": str(record)[:120],
        "context": [{"key": k, "value": str(v)[:120]} for k, v in sorted(context.items())],
        "rows": rows,
        "problems": problems,
    }
