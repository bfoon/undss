import contextlib
import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

# Scheduled jobs set this so mail goes out before the job returns. A daemon
# thread is right inside a web request (the user should not wait on SMTP) and
# wrong everywhere else: a management command run by cron, or a worker being
# restarted, exits — and takes every unsent daemon thread with it, silently.
# Reminders queued that way were simply never delivered.
_SYNC = threading.local()


@contextlib.contextmanager
def deliver_synchronously(raise_errors: bool = True):
    """
    Within this block, `send_email_async` sends on the calling thread.

    With `raise_errors`, an SMTP failure raises instead of vanishing, so the
    job that asked for the email can count it as failed. Callers like
    esign_notify._send already catch and log, so this surfaces failures in
    the logs and the job heartbeat rather than breaking the loop.
    """
    previous = getattr(_SYNC, "on", False), getattr(_SYNC, "raise_errors", False)
    _SYNC.on, _SYNC.raise_errors = True, raise_errors
    try:
        yield
    finally:
        _SYNC.on, _SYNC.raise_errors = previous


def delivering_synchronously() -> bool:
    return bool(getattr(_SYNC, "on", False))
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


def _send_email(
    subject: str,
    to_emails: list[str],
    html_template: str,
    context: dict,
    text_template: str | None = None,
    attachments: list | None = None,
):
    """
    Sends a single email with HTML body (and optional text fallback).

    `attachments` is an optional list of (filename, content_bytes, mimetype)
    tuples. Content must already be bytes — never a file handle — because this
    runs on a background thread where the request's storage handles may already
    be closed.
    """
    to_emails = [e for e in (to_emails or []) if e]
    if not to_emails:
        return

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or "no-reply@example.com"

    html_body = render_to_string(html_template, context)
    text_body = render_to_string(text_template, context) if text_template else "Please view this message in HTML."

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=from_email,
        to=to_emails,
    )
    msg.attach_alternative(html_body, "text/html")

    for item in (attachments or []):
        try:
            filename, content, mimetype = item
            if content:
                msg.attach(filename, content, mimetype)
        except Exception:
            # A bad attachment must never stop the message itself going out.
            continue

    try:
        msg.send(fail_silently=False)
    except Exception as exc:
        # Never silent any more. The failure is logged with the SMTP server's
        # own words and remembered, so `unpass_doctor` and the envelope page
        # can say "e-mail is failing, and why" instead of everyone assuming
        # the invitation went out.
        logger.error("E-mail to %s failed (%s): %s", to_emails, subject, exc)
        _remember(False, f"{type(exc).__name__}: {exc}", to_emails)
        if getattr(_SYNC, "raise_errors", False):
            raise
        return
    _remember(True, "", to_emails)


_STATE = "unpass:mail:state"


def _remember(ok: bool, error: str, to_emails):
    """Last success / last failure, in Redis next to the job heartbeats."""
    try:
        import json

        from django.utils import timezone

        from .scheduler import redis_client

        client = redis_client()
        if client is None:
            return
        now = timezone.now().isoformat()
        if ok:
            client.hset(_STATE, mapping={"last_ok": now})
            client.hincrby(_STATE, "sent", 1)
        else:
            client.hset(_STATE, mapping={
                "last_error_at": now, "last_error": error[:500],
                "last_error_to": json.dumps(list(to_emails)[:5]),
            })
            client.hincrby(_STATE, "failed", 1)
    except Exception:
        pass


def mail_state() -> dict:
    try:
        from .scheduler import redis_client

        client = redis_client()
        return dict(client.hgetall(_STATE)) if client is not None else {}
    except Exception:
        return {}


def mail_config_problems() -> list[str]:
    """What is missing for mail to work at all, in plain words."""
    backend = getattr(settings, "EMAIL_BACKEND", "")
    problems = []
    if backend.endswith("smtp.EmailBackend"):
        if not getattr(settings, "EMAIL_HOST", ""):
            problems.append("EMAIL_HOST is not set")
        if "office365" in (getattr(settings, "EMAIL_HOST", "") or "").lower() or \
                "outlook" in (getattr(settings, "EMAIL_HOST", "") or "").lower():
            if not getattr(settings, "EMAIL_HOST_USER", ""):
                problems.append("EMAIL_HOST_USER is not set (Office 365 needs a mailbox to sign in as)")
            if not getattr(settings, "EMAIL_HOST_PASSWORD", ""):
                problems.append("EMAIL_HOST_PASSWORD is not set")
    elif backend.endswith("mail_graph.GraphEmailBackend"):
        for name in ("MS_GRAPH_TENANT_ID", "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET"):
            if not getattr(settings, name, ""):
                problems.append(f"{name} is not set")
    elif backend.endswith(("console.EmailBackend", "locmem.EmailBackend",
                           "dummy.EmailBackend", "filebased.EmailBackend")):
        problems.append(f"EMAIL_BACKEND is {backend.rsplit('.', 1)[-1]}, which does not deliver mail")
    if not (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip():
        problems.append("DEFAULT_FROM_EMAIL is not set")
    return problems


def send_email_async(
    subject: str,
    to_emails: list[str],
    html_template: str,
    context: dict,
    text_template: str | None = None,
    attachments: list | None = None,
):
    """
    Non-blocking email send using a background thread — except inside
    `deliver_synchronously()`, where it sends before returning.
    """
    if delivering_synchronously():
        _send_email(subject, to_emails, html_template, context,
                    text_template, attachments)
        return
    threading.Thread(
        target=_send_email,
        args=(subject, to_emails, html_template, context, text_template, attachments),
        daemon=True
    ).start()
