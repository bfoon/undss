# accounts/models_esign.py
"""
UN PASS — eSign module (DocuSign-style envelopes)

Drop this file into your `accounts` app next to models.py, then add ONE line at
the very bottom of accounts/models.py:

    from .models_esign import *  # noqa: F401,F403

Then:  python manage.py makemigrations accounts && python manage.py migrate
"""

import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

# If your app label is not "accounts", change these two strings only.
AGENCY_MODEL = "accounts.Agency"
ASSET_MODEL = "accounts.Asset"

__all__ = [
    "new_envelope_id",
    "new_token",
    "SignatureProfile",
    "Envelope",
    "EnvelopeDocument",
    "EnvelopeRecipient",
    "SignatureField",
    "EnvelopeComment",
    "EnvelopeEvent",
    "esign_signature_upload_to",
    "esign_document_upload_to",
    "esign_converted_upload_to",
    "esign_completed_upload_to",
]


# ─────────────────────────────────────────────────────────────────────────────
# Token helpers
# ─────────────────────────────────────────────────────────────────────────────

def new_envelope_id() -> str:
    """32-char uppercase hex, printed on every page (DocuSign parity)."""
    return uuid.uuid4().hex.upper()


def new_token(nbytes: int = 24) -> str:
    """URL-safe secret used for tokenized recipient links."""
    return secrets.token_urlsafe(nbytes)


def esign_signature_upload_to(instance, filename):
    return f"esign/signatures/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.png"


def esign_document_upload_to(instance, filename):
    env = getattr(instance, "envelope", None)
    eid = getattr(env, "envelope_id", "unassigned")
    return f"esign/envelopes/{eid}/source/{filename}"


def esign_converted_upload_to(instance, filename):
    env = getattr(instance, "envelope", None)
    eid = getattr(env, "envelope_id", "unassigned")
    return f"esign/envelopes/{eid}/converted/{filename}"


def esign_completed_upload_to(instance, filename):
    return f"esign/envelopes/{instance.envelope_id}/final/{filename}"


# ─────────────────────────────────────────────────────────────────────────────
# Saved signatures ("My Signature" studio)
# ─────────────────────────────────────────────────────────────────────────────

class SignatureProfile(models.Model):
    """
    A signature a user has saved and can re-use with one click.
    Whatever the input method, we always persist a transparent PNG so that
    stamping into the PDF is identical across types.
    """

    KIND_TYPED = "typed"
    KIND_DRAWN = "drawn"
    KIND_UPLOADED = "uploaded"
    KIND_CHOICES = [
        (KIND_TYPED, "Typed"),
        (KIND_DRAWN, "Hand drawn"),
        (KIND_UPLOADED, "Uploaded image"),
    ]

    # Keep these keys in sync with ESIGN_FONTS in views_esign.py / sign.html
    FONT_CHOICES = [
        ("dancing", "Dancing Script"),
        ("greatvibes", "Great Vibes"),
        ("sacramento", "Sacramento"),
        ("allura", "Allura"),
        ("caveat", "Caveat"),
        ("homemade", "Homemade Apple"),
        ("parisienne", "Parisienne"),
        ("cedarville", "Cedarville Cursive"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="signature_profiles",
    )
    label = models.CharField(max_length=60, blank=True, default="")
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_TYPED)

    typed_text = models.CharField(max_length=120, blank=True, default="")
    initials_text = models.CharField(max_length=12, blank=True, default="")
    font_key = models.CharField(max_length=24, blank=True, default="dancing")

    image = models.ImageField(upload_to=esign_signature_upload_to)
    initials_image = models.ImageField(
        upload_to=esign_signature_upload_to, blank=True, null=True
    )

    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "-updated_at"]
        verbose_name = "Saved signature"
        verbose_name_plural = "Saved signatures"

    def __str__(self):
        return f"{self.user} — {self.label or self.get_kind_display()}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            SignatureProfile.objects.filter(user=self.user).exclude(pk=self.pk).update(
                is_default=False
            )


# ─────────────────────────────────────────────────────────────────────────────
# Envelope
# ─────────────────────────────────────────────────────────────────────────────

class Envelope(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_SENT = "sent"
    STATUS_RETURNED = "returned"
    STATUS_COMPLETED = "completed"
    STATUS_DECLINED = "declined"
    STATUS_VOIDED = "voided"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_SENT, "Out for signature"),
        (STATUS_RETURNED, "Returned for changes"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_VOIDED, "Voided"),
        (STATUS_EXPIRED, "Expired"),
    ]

    agency = models.ForeignKey(
        AGENCY_MODEL, on_delete=models.CASCADE, related_name="esign_envelopes"
    )
    envelope_id = models.CharField(
        max_length=32, unique=True, default=new_envelope_id, editable=False, db_index=True
    )

    subject = models.CharField(max_length=200)
    message = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="esign_envelopes_created",
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_DRAFT)

    # Routing
    enforce_order = models.BooleanField(
        default=True, help_text="Sign in the order recipients were added."
    )
    is_self_sign = models.BooleanField(
        default=False,
        help_text=(
            "Signed by its creator alone — one recipient, no invitation email. "
            "Everything else about the envelope is unchanged, so the completed "
            "PDF and certificate are produced the same way."
        ),
    )
    reminders_enabled = models.BooleanField(default=True)
    reminder_days = models.PositiveSmallIntegerField(default=3)
    expires_at = models.DateTimeField(null=True, blank=True)

    # Link back into ICT & Asset (handover forms, clearance, exit, etc.)
    asset = models.ForeignKey(
        ASSET_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="esign_envelopes",
    )
    reference = models.CharField(
        max_length=120, blank=True, default="", help_text="Free-text reference / doc no."
    )

    # Outputs
    completed_pdf = models.FileField(
        upload_to=esign_completed_upload_to, blank=True, null=True
    )
    certificate_pdf = models.FileField(
        upload_to=esign_completed_upload_to, blank=True, null=True
    )
    download_token = models.CharField(max_length=64, default=new_token, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.TextField(blank=True, default="")
    last_reminded_at = models.DateTimeField(null=True, blank=True)

    # Return-for-changes / rework
    returned_at = models.DateTimeField(null=True, blank=True)
    return_reason = models.TextField(blank=True, default="")
    returned_by = models.ForeignKey(
        "accounts.EnvelopeRecipient",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="returned_envelopes",
    )
    #: Bumped every time the sender reworks and re-sends. Signatures collected
    #: against an earlier revision are cleared, because they attested to a
    #: document that no longer exists.
    revision = models.PositiveSmallIntegerField(default=1)
    duplicated_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.subject} ({self.short_id})"

    # -- display helpers ------------------------------------------------
    @property
    def short_id(self) -> str:
        """DocuSign-style abbreviated envelope token, e.g. 9F3C…A118."""
        return f"{self.envelope_id[:8]}…{self.envelope_id[-4:]}"

    @property
    def is_open(self) -> bool:
        return self.status in (self.STATUS_DRAFT, self.STATUS_SENT, self.STATUS_RETURNED)

    @property
    def is_editable(self) -> bool:
        """Documents, fields and recipients can be changed in these states."""
        return self.status in (self.STATUS_DRAFT, self.STATUS_RETURNED)

    @property
    def status_color(self) -> str:
        return {
            self.STATUS_DRAFT: "secondary",
            self.STATUS_SENT: "warning",
            self.STATUS_RETURNED: "warning",
            self.STATUS_COMPLETED: "success",
            self.STATUS_DECLINED: "danger",
            self.STATUS_VOIDED: "dark",
            self.STATUS_EXPIRED: "dark",
        }.get(self.status, "secondary")

    # -- routing helpers ------------------------------------------------
    def signers(self):
        return self.recipients.filter(
            role__in=[EnvelopeRecipient.ROLE_SIGNER, EnvelopeRecipient.ROLE_APPROVER]
        ).order_by("order", "id")

    def observers(self):
        return self.recipients.filter(
            role__in=[
                EnvelopeRecipient.ROLE_CC,
                EnvelopeRecipient.ROLE_BCC,
                EnvelopeRecipient.ROLE_VIEWER,
            ]
        ).order_by("order", "id")

    def next_pending_signer(self):
        return self.signers().exclude(
            status__in=[
                EnvelopeRecipient.STATUS_SIGNED,
                EnvelopeRecipient.STATUS_DECLINED,
                EnvelopeRecipient.STATUS_RETURNED,
            ]
        ).first()

    def progress(self):
        total = self.signers().count()
        done = self.signers().filter(status=EnvelopeRecipient.STATUS_SIGNED).count()
        pct = int(round((done / total) * 100)) if total else 0
        return {"total": total, "done": done, "percent": pct}

    def all_signed(self) -> bool:
        signers = self.signers()
        return signers.exists() and not signers.exclude(
            status=EnvelopeRecipient.STATUS_SIGNED
        ).exists()


class EnvelopeDocument(models.Model):
    envelope = models.ForeignKey(
        Envelope, on_delete=models.CASCADE, related_name="documents"
    )
    file = models.FileField(upload_to=esign_document_upload_to)
    #: PDF rendition of `file`, produced once at upload time for anything that
    #: isn't already a PDF (DOCX/XLSX/PPTX via LibreOffice, images via Pillow).
    #: Everything downstream — viewer, field placement, stamping — reads this.
    converted_pdf = models.FileField(
        upload_to=esign_converted_upload_to, blank=True, null=True
    )
    name = models.CharField(max_length=200, blank=True, default="")
    order = models.PositiveSmallIntegerField(default=0)
    page_count = models.PositiveSmallIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name or (self.file.name.rsplit("/", 1)[-1] if self.file else "Document")

    @property
    def source_ext(self) -> str:
        base = self.name or (self.file.name if self.file else "")
        return ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""

    @property
    def was_converted(self) -> bool:
        return bool(self.converted_pdf)


class EnvelopeRecipient(models.Model):
    ROLE_SIGNER = "signer"
    ROLE_APPROVER = "approver"
    ROLE_CC = "cc"
    ROLE_BCC = "bcc"
    ROLE_VIEWER = "viewer"
    ROLE_CHOICES = [
        (ROLE_SIGNER, "Needs to sign"),
        (ROLE_APPROVER, "Needs to approve"),
        (ROLE_CC, "Receives a copy (CC)"),
        (ROLE_BCC, "Receives a blind copy (BCC)"),
        (ROLE_VIEWER, "Can view only"),
    ]

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_VIEWED = "viewed"
    STATUS_SIGNED = "signed"
    STATUS_DECLINED = "declined"
    STATUS_RETURNED = "returned"
    STATUS_DELIVERED = "delivered"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Waiting"),
        (STATUS_SENT, "Sent"),
        (STATUS_VIEWED, "Viewed"),
        (STATUS_SIGNED, "Signed"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_RETURNED, "Returned for changes"),
        (STATUS_DELIVERED, "Copy delivered"),
    ]

    envelope = models.ForeignKey(
        Envelope, on_delete=models.CASCADE, related_name="recipients"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="esign_recipients",
    )
    name = models.CharField(max_length=150)
    email = models.EmailField()
    title = models.CharField(max_length=120, blank=True, default="")

    role = models.CharField(max_length=12, choices=ROLE_CHOICES, default=ROLE_SIGNER)
    order = models.PositiveSmallIntegerField(default=1)

    token = models.CharField(max_length=64, unique=True, default=new_token, db_index=True)
    access_code = models.CharField(
        max_length=32, blank=True, default="", help_text="Optional shared secret."
    )

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    sent_at = models.DateTimeField(null=True, blank=True)
    viewed_at = models.DateTimeField(null=True, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.TextField(blank=True, default="")
    reminded_at = models.DateTimeField(null=True, blank=True)

    signed_ip = models.GenericIPAddressField(null=True, blank=True)
    signed_user_agent = models.CharField(max_length=300, blank=True, default="")
    consent_accepted = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.name} <{self.email}> — {self.get_role_display()}"

    @property
    def is_signing_role(self) -> bool:
        return self.role in (self.ROLE_SIGNER, self.ROLE_APPROVER)

    @property
    def short_token(self) -> str:
        """Short recipient token stamped beside the signature."""
        return self.token[:10].upper()

    @property
    def status_color(self) -> str:
        return {
            self.STATUS_PENDING: "secondary",
            self.STATUS_SENT: "info",
            self.STATUS_VIEWED: "primary",
            self.STATUS_SIGNED: "success",
            self.STATUS_DECLINED: "danger",
            self.STATUS_RETURNED: "warning",
            self.STATUS_DELIVERED: "primary",
        }.get(self.status, "secondary")

    def can_sign_now(self) -> bool:
        env = self.envelope
        if env.status != Envelope.STATUS_SENT or not self.is_signing_role:
            return False
        if self.status in (self.STATUS_SIGNED, self.STATUS_DECLINED):
            return False
        if not env.enforce_order:
            return True
        nxt = env.next_pending_signer()
        return bool(nxt and nxt.pk == self.pk)


class SignatureField(models.Model):
    """
    A field placed on a document page. Coordinates are stored as fractions of
    the page (0..1) with the origin at the TOP-LEFT, so the same values work in
    the browser overlay and in the ReportLab stamp.
    """

    KIND_SIGNATURE = "signature"
    KIND_INITIALS = "initials"
    KIND_DATE = "date_signed"
    KIND_NAME = "full_name"
    KIND_TITLE = "job_title"
    KIND_EMAIL = "email"
    KIND_TEXT = "text"
    KIND_CHECKBOX = "checkbox"
    KIND_CHOICES = [
        (KIND_SIGNATURE, "Signature"),
        (KIND_INITIALS, "Initials"),
        (KIND_DATE, "Date signed"),
        (KIND_NAME, "Full name"),
        (KIND_TITLE, "Title"),
        (KIND_EMAIL, "Email"),
        (KIND_TEXT, "Text box"),
        (KIND_CHECKBOX, "Checkbox"),
    ]

    envelope = models.ForeignKey(Envelope, on_delete=models.CASCADE, related_name="fields")
    document = models.ForeignKey(
        EnvelopeDocument, on_delete=models.CASCADE, related_name="fields"
    )
    recipient = models.ForeignKey(
        EnvelopeRecipient, on_delete=models.CASCADE, related_name="fields"
    )

    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_SIGNATURE)
    label = models.CharField(max_length=80, blank=True, default="")
    page = models.PositiveSmallIntegerField(default=1)

    x = models.FloatField(default=0.1)
    y = models.FloatField(default=0.1)
    w = models.FloatField(default=0.22)
    h = models.FloatField(default=0.06)

    required = models.BooleanField(default=True)

    value = models.TextField(blank=True, default="")
    image = models.ImageField(upload_to=esign_signature_upload_to, blank=True, null=True)
    filled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["document__order", "page", "y", "x"]

    def __str__(self):
        return f"{self.get_kind_display()} p{self.page} → {self.recipient.name}"

    @property
    def is_filled(self) -> bool:
        if self.kind in (self.KIND_SIGNATURE, self.KIND_INITIALS):
            return bool(self.image)
        if self.kind == self.KIND_CHECKBOX:
            return self.value == "1"
        return bool((self.value or "").strip())


class EnvelopeComment(models.Model):
    """
    A note on the envelope. Signers can leave one while reviewing — asking a
    question or flagging a problem — without declining or returning.

    Internal comments are visible only to the sender and the ICT/Ops team;
    recipients never see them.
    """

    envelope = models.ForeignKey(Envelope, on_delete=models.CASCADE, related_name="comments")
    recipient = models.ForeignKey(
        EnvelopeRecipient, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="comments",
    )
    author_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="esign_comments",
    )
    author_name = models.CharField(max_length=150, blank=True, default="")

    text = models.TextField()
    #: Optional anchor, so a comment can point at a specific page
    document = models.ForeignKey(
        EnvelopeDocument, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="comments",
    )
    page = models.PositiveSmallIntegerField(null=True, blank=True)

    is_internal = models.BooleanField(
        default=False, help_text="Visible to the sender's team only."
    )
    revision = models.PositiveSmallIntegerField(default=1)

    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.display_name}: {self.text[:40]}"

    @property
    def display_name(self) -> str:
        if self.author_name:
            return self.author_name
        if self.recipient:
            return self.recipient.name
        if self.author_user:
            return self.author_user.get_full_name() or self.author_user.username
        return "Unknown"

    @property
    def role_label(self) -> str:
        if self.is_internal:
            return "Internal"
        if self.recipient:
            return self.recipient.get_role_display()
        return "Sender"


class EnvelopeEvent(models.Model):
    """Immutable audit trail. One row per action, rendered into the certificate."""

    EVENTS = [
        ("created", "Envelope created"),
        ("document_added", "Document added"),
        ("field_placed", "Fields placed"),
        ("recipient_changed", "Recipients changed"),
        ("sent", "Sent for signature"),
        ("delivered", "Email delivered"),
        ("viewed", "Document viewed"),
        ("consent", "Electronic record consent accepted"),
        ("signed", "Signed"),
        ("approved", "Approved"),
        ("declined", "Declined"),
        ("returned", "Returned for changes"),
        ("reworked", "Reworked and re-sent"),
        ("commented", "Comment added"),
        ("duplicated", "Duplicated"),
        ("reminder_sent", "Reminder sent"),
        ("completed", "Completed"),
        ("copy_delivered", "Copy delivered"),
        ("downloaded", "Downloaded"),
        ("voided", "Voided"),
        ("resent", "Resent"),
    ]

    envelope = models.ForeignKey(Envelope, on_delete=models.CASCADE, related_name="events")
    recipient = models.ForeignKey(
        EnvelopeRecipient,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    event = models.CharField(max_length=24, choices=EVENTS)
    note = models.CharField(max_length=300, blank=True, default="")
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    meta = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["at", "id"]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} — {self.get_event_display()}"

    @property
    def icon(self) -> str:
        return {
            "created": "bi-file-earmark-plus",
            "document_added": "bi-paperclip",
            "field_placed": "bi-crosshair",
            "recipient_changed": "bi-person-gear",
            "sent": "bi-send",
            "delivered": "bi-envelope-check",
            "viewed": "bi-eye",
            "consent": "bi-shield-check",
            "signed": "bi-pen",
            "approved": "bi-patch-check",
            "declined": "bi-x-octagon",
            "returned": "bi-arrow-return-left",
            "reworked": "bi-tools",
            "commented": "bi-chat-left-text",
            "duplicated": "bi-files",
            "reminder_sent": "bi-bell",
            "completed": "bi-check2-circle",
            "copy_delivered": "bi-mailbox",
            "downloaded": "bi-download",
            "voided": "bi-slash-circle",
            "resent": "bi-arrow-repeat",
        }.get(self.event, "bi-dot")
