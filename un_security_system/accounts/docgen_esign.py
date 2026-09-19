# accounts/docgen_esign.py
"""
UN PASS — eSign Studio: building the follow-on document.

A rule says "take this submission, write these answers into that form, number
it, render it and send it". The values are written with the same formula
language used on the forms themselves, so a mapping can do arithmetic:

    target "amount_due"   <-  SUM([items.line_total]) * 1.15
    target "client_name"  <-  [requester_name]
    target "issued_on"    <-  @today
    target table "lines"  <-  from "items", column "description" = [item],
                                              column "amount"      = [line_total]

Besides the source form's own answers, a mapping can read:

    @today            today, as yyyy-mm-dd
    @reference        the source submission's reference
    @form             the source form's name
    @submitter        who submitted it
    @submitter_email  their address
    @initiator        who started the workflow
    @run              the run reference
    @status           the run's status
"""

from __future__ import annotations

import logging
from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import form_formula_esign as FX

logger = logging.getLogger(__name__)


class DocGenError(RuntimeError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Context and mapping
# ─────────────────────────────────────────────────────────────────────────────

def context_values(submission, run=None):
    """The source answers plus the @… extras a mapping may read."""
    values = dict(submission.values or {})
    values["@reference"] = submission.reference
    values["@form"] = submission.form_name
    values["@submitter"] = submission.submitter_name
    values["@submitter_email"] = submission.submitter_email
    values["@today"] = timezone.localdate().isoformat()
    if run is not None:
        values["@run"] = run.reference
        values["@status"] = run.get_status_display()
        if run.initiator:
            values["@initiator"] = run.initiator.get_full_name() or run.initiator.username
            values["@initiator_email"] = run.initiator.email or ""
    return values


def _resolver(values, schema_index, row=None):
    return FX._resolver(values, schema_index, row=row)


def _target_element(schema, key):
    for element in (schema or {}).get("elements") or []:
        if element.get("key") == key:
            return element
    return None


def build_values(rule, submission, run=None):
    """
    Work out the child form's answers. Returns (values, problems) — problems is
    a list of readable strings; a mapping that fails never stops the others.
    """
    source_values = context_values(submission, run)
    source_index = {
        el["key"]: el for el in (submission.schema or {}).get("elements") or [] if el.get("key")
    }
    target_schema = rule.target_form.schema or {}
    values, problems = {}, []

    for mapping in rule.field_map or []:
        target = str((mapping or {}).get("target") or "").strip()
        expr = str((mapping or {}).get("expr") or "").strip()
        if not target or not expr:
            continue
        element = _target_element(target_schema, target)
        if element is None:
            problems.append(f"“{rule.target_form.name}” has no question called “{target}”.")
            continue
        try:
            raw = FX.evaluate(expr, _resolver(source_values, source_index))
        except FX.FormulaError as exc:
            problems.append(f"{target}: {exc}")
            continue
        values[target] = _coerce(element, raw)

    for mapping in rule.table_map or []:
        target = str((mapping or {}).get("target") or "").strip()
        source_key = str((mapping or {}).get("from") or "").strip()
        columns = (mapping or {}).get("columns") or {}
        if not target or not source_key:
            continue
        element = _target_element(target_schema, target)
        if element is None or element.get("type") != "table":
            problems.append(f"“{rule.target_form.name}” has no table called “{target}”.")
            continue
        source_rows = source_values.get(source_key)
        source_rows = [r for r in source_rows if isinstance(r, dict)] if isinstance(source_rows, list) else []
        rows = []
        for source_row in source_rows:
            row = {}
            for column_key, expr in columns.items():
                expr = str(expr or "").strip()
                if not expr:
                    continue
                try:
                    row[str(column_key)] = FX.to_text(
                        FX.evaluate(expr, _resolver(source_values, source_index, row=source_row))
                    )
                except FX.FormulaError as exc:
                    problems.append(f"{target}.{column_key}: {exc}")
                    row[str(column_key)] = ""
            if any(str(v).strip() for v in row.values()):
                rows.append(row)
        values[target] = rows

    # Anything the target form calculates itself is worked out now, so an
    # invoice's own totals are right even if the mapping only sent it the lines.
    values, _errors = FX.compute_values(target_schema, values)
    return values, problems


def _coerce(element, value):
    kind = element.get("type")
    if kind == "checkboxes":
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        text = FX.to_text(value)
        return [part.strip() for part in text.split(",") if part.strip()] if text else []
    if kind == "table":
        return value if isinstance(value, list) else []
    if kind == "yesno":
        return "yes" if FX.to_bool(value) else "no"
    if kind == "number":
        number = FX.to_number(value, strict=True)
        if number is None:
            return ""
        decimals = int(element.get("decimals") or 0)
        return f"{number:.{decimals}f}" if decimals else str(int(round(number)))
    if kind == "date":
        parsed = FX.to_date(value)
        return parsed.isoformat() if parsed else ""
    if kind in ("select", "radio"):
        text = FX.to_text(value).strip()
        options = element.get("options") or []
        if text in options:
            return text
        for option in options:
            if option.strip().casefold() == text.casefold():
                return option
        return ""
    return FX.to_text(value)


# ─────────────────────────────────────────────────────────────────────────────
# Numbering
# ─────────────────────────────────────────────────────────────────────────────

def next_number(rule, submission):
    from .models_esign_docgen import GeneratedDocument

    today = timezone.localdate()
    prefix = (rule.number_prefix or rule.target_form.reference_prefix or "DOC").strip()
    pattern = rule.number_format or "{prefix}-{year}-{sequence:04d}"
    used = GeneratedDocument.objects.filter(rule=rule).count()

    for attempt in range(1, 40):
        sequence = used + attempt
        try:
            number = pattern.format(
                prefix=prefix, year=today.year, month=f"{today.month:02d}",
                sequence=sequence, reference=submission.reference,
            )[:60]
        except (KeyError, IndexError, ValueError):
            number = f"{prefix}-{today.year}-{sequence:04d}"
        if not GeneratedDocument.objects.filter(number=number).exists():
            return number
    return f"{prefix}-{today.year}-{timezone.now():%H%M%S}"


def _child_reference(target_form, number):
    from .models_esign_studio import FormSubmission

    base = (number or f"{target_form.reference_prefix or 'DOC'}-{date.today().year}")[:40]
    candidate, n = base, 2
    while FormSubmission.objects.filter(reference=candidate).exists() and n < 50:
        candidate = f"{base}-{n}"[:40]
        n += 1
    return candidate


# ─────────────────────────────────────────────────────────────────────────────
# Producing a document
# ─────────────────────────────────────────────────────────────────────────────

def applies(rule, submission, run=None, trigger=None, node_id=""):
    if not rule.is_active or not rule.target_form_id:
        return False
    if trigger and rule.trigger != trigger:
        return False
    if rule.trigger == rule.TRIGGER_STEP and node_id and rule.node_id and rule.node_id != node_id:
        return False
    condition = rule.condition or {}
    if condition and (condition.get("rules") or []):
        from .esign_condition_engine import clean_tree, derive_values, evaluate_tree

        values = FX.compute_values(submission.schema or {}, submission.values or {})[0]
        values = derive_values(values, submission.schema or {})
        values.update({k: v for k, v in context_values(submission, run).items() if k.startswith("@")})
        return bool(evaluate_tree(values, clean_tree(condition)))
    return True


def generate(rule, submission, *, run=None, actor=None, request=None, force=False):
    """Build one document. Returns the GeneratedDocument (never raises)."""
    from django.core.files.base import ContentFile

    from .form_pdf_esign import clean_schema, render_form_pdf
    from .models_esign_docgen import GeneratedDocument
    from .models_esign_studio import FormSubmission

    existing = GeneratedDocument.objects.filter(rule=rule, source_submission=submission).first()
    if existing and not force:
        return existing

    document = GeneratedDocument(
        rule=rule, source_submission=submission, run=run, target_form=rule.target_form,
        title=f"{rule.name} — {submission.reference}"[:200],
    )

    try:
        values, problems = build_values(rule, submission, run)
        number = next_number(rule, submission)
        schema = clean_schema(rule.target_form.schema)

        with transaction.atomic():
            child = None
            try:
                child = FormSubmission.objects.create(
                    form=rule.target_form,
                    form_name=rule.target_form.name,
                    schema=schema,
                    values=values,
                    reference=_child_reference(rule.target_form, number),
                    agency=submission.agency or rule.target_form.agency,
                    submitted_by=getattr(actor, "pk", None) and actor or submission.submitted_by,
                    submitter_name=submission.submitter_name,
                    submitter_email=submission.submitter_email,
                    status=FormSubmission.STATUS_SUBMITTED,
                )
            except IntegrityError:
                logger.warning("eSign Studio: reference clash writing %s for %s", number, submission.reference)

            pdf, _boxes = render_form_pdf(
                schema, values,
                reference=number,
                submitted_at=timezone.now(),
                submitter=submission.submitter_name or "",
            )

            document.submission = child
            document.number = number
            document.status = GeneratedDocument.STATUS_READY
            document.error = "; ".join(problems)[:300]
            document.pdf.save(f"{number}.pdf", ContentFile(pdf), save=False)
            document.save()

        if rule.start_target_workflow and rule.target_form.workflow_id and child is not None:
            _start_child_flow(rule, child, request=request, actor=actor or submission.submitted_by)

    except Exception as exc:  # noqa: BLE001
        logger.exception("eSign Studio: could not generate “%s” for %s", rule.name, submission.reference)
        document.status = GeneratedDocument.STATUS_FAILED
        document.error = str(exc)[:300]
        document.save()
        return document

    if run is not None:
        try:
            from .workflow_engine_esign import log_run

            log_run(run, "step", note=f"Document produced: {rule.name} {number}."[:300])
        except Exception:  # noqa: BLE001
            pass

    if rule.send_when == rule.SEND_IMMEDIATELY:
        deliver(document, request=request)
    return document


def _start_child_flow(rule, child, *, request=None, actor=None):
    from . import workflow_engine_esign as E

    workflow = rule.target_form.workflow
    if not workflow or not workflow.is_active:
        return
    try:
        E.start_run(
            workflow, actor or rule.created_by, agency=child.agency or rule.target_form.agency,
            subject=f"{child.title} — {child.reference}", submission=child, slots={}, request=request,
        )
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: the follow-on flow for %s could not start", child.reference)


# ─────────────────────────────────────────────────────────────────────────────
# Triggers
# ─────────────────────────────────────────────────────────────────────────────

def run_rules(submission, trigger, *, run=None, node_id="", request=None, actor=None):
    """Produce every document whose rule matches this moment."""
    if submission is None or not submission.form_id:
        return []
    from .models_esign_docgen import FormDocumentRule

    made = []
    rules = FormDocumentRule.objects.filter(
        form_id=submission.form_id, is_active=True, trigger=trigger
    ).select_related("target_form", "target_form__workflow")
    for rule in rules:
        try:
            if not applies(rule, submission, run=run, trigger=trigger, node_id=node_id):
                continue
            made.append(generate(rule, submission, run=run, request=request, actor=actor))
        except Exception:  # noqa: BLE001
            logger.exception("eSign Studio: rule %s failed for %s", rule.pk, submission.reference)
    return made


# ─────────────────────────────────────────────────────────────────────────────
# Delivery
# ─────────────────────────────────────────────────────────────────────────────

def recipients_for(document):
    """[{'name', 'email'}] for one generated document."""
    rule = document.rule
    submission = document.source_submission
    people, seen = [], set()

    def add(name, email):
        address = str(email or "").strip().lower()
        if not address or "@" not in address or address in seen:
            return
        seen.add(address)
        people.append({"name": (name or address).strip(), "email": address})

    if rule is None:
        return people

    if rule.send_to_submitter:
        add(submission.submitter_name, submission.submitter_email)
    if rule.send_to_initiator and document.run and document.run.initiator:
        initiator = document.run.initiator
        add(initiator.get_full_name() or initiator.username, initiator.email)
    if rule.send_to_signers and document.run:
        from . import esign_delivery

        for person in esign_delivery.participants(
            document.run,
            {**esign_delivery.default_policy(), "include_signers": True,
             "include_initiator": False, "include_participants": False},
        ):
            add(person["name"], person["email"])

    values = submission.values or {}
    for key in rule.send_to_email_fields or []:
        add("", values.get(str(key)))
    for address in rule.send_to_emails or []:
        add("", address)
    return people


def deliver(document, request=None):
    """Email one generated document. Returns how many messages went out."""
    from django.conf import settings

    from .asset_email import send_email_async
    from .models_esign_docgen import GeneratedDocument

    rule = document.rule
    if rule is None or rule.send_when == rule.SEND_NEVER:
        return 0
    if document.status == GeneratedDocument.STATUS_FAILED or not document.pdf:
        return 0

    people = recipients_for(document)
    if not people:
        return 0

    try:
        document.pdf.open("rb")
        data = document.pdf.read()
        document.pdf.close()
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: could not read %s for delivery", document.number)
        return 0

    subject = (rule.email_subject or f"{rule.name}: {document.number}")[:200]
    sent = 0
    for person in people:
        try:
            send_email_async(
                subject=subject,
                to_emails=[person["email"]],
                html_template="accounts/esign/email/workflow_update.html",
                context={
                    "brand": getattr(settings, "ESIGN_BRAND", "UNDP eSign"),
                    "now": timezone.now(),
                    "subject": subject,
                    "subject_line": document.title,
                    "headline": rule.name,
                    "detail": rule.email_message or "",
                    "reference": document.number,
                    "flow_name": document.source_submission.form_name,
                    "recipient_name": person["name"],
                    "tone": "info",
                    "attached": True,
                    "action_label": "",
                },
                attachments=[(f"{document.number}.pdf", data, "application/pdf")],
            )
            sent += 1
        except Exception:  # noqa: BLE001
            logger.exception("eSign Studio: could not email %s to %s", document.number, person["email"])

    if sent:
        document.status = GeneratedDocument.STATUS_SENT
        document.sent_to = [p["email"] for p in people]
        document.sent_at = timezone.now()
        document.save(update_fields=["status", "sent_to", "sent_at"])
    return sent


def preview(rule, submission, run=None):
    """What the rule would write, without saving anything — used by the editor."""
    values, problems = build_values(rule, submission, run)
    schema = rule.target_form.schema or {}
    rows = []
    for element in schema.get("elements") or []:
        key = element.get("key")
        if not key:
            continue
        from .form_pdf_esign import display_value

        rows.append({
            "key": key,
            "label": element.get("label") or key,
            "type": element.get("type"),
            "value": display_value(element, values.get(key)),
            "mapped": any(str(m.get("target")) == key for m in (rule.field_map or []))
                      or any(str(m.get("target")) == key for m in (rule.table_map or [])),
        })
    return {"values": values, "rows": rows, "problems": problems,
            "number": f"{rule.number_prefix or 'DOC'}-…"}
