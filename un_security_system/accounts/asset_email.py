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

    msg.send(fail_silently=not getattr(_SYNC, "raise_errors", False))


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
