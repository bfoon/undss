# accounts/esign_delivery.py
"""
UN PASS — eSign Studio: who gets the signed copy, and when.

Two ways to hand out a signed document:

  "each"    (the old behaviour) — every signature step finishes on its own, and
            everyone on that envelope is emailed the signed PDF straight away.
            A flow with three signature steps therefore sends three copies.

  "at_end"  — nothing is emailed while the flow runs. When the last step is
            done, one message goes out with the finished document, the
            certificate of completion and any follow-on documents (invoice,
            receipt) attached, addressed to the signers and the person who
            started it.

The choice is made on the form (Settings → Signed copies) or on the workflow,
and is copied into the run when it starts, so changing the setting later never
changes a run that is already under way.

No migration: the policy lives in FormTemplate.schema["delivery"],
DocumentWorkflow.graph["delivery"] and WorkflowRun.context["delivery"].
"""

from __future__ import annotations

import logging
import re

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

MODE_EACH = "each"
MODE_AT_END = "at_end"
MODES = (MODE_EACH, MODE_AT_END)

DEFAULT_POLICY = {
    "mode": MODE_EACH,
    "single_message": True,
    "include_signers": True,
    "include_initiator": True,
    "include_participants": False,
    "attach_certificate": True,
    "attach_documents": True,
    "extra_emails": [],
    "message": "",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_policy(raw):
    raw = raw if isinstance(raw, dict) else {}
    emails, seen = [], set()
    for item in (raw.get("extra_emails") or [])[:20]:
        address = str(item or "").strip().lower()[:254]
        if _EMAIL_RE.match(address) and address not in seen:
            seen.add(address)
            emails.append(address)
    return {
        "mode": raw.get("mode") if raw.get("mode") in MODES else MODE_EACH,
        "single_message": bool(raw.get("single_message", True)),
        "include_signers": bool(raw.get("include_signers", True)),
        "include_initiator": bool(raw.get("include_initiator", True)),
        "include_participants": bool(raw.get("include_participants", False)),
        "attach_certificate": bool(raw.get("attach_certificate", True)),
        "attach_documents": bool(raw.get("attach_documents", True)),
        "extra_emails": emails,
        "message": str(raw.get("message") or "").strip()[:600],
    }


def default_policy():
    return dict(DEFAULT_POLICY, extra_emails=[])


# ─────────────────────────────────────────────────────────────────────────────
# Finding the policy that applies
# ─────────────────────────────────────────────────────────────────────────────

def policy_for_form(form):
    if form is None:
        return default_policy()
    schema_policy = (form.schema or {}).get("delivery") if isinstance(form.schema, dict) else None
    if schema_policy:
        return clean_policy(schema_policy)
    if form.workflow_id:
        return policy_for_workflow(form.workflow)
    return default_policy()


def policy_for_workflow(workflow):
    if workflow is None:
        return default_policy()
    graph_policy = (workflow.graph or {}).get("delivery") if isinstance(workflow.graph, dict) else None
    return clean_policy(graph_policy) if graph_policy else default_policy()


def policy_for_run(run):
    """The policy frozen into the run when it started, or the best guess."""
    if run is None:
        return default_policy()
    stored = (run.context or {}).get("delivery") if isinstance(run.context, dict) else None
    if stored:
        return clean_policy(stored)
    if run.submission_id and run.submission and run.submission.form_id:
        return policy_for_form(run.submission.form)
    return policy_for_workflow(run.workflow)


def freeze_policy(run, form=None, workflow=None):
    """Called by start_run: remember the policy this run will follow."""
    policy = policy_for_form(form) if form is not None else policy_for_workflow(workflow or run.workflow)
    if not isinstance(run.context, dict):
        run.context = {}
    run.context["delivery"] = policy
    return policy


def run_for_envelope(envelope):
    task = envelope.workflow_tasks.select_related("run").first() if envelope else None
    return task.run if task else None


def hold_signed_copies(envelope):
    """
    True when this envelope belongs to a flow that hands out copies only at the
    end. esign_notify.notify_completed checks this before emailing anybody.
    """
    try:
        run = run_for_envelope(envelope)
    except Exception:  # noqa: BLE001
        return False
    if run is None:
        return False
    return policy_for_run(run).get("mode") == MODE_AT_END


# ─────────────────────────────────────────────────────────────────────────────
# Who is on the list
# ─────────────────────────────────────────────────────────────────────────────

def participants(run, policy=None):
    """[{'name', 'email', 'role'}] for the people this policy wants to reach."""
    from .models_esign import EnvelopeRecipient
    from .models_esign_studio import WorkflowTask

    policy = policy or policy_for_run(run)
    people, seen = [], set()

    def add(name, email, role):
        address = (email or "").strip().lower()
        if not address or address in seen:
            return
        seen.add(address)
        people.append({"name": (name or "").strip() or address, "email": address, "role": role})

    if policy.get("include_initiator") and run.initiator:
        add(run.initiator.get_full_name() or run.initiator.username, run.initiator.email, "initiator")

    if policy.get("include_signers"):
        envelope_ids = [
            task.envelope_id for task in run.tasks.filter(kind=WorkflowTask.KIND_SIGNATURE)
            if task.envelope_id
        ]
        if envelope_ids:
            recipients = EnvelopeRecipient.objects.filter(
                envelope_id__in=envelope_ids,
                role__in=[EnvelopeRecipient.ROLE_SIGNER],
            )
            for recipient in recipients:
                add(recipient.name, recipient.email, "signer")

    if policy.get("include_participants"):
        for task in run.tasks.exclude(email=""):
            add(task.name, task.email, "participant")

    for address in policy.get("extra_emails") or []:
        add(address, address, "copy")

    if run.submission_id and run.submission and run.submission.submitter_email:
        if policy.get("include_initiator"):
            add(run.submission.submitter_name, run.submission.submitter_email, "submitter")

    return people


# ─────────────────────────────────────────────────────────────────────────────
# The bundle
# ─────────────────────────────────────────────────────────────────────────────

def _safe_name(text, fallback):
    cleaned = re.sub(r"[^\w\s.-]", "", str(text or "")).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)[:60].strip("-.")
    return cleaned or fallback


def _read(handle):
    if not handle:
        return None
    try:
        handle.open("rb")
        data = handle.read()
        handle.close()
        return data or None
    except Exception:  # noqa: BLE001
        return None


def bundle_attachments(run, policy=None):
    """
    (attachments, names, note) — the finished document, the certificates of the
    envelopes that were signed along the way, and any follow-on documents.
    """
    from .models_esign_studio import WorkflowTask

    policy = policy or policy_for_run(run)
    limit = int(getattr(settings, "ESIGN_ATTACH_MAX_BYTES", 10 * 1024 * 1024))
    base = _safe_name(run.subject, run.reference)
    attachments, skipped = [], []
    total = 0

    def offer(name, data):
        nonlocal total
        if not data:
            return
        if total + len(data) > limit:
            skipped.append(name)
            return
        total += len(data)
        attachments.append((name, data, "application/pdf"))

    offer(f"{base}-{run.reference}.pdf", _read(run.final_pdf) or _read(run.document))

    if policy.get("attach_certificate"):
        for task in run.tasks.filter(kind=WorkflowTask.KIND_SIGNATURE).select_related("envelope"):
            envelope = task.envelope
            if not envelope:
                continue
            offer(f"{base}-{envelope.short_id}-certificate.pdf", _read(envelope.certificate_pdf))

    if policy.get("attach_documents"):
        try:
            from .models_esign_docgen import GeneratedDocument

            for document in GeneratedDocument.objects.filter(
                run=run, status=GeneratedDocument.STATUS_READY
            ).select_related("submission"):
                offer(f"{_safe_name(document.title, document.number)}.pdf", _read(document.pdf))
        except Exception:  # noqa: BLE001
            logger.exception("eSign Studio: could not attach generated documents for run %s", run.pk)

    note = ""
    if skipped:
        note = ("Some files were too large to attach and can be downloaded from the run: "
                + ", ".join(skipped))
    return attachments, [name for name, _data, _mime in attachments], note


def deliver_final(run, request=None):
    """
    Send the finished document once, to everyone the policy names. Returns the
    number of messages sent. Safe to call twice — it only sends once per run.
    """
    from .asset_email import send_email_async
    from .models_esign_studio import WorkflowRun
    from .workflow_engine_esign import log_run, run_url

    policy = policy_for_run(run)
    if run.status != WorkflowRun.STATUS_COMPLETED:
        return 0
    if not isinstance(run.context, dict):
        run.context = {}
    if run.context.get("final_copies_sent"):
        return 0

    people = participants(run, policy)
    if not people:
        return 0

    attachments, names, note = bundle_attachments(run, policy)
    subject = f"Signed and completed: {run.subject}"
    context = {
        "brand": getattr(settings, "ESIGN_BRAND", "UNDP eSign"),
        "now": timezone.now(),
        "subject": subject,
        "run": run,
        "reference": run.reference,
        "subject_line": run.subject,
        "flow_name": run.workflow_name,
        "headline": "Everyone has signed — here is the completed document",
        "detail": policy.get("message") or run.outcome_note or "",
        "tone": "success",
        "attached": bool(attachments),
        "attached_names": names,
        "attach_note": note,
        "action_label": "Open the record",
        "run_link": run_url(run, request),
    }

    if run.submission_id:
        from .form_pdf_esign import summary_rows

        context["summary"] = [
            row for row in summary_rows(run.submission.schema, run.submission.values) if row[1]
        ][:12]

    sent = 0
    template = "accounts/esign/email/workflow_update.html"

    if policy.get("single_message"):
        # One message. Addresses are visible to each other on purpose: these are
        # the parties to the same document.
        try:
            send_email_async(
                subject=subject,
                to_emails=[p["email"] for p in people],
                html_template=template,
                context={**context, "recipient_name": ""},
                attachments=attachments,
            )
            sent = 1
        except Exception:  # noqa: BLE001
            logger.exception("eSign Studio: bundled completion email failed for run %s", run.pk)
    else:
        for person in people:
            try:
                send_email_async(
                    subject=subject,
                    to_emails=[person["email"]],
                    html_template=template,
                    context={**context, "recipient_name": person["name"]},
                    attachments=attachments,
                )
                sent += 1
            except Exception:  # noqa: BLE001
                logger.exception("eSign Studio: completion email to %s failed for run %s",
                                 person["email"], run.pk)

    if sent:
        run.context["final_copies_sent"] = timezone.now().isoformat()
        run.save(update_fields=["context"])
        log_run(
            run, "notified",
            note=(f"Signed copy delivered to {len(people)} recipient(s) in "
                  f"{'one message' if policy.get('single_message') else str(sent) + ' messages'}"
                  f" with {len(attachments)} attachment(s).")[:300],
            meta={"recipients": [p["email"] for p in people], "attachments": names},
        )
    return sent


def describe(policy):
    """One line for the designer and the run page."""
    policy = clean_policy(policy)
    if policy["mode"] == MODE_EACH:
        return "Each signer is emailed a signed copy as soon as their step finishes."
    who = []
    if policy["include_signers"]:
        who.append("the signers")
    if policy["include_initiator"]:
        who.append("whoever started it")
    if policy["include_participants"]:
        who.append("everyone who took part")
    if policy["extra_emails"]:
        who.append(f"{len(policy['extra_emails'])} extra address(es)")
    return (("One email when the whole flow is finished, to " + " and ".join(who or ["nobody"]) + ".")
            + (" Certificate attached." if policy["attach_certificate"] else "")
            + (" Follow-on documents attached." if policy["attach_documents"] else ""))
