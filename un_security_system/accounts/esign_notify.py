# accounts/esign_notify.py
"""
Every email in the eSign flow. Uses the existing non-blocking
`asset_email.send_email_async` so signing never waits on SMTP.

Flow coverage
-------------
sender   → invite sent, viewed, each signature, declined, completed
signer   → invite, reminder, "your turn now", completion copy
cc/bcc   → completion copy (BCC recipients are always sent separately,
           never exposed in a To/CC header)
viewer   → read-only link at send time + completion copy
"""

from django.conf import settings
from django.utils import timezone

from .asset_email import send_email_async
from .models_esign import Envelope, EnvelopeRecipient
from .utils_esign import log_event, review_url, sign_url

TPL = "accounts/esign/email/{}.html"


def _brand():
    return getattr(settings, "SITE_NAME", "UN PASS")


def _ctx(envelope, **extra):
    ctx = {
        "brand": _brand(),
        "envelope": envelope,
        "envelope_id": envelope.envelope_id,
        "short_id": envelope.short_id,
        "subject_line": envelope.subject,
        "message": envelope.message,
        "sender_name": (
            envelope.created_by.get_full_name() or envelope.created_by.username
            if envelope.created_by
            else _brand()
        ),
        "now": timezone.now(),
    }
    ctx.update(extra)
    return ctx


def _send(subject, to_emails, template, ctx):
    to_emails = sorted({e for e in (to_emails or []) if e})
    if not to_emails:
        return
    try:
        send_email_async(
            subject=subject,
            to_emails=to_emails,
            html_template=TPL.format(template),
            context={**ctx, "subject": subject},
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Signer-facing
# ─────────────────────────────────────────────────────────────────────────────

def notify_invite(request, recipient: EnvelopeRecipient, is_turn=True):
    env = recipient.envelope
    subject = f"Signature requested: {env.subject}"
    _send(
        subject,
        [recipient.email],
        "invite",
        _ctx(
            env,
            recipient=recipient,
            action_url=sign_url(request, recipient),
            is_turn=is_turn,
            access_code_required=bool(recipient.access_code),
        ),
    )
    log_event(
        env,
        "delivered",
        request=request,
        recipient=recipient,
        note=f"Signing invitation emailed to {recipient.email}",
    )


def notify_turn(request, recipient: EnvelopeRecipient):
    env = recipient.envelope
    subject = f"It's your turn to sign: {env.subject}"
    _send(
        subject,
        [recipient.email],
        "your_turn",
        _ctx(env, recipient=recipient, action_url=sign_url(request, recipient)),
    )
    log_event(
        env,
        "delivered",
        request=request,
        recipient=recipient,
        note=f"Turn notification emailed to {recipient.email}",
    )


def notify_reminder(request, recipient: EnvelopeRecipient):
    env = recipient.envelope
    subject = f"Reminder — signature still needed: {env.subject}"
    _send(
        subject,
        [recipient.email],
        "reminder",
        _ctx(env, recipient=recipient, action_url=sign_url(request, recipient)),
    )
    recipient.reminded_at = timezone.now()
    recipient.save(update_fields=["reminded_at"])
    log_event(
        env,
        "reminder_sent",
        request=request,
        recipient=recipient,
        note=f"Reminder emailed to {recipient.email}",
    )


def notify_viewer(request, recipient: EnvelopeRecipient):
    env = recipient.envelope
    subject = f"Document shared with you: {env.subject}"
    _send(
        subject,
        [recipient.email],
        "viewer_access",
        _ctx(env, recipient=recipient, action_url=review_url(request, recipient)),
    )
    log_event(
        env,
        "delivered",
        request=request,
        recipient=recipient,
        note=f"View-only link emailed to {recipient.email}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sender-facing
# ─────────────────────────────────────────────────────────────────────────────

def _sender_email(env):
    return [getattr(env.created_by, "email", None)]


def notify_sender_viewed(request, recipient: EnvelopeRecipient):
    env = recipient.envelope
    _send(
        f"Viewed by {recipient.name}: {env.subject}",
        _sender_email(env),
        "sender_viewed",
        _ctx(env, recipient=recipient, action_url=_detail_url(request, env)),
    )


def notify_sender_signed(request, recipient: EnvelopeRecipient):
    env = recipient.envelope
    prog = env.progress()
    _send(
        f"Signed by {recipient.name} ({prog['done']}/{prog['total']}): {env.subject}",
        _sender_email(env),
        "sender_signed",
        _ctx(env, recipient=recipient, progress=prog, action_url=_detail_url(request, env)),
    )


def notify_declined(request, recipient: EnvelopeRecipient):
    env = recipient.envelope
    subject = f"Declined by {recipient.name}: {env.subject}"
    to = set(_sender_email(env))
    for r in env.signers():
        if r.status == EnvelopeRecipient.STATUS_SIGNED:
            to.add(r.email)
    _send(
        subject,
        list(to),
        "declined",
        _ctx(env, recipient=recipient, reason=recipient.decline_reason),
    )


def notify_voided(request, envelope: Envelope):
    subject = f"Voided: {envelope.subject}"
    to = {r.email for r in envelope.recipients.all()} | set(_sender_email(envelope))
    _send(subject, list(to), "voided", _ctx(envelope, reason=envelope.void_reason))


# ─────────────────────────────────────────────────────────────────────────────
# Completion — signers, CC, viewers get a shared To; BCC always sent alone
# ─────────────────────────────────────────────────────────────────────────────

def notify_completed(request, envelope: Envelope):
    subject = f"Completed: {envelope.subject}"

    open_recipients, blind_recipients = [], []
    for r in envelope.recipients.all():
        (blind_recipients if r.role == EnvelopeRecipient.ROLE_BCC else open_recipients).append(r)

    for r in open_recipients:
        _send(
            subject,
            [r.email],
            "completed",
            _ctx(envelope, recipient=r, action_url=review_url(request, r)),
        )
        if r.role in (EnvelopeRecipient.ROLE_CC, EnvelopeRecipient.ROLE_VIEWER):
            r.status = EnvelopeRecipient.STATUS_DELIVERED
            r.save(update_fields=["status"])
            log_event(
                envelope,
                "copy_delivered",
                request=request,
                recipient=r,
                note=f"Completed copy delivered to {r.email} ({r.get_role_display()})",
            )

    for r in blind_recipients:
        _send(
            subject,
            [r.email],
            "completed",
            _ctx(envelope, recipient=r, action_url=review_url(request, r), is_bcc=True),
        )
        r.status = EnvelopeRecipient.STATUS_DELIVERED
        r.save(update_fields=["status"])
        log_event(
            envelope,
            "copy_delivered",
            request=request,
            recipient=r,
            note="Completed copy delivered (blind copy — not disclosed to other parties)",
        )

    sender = getattr(envelope.created_by, "email", None)
    if sender and sender not in {r.email for r in open_recipients}:
        _send(
            subject,
            [sender],
            "completed",
            _ctx(envelope, recipient=None, action_url=_detail_url(request, envelope)),
        )


def _detail_url(request, envelope):
    from django.urls import reverse

    path = reverse("accounts:esign_envelope_detail", args=[envelope.pk])
    if request is not None:
        return request.build_absolute_uri(path)
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    return f"{base}{path}" if base else path
