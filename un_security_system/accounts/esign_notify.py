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

import logging
import re

from .asset_email import send_email_async
from .models_esign import Envelope, EnvelopeRecipient
from .utils_esign import log_event, review_url, sign_url

TPL = "accounts/esign/email/{}.html"
logger = logging.getLogger(__name__)


def _brand():
    """Wordmark used in every eSign email. Matches the page border stamp and
    the certificate — one setting, ESIGN_BRAND, drives all three."""
    return getattr(settings, "ESIGN_BRAND", "UNDP eSign")


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


def _send(subject, to_emails, template, ctx, attachments=None):
    to_emails = sorted({e for e in (to_emails or []) if e})
    if not to_emails:
        return
    try:
        send_email_async(
            subject=subject,
            to_emails=to_emails,
            html_template=TPL.format(template),
            context={**ctx, "subject": subject},
            attachments=attachments,
        )
    except Exception:
        logger.exception("eSign: could not queue '%s' to %s", template, to_emails)


# ─────────────────────────────────────────────────────────────────────────────
# Attachments
# ─────────────────────────────────────────────────────────────────────────────

def _safe_filename(text: str, fallback: str) -> str:
    """A readable filename a recipient can find later, not a 32-char hex blob."""
    cleaned = re.sub(r"[^\w\s.-]", "", str(text or "")).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)[:60].strip("-.")
    return cleaned or fallback


def _read_file(handle) -> bytes | None:
    """Read a FieldFile to bytes here, on the request thread — the email is sent
    from a background thread where storage handles may already be closed."""
    if not handle:
        return None
    try:
        handle.open("rb")
        data = handle.read()
        handle.close()
        return data or None
    except Exception:
        logger.exception("eSign: could not read %s for attachment", getattr(handle, "name", "?"))
        return None


def build_completed_attachments(envelope):
    """
    (attachments, note) for a completed envelope.

    Signed PDF first, Certificate of Completion second. Skipped entirely if
    the pair would exceed ESIGN_ATTACH_MAX_BYTES — plenty of mail gateways
    silently drop oversized messages, and a recipient who receives nothing is
    worse off than one who receives a link.
    """
    if not getattr(settings, "ESIGN_ATTACH_COMPLETED", True):
        return [], ""

    limit = int(getattr(settings, "ESIGN_ATTACH_MAX_BYTES", 10 * 1024 * 1024))
    base = _safe_filename(envelope.reference or envelope.subject, envelope.envelope_id[:8])

    items = []
    signed = _read_file(envelope.completed_pdf)
    if signed:
        items.append((f"{base}-signed.pdf", signed, "application/pdf"))

    if getattr(settings, "ESIGN_ATTACH_CERTIFICATE", True):
        cert = _read_file(envelope.certificate_pdf)
        if cert:
            items.append((f"{base}-certificate-of-completion.pdf", cert, "application/pdf"))

    total = sum(len(c) for _, c, _ in items)
    if not items:
        return [], ""

    if total > limit:
        logger.info(
            "eSign: envelope %s attachments are %d bytes, over the %d limit — sending links only",
            envelope.envelope_id, total, limit,
        )
        return [], (
            "The signed document was too large to attach to this email. "
            "Use the link above to download it."
        )

    return items, ""


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


def notify_returned(request, recipient: EnvelopeRecipient):
    """Signer sent it back — the sender needs to act."""
    env = recipient.envelope
    _send(
        f"Returned for changes by {recipient.name}: {env.subject}",
        _sender_email(env),
        "returned",
        _ctx(env, recipient=recipient, reason=env.return_reason,
             action_url=_detail_url(request, env)),
    )
    # Anyone who already signed should know the round is paused.
    others = [
        r.email for r in env.signers()
        if r.status == EnvelopeRecipient.STATUS_SIGNED and r.email != recipient.email
    ]
    if others:
        _send(
            f"Paused — returned for changes: {env.subject}",
            others,
            "returned",
            _ctx(env, recipient=recipient, reason=env.return_reason,
                 action_url=_detail_url(request, env), is_copy=True),
        )


def notify_markup_reply(request, recipient: EnvelopeRecipient, envelope: Envelope,
                        author: str, text: str, page=None):
    """
    Someone replied to a mark this recipient left on the document.

    Without this the conversation is one-directional: a recipient's mark emails
    the sender, but the sender's answer only ever appears in the page thread —
    so a signer who has closed the tab never learns their question was answered
    and the envelope stalls waiting on them.

    Reuses the `comment` email template. If you want wording specific to page
    markup, copy it to `accounts/esign/email/markup_reply.html` and change the
    template name below.
    """
    subject = f"Reply to your comment: {envelope.subject}"

    if recipient.is_signing_role and recipient.status != EnvelopeRecipient.STATUS_SIGNED:
        action_url = sign_url(request, recipient)
    else:
        action_url = review_url(request, recipient)

    _send(
        subject,
        [recipient.email],
        "comment",
        _ctx(envelope, recipient=recipient, author=author, comment=text,
             action_url=action_url, page=page, is_markup_reply=True),
    )
    log_event(
        envelope,
        "delivered",
        request=request,
        recipient=recipient,
        note=f"Markup reply emailed to {recipient.email}",
    )


def notify_comment(request, envelope: Envelope, author: str, text: str):
    _send(
        f"Comment from {author}: {envelope.subject}",
        _sender_email(envelope),
        "comment",
        _ctx(envelope, author=author, comment=text,
             action_url=_detail_url(request, envelope)),
    )


def notify_voided(request, envelope: Envelope):
    subject = f"Voided: {envelope.subject}"
    to = {r.email for r in envelope.recipients.all()} | set(_sender_email(envelope))
    _send(subject, list(to), "voided", _ctx(envelope, reason=envelope.void_reason))


# ─────────────────────────────────────────────────────────────────────────────
# Completion — signers, CC, viewers get a shared To; BCC always sent alone
# ─────────────────────────────────────────────────────────────────────────────

def notify_completed(request, envelope: Envelope):
    """
    Everyone who took part gets the signed PDF and the certificate as real
    attachments, plus the link. Read once here and reused for every message.
    """
    subject = f"Completed: {envelope.subject}"

    attachments, attach_note = build_completed_attachments(envelope)
    attached_names = [name for name, _, _ in attachments]

    open_recipients, blind_recipients = [], []
    for r in envelope.recipients.all():
        (blind_recipients if r.role == EnvelopeRecipient.ROLE_BCC else open_recipients).append(r)

    if attachments:
        log_event(
            envelope, "copy_delivered", request=request,
            note=f"Signed PDF attached to completion emails ({len(attachments)} file(s))",
        )

    for r in open_recipients:
        _send(
            subject,
            [r.email],
            "completed",
            _ctx(envelope, recipient=r, action_url=review_url(request, r),
                 attached=attached_names, attach_note=attach_note),
            attachments=attachments,
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
            _ctx(envelope, recipient=r, action_url=review_url(request, r), is_bcc=True,
                 attached=attached_names, attach_note=attach_note),
            attachments=attachments,
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
            _ctx(envelope, recipient=None, action_url=_detail_url(request, envelope),
                 attached=attached_names, attach_note=attach_note),
            attachments=attachments,
        )


def _detail_url(request, envelope):
    from django.urls import reverse

    path = reverse("accounts:esign_envelope_detail", args=[envelope.pk])
    if request is not None:
        return request.build_absolute_uri(path)
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    return f"{base}{path}" if base else path
