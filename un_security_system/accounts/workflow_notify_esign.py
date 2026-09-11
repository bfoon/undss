# accounts/workflow_notify_esign.py
"""
UN PASS — eSign Studio workflow emails.

Same delivery path as esign_notify.py: `asset_email.send_email_async`, so a
decision never waits on SMTP. Signature steps don't email from here at all —
they create an ordinary envelope and esign_notify sends the invitations.

Who hears what
--------------
assignee     a task is waiting (approve / review / fill / place fields / resubmit)
initiator    each decision, a return for changes, needs attention, finished
participants finished (completed, rejected or cancelled)
notify step  whoever the step names, with its message and optionally the PDF
"""

import logging
import re

from django.conf import settings
from django.utils import timezone

from .asset_email import send_email_async

logger = logging.getLogger(__name__)
TPL = "accounts/esign/email/{}.html"


def _brand():
    return getattr(settings, "ESIGN_BRAND", "UNDP eSign")


def _send(subject, to_emails, template, ctx, attachments=None):
    to_emails = sorted({(e or "").strip().lower() for e in (to_emails or []) if e})
    if not to_emails:
        return 0
    try:
        send_email_async(
            subject=subject,
            to_emails=to_emails,
            html_template=TPL.format(template),
            context={"brand": _brand(), "now": timezone.now(), "subject": subject, **ctx},
            attachments=attachments,
        )
        return len(to_emails)
    except Exception:  # noqa: BLE001
        logger.exception("eSign Studio: could not queue '%s' to %s", template, to_emails)
        return 0


def _safe_filename(text, fallback):
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


def _initiator(run):
    u = run.initiator
    if not u:
        return "", ""
    return (u.get_full_name() or u.username), (u.email or "")


def _run_ctx(run, request=None, **extra):
    from .workflow_engine_esign import run_url

    name, _ = _initiator(run)
    ctx = {
        "run": run,
        "reference": run.reference,
        "subject_line": run.subject,
        "flow_name": run.workflow_name,
        "initiator_name": name or _brand(),
        "run_link": run_url(run, request),
    }
    if run.submission_id:
        from .form_pdf_esign import summary_rows

        ctx["form_name"] = run.submission.form_name
        ctx["summary"] = [r for r in summary_rows(run.submission.schema, run.submission.values) if r[1]][:12]
    ctx.update(extra)
    return ctx


def _attach_pdf(run, which="document"):
    """[(name, bytes, mime)] or [] when missing / over ESIGN_ATTACH_MAX_BYTES."""
    limit = int(getattr(settings, "ESIGN_ATTACH_MAX_BYTES", 10 * 1024 * 1024))
    base = _safe_filename(run.subject, run.reference)
    if which == "final":
        data = _read(run.final_pdf)
        name = f"{base}-{run.reference}-final.pdf"
    else:
        try:
            from .workflow_engine_esign import run_pdf_bytes

            data = run_pdf_bytes(run)
        except Exception:  # noqa: BLE001
            data = None
        name = f"{base}-{run.reference}.pdf"
    if not data or len(data) > limit:
        return []
    return [(name, data, "application/pdf")]


# ─────────────────────────────────────────────────────────────────────────────
# Assignees
# ─────────────────────────────────────────────────────────────────────────────

def task_assigned(task, request=None):
    from .models_esign_studio import WorkflowTask
    from .workflow_engine_esign import task_url

    if not task.email:
        return 0            # internal user without an address: the task list and badge still show it
    run = task.run
    node = run.node(task.node_id) or {}
    verb = {
        WorkflowTask.KIND_APPROVAL: "Approval needed",
        WorkflowTask.KIND_REVIEW: "Please review",
        WorkflowTask.KIND_FILL: "Please complete",
        WorkflowTask.KIND_PREPARE: "Place signature fields",
        WorkflowTask.KIND_RESUBMIT: "Changes requested",
    }.get(task.kind, "Action needed")
    return _send(
        f"{verb}: {run.subject}",
        [task.email],
        "workflow_task",
        _run_ctx(
            run, request,
            task=task,
            verb=verb,
            recipient_name=task.name,
            step_label=task.node_label,
            instructions=((node.get("config") or {}).get("instructions") or ""),
            action_url=task_url(task, request),
            due_at=task.due_at,
        ),
    )


def reminder(task, request=None):
    from .workflow_engine_esign import task_url

    if not task.email:
        return 0
    run = task.run
    return _send(
        f"Reminder: {run.subject} is waiting for you",
        [task.email],
        "workflow_task",
        _run_ctx(run, request, task=task, verb="Reminder", recipient_name=task.name,
                 step_label=task.node_label, action_url=task_url(task, request),
                 due_at=task.due_at, is_reminder=True),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Initiator
# ─────────────────────────────────────────────────────────────────────────────

def decision_made(run, task, request=None):
    name, email = _initiator(run)
    if not email or (task.user_id and task.user_id == run.initiator_id):
        return 0
    return _send(
        f"{task.get_status_display()}: {run.subject} — {task.node_label}",
        [email],
        "workflow_update",
        _run_ctx(run, request, headline=f"{task.name} — {task.get_status_display().lower()}",
                 detail=task.comment, step_label=task.node_label, tone=task.status_color,
                 recipient_name=name),
    )


def returned(run, task, resubmit_task, request=None):
    from .workflow_engine_esign import task_url

    name, email = _initiator(run)
    if not email:
        return 0
    return _send(
        f"Changes requested: {run.subject}",
        [email],
        "workflow_update",
        _run_ctx(run, request, headline=f"{task.name} returned “{task.node_label}” for changes",
                 detail=task.comment, step_label=task.node_label, tone="warning",
                 recipient_name=name, action_url=task_url(resubmit_task, request),
                 action_label="Update and resubmit"),
    )


def run_blocked(run, request=None):
    name, email = _initiator(run)
    recipients = [email]
    if run.workflow_id and run.workflow.created_by_id and run.workflow.created_by.email:
        recipients.append(run.workflow.created_by.email)
    return _send(
        f"Needs attention: {run.subject}",
        recipients,
        "workflow_update",
        _run_ctx(run, request, headline="This workflow has stopped and needs attention",
                 detail=run.block_reason, tone="danger", recipient_name=name,
                 action_label="Open the run"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Finished
# ─────────────────────────────────────────────────────────────────────────────

def run_finished(run, request=None):
    from .models_esign_studio import WorkflowRun

    name, email = _initiator(run)
    people = set(run.tasks.exclude(email="").values_list("email", flat=True))
    people.discard(email)

    headline = {
        WorkflowRun.STATUS_COMPLETED: "Completed",
        WorkflowRun.STATUS_REJECTED: "Rejected",
        WorkflowRun.STATUS_CANCELLED: "Cancelled",
    }.get(run.status, run.get_status_display())
    tone = run.status_color
    attachments = _attach_pdf(run, "final") if run.status == WorkflowRun.STATUS_COMPLETED else []

    sent = _send(
        f"{headline}: {run.subject}",
        [email],
        "workflow_update",
        _run_ctx(run, request, headline=f"Your workflow is {headline.lower()}",
                 detail=run.outcome_note, tone=tone, recipient_name=name,
                 attached=bool(attachments), action_label="Open the run"),
        attachments=attachments,
    )
    # Everyone who took part — sent individually so addresses aren't exposed.
    for addr in sorted(people):
        sent += _send(
            f"{headline}: {run.subject}",
            [addr],
            "workflow_update",
            _run_ctx(run, request, headline=f"A workflow you took part in is {headline.lower()}",
                     detail=run.outcome_note, tone=tone, recipient_name="",
                     attached=bool(attachments), action_label="Open the run"),
            attachments=attachments,
        )
    return sent


def notify_step(run, node, people, request=None):
    cfg = node.get("config") or {}
    attachments = _attach_pdf(run, "document") if cfg.get("attach_pdf") else []
    sent = 0
    for p in people:
        if not p.get("email"):
            continue
        sent += _send(
            f"{node.get('label') or 'Update'}: {run.subject}",
            [p["email"]],
            "workflow_update",
            _run_ctx(run, request, headline=node.get("label") or "Update",
                     detail=cfg.get("message") or "", tone="info", recipient_name=p.get("name") or "",
                     attached=bool(attachments), action_label="Open the run"),
            attachments=attachments,
        )
    return sent
