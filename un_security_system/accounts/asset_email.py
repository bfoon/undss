import threading

from django.conf import settings
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

    msg.send(fail_silently=True)


def send_email_async(
    subject: str,
    to_emails: list[str],
    html_template: str,
    context: dict,
    text_template: str | None = None,
    attachments: list | None = None,
):
    """
    Non-blocking email send using a background thread.
    """
    threading.Thread(
        target=_send_email,
        args=(subject, to_emails, html_template, context, text_template, attachments),
        daemon=True
    ).start()
