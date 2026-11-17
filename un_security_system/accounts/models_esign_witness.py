# accounts/models_esign_witness.py
"""
UN PASS — NES cross-office witnessing.

The problem this solves
-----------------------
Everything else in NES is held by the office that issued the document. The
seal, the audit chain, the anchor, the register — all of it lives on one
server, under one key. That is strong against a careless administrator and
weak against a determined one, because whoever holds both the database and the
signing key can rebuild a consistent story.

Witnessing removes the single point of trust. When an office seals a document,
every participating country office receives a small signed statement: *this
envelope, this document hash, this audit-chain head, at this time*. Later,
anyone can ask the other offices what they hold. To forge a document
convincingly you would now have to alter it in every participating office at
once, in each office's own database, each of which you do not control.

That is the same reason a land registry keeps copies in more than one
building.

What is shared, and what is not
-------------------------------
**Fingerprints only** — hashes, the envelope identifier, timestamps. Not the
document, not the signature images, not the field values, not recipient
addresses. A UN envelope may be confidential to one office; shipping the PDF
to five others to protect it would create a worse problem than the one being
solved. A SHA-256 is enough to *prove* tampering and reveals nothing about
content.

The consequence, stated plainly: witnessing **detects and proves** alteration.
It does not restore the file. Restoration comes from backups and from evidence
packages archived at completion.

Re-sealing is expected
----------------------
An archival refresh adds a document timestamp, which legitimately changes the
file's hash. So records are a *history*, not a single value: each one attests
to the document as it stood at that moment, with `kind` saying why it changed.
Verification compares the live file against the most recent attestation and
treats a hash matching no attestation at all as the alarm.

Install
-------
Add ONE line at the bottom of accounts/models.py:

    from .models_esign_witness import *  # noqa: F401,F403

Then:  python manage.py makemigrations accounts && python manage.py migrate
"""

from django.db import models
from django.utils import timezone

__all__ = [
    "WitnessPeer",
    "WitnessRecord",
    "WitnessDelivery",
    "WitnessAlert",
]


class WitnessPeer(models.Model):
    """
    A participating country office.

    Two tokens, deliberately. `outbound_token` is what we present when pushing
    to them; `inbound_token` is what we require from them. Separate secrets
    mean a leak in one direction does not grant the other, and either can be
    rotated alone.

    The token authenticates the *connection*. What authenticates the
    *statement* is `certificate_pem` — the peer's seal certificate, which must
    match the certificate that actually signed any statement we accept. A
    stolen token therefore buys an attacker nothing: they still cannot produce
    a statement the peer's key would have signed.
    """

    code = models.CharField(
        max_length=32, unique=True, db_index=True,
        help_text="Short office code, e.g. UNDP-GM. Appears in statements.",
    )
    name = models.CharField(max_length=160)
    base_url = models.URLField(
        max_length=300,
        help_text="Root URL of that office's UN PASS, e.g. https://unpass.sn",
    )

    outbound_token = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Bearer token we present when pushing to this peer.",
    )
    inbound_token = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Bearer token we require when this peer pushes to us.",
    )
    certificate_pem = models.TextField(
        blank=True, default="",
        help_text="This peer's seal certificate (PEM). Statements are only "
                  "accepted if signed by it.",
    )

    active = models.BooleanField(default=True)
    accepts_push = models.BooleanField(
        default=True, help_text="Send our sealed-document fingerprints here.",
    )
    answers_lookup = models.BooleanField(
        default=True, help_text="Query this peer when verifying a document.",
    )

    last_contact_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "NES witness peer"
        verbose_name_plural = "NES witness peers"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"

    @property
    def receive_url(self) -> str:
        return self.base_url.rstrip("/") + "/accounts/esign/witness/receive/"

    @property
    def lookup_url(self) -> str:
        return self.base_url.rstrip("/") + "/accounts/esign/witness/lookup/"

    @property
    def is_configured(self) -> bool:
        """Usable in both directions and able to authenticate statements."""
        return bool(self.base_url and self.certificate_pem)


class WitnessRecord(models.Model):
    """
    One signed attestation: a document's fingerprint at a point in time.

    Held both for our own envelopes (so we know what we told the world) and
    for peers' envelopes (so we can answer for them). `received_from` is null
    for our own.
    """

    KIND_SEAL = "seal"
    KIND_REFRESH = "refresh"
    KIND_CHOICES = [
        (KIND_SEAL, "Sealed"),
        (KIND_REFRESH, "Archival refresh"),
    ]

    origin_code = models.CharField(
        max_length=32, db_index=True,
        help_text="Office that sealed the document.",
    )
    envelope_uid = models.CharField(
        max_length=64, db_index=True,
        help_text="The envelope_id of the sealed envelope — globally unique.",
    )
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_SEAL)

    #: Deliberately minimal metadata. Enough to recognise the record in a
    #: list; not enough to learn anything about the document's contents.
    reference = models.CharField(max_length=120, blank=True, default="")

    document_sha256 = models.CharField(max_length=64, db_index=True)
    certificate_sha256 = models.CharField(max_length=64, blank=True, default="")
    chain_head = models.CharField(max_length=64, blank=True, default="")
    chain_length = models.PositiveIntegerField(default=0)
    seal_level = models.CharField(max_length=8, blank=True, default="")

    sealed_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(default=timezone.now)

    statement = models.TextField(
        help_text="The exact canonical text that was signed.",
    )
    signature_b64 = models.TextField(
        blank=True, default="",
        help_text="Base64 detached CMS signature over `statement`.",
    )

    received_from = models.ForeignKey(
        "accounts.WitnessPeer",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="records",
        help_text="Null when this is our own record.",
    )
    received_at = models.DateTimeField(null=True, blank=True)
    signature_verified = models.BooleanField(default=False)
    verify_detail = models.TextField(blank=True, default="")

    local_envelope = models.ForeignKey(
        "accounts.Envelope",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="witness_records",
        help_text="Set when the record concerns an envelope held here.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "NES witness record"
        verbose_name_plural = "NES witness records"
        ordering = ["-issued_at", "-id"]
        indexes = [
            models.Index(fields=["origin_code", "envelope_uid"]),
        ]
        constraints = [
            # The same attestation arriving twice is a no-op, not a duplicate.
            models.UniqueConstraint(
                fields=["origin_code", "envelope_uid", "document_sha256", "kind"],
                name="nes_witness_unique_attestation",
            ),
        ]

    def __str__(self):
        return f"{self.origin_code}/{self.envelope_uid[:12]} {self.document_sha256[:12]}"

    @property
    def is_ours(self) -> bool:
        return self.received_from_id is None


class WitnessDelivery(models.Model):
    """
    Outbound queue entry.

    Deliveries are queued, never sent from inside the signing path. Sealing an
    envelope must not wait on five HTTP calls to other countries, and must not
    fail because one of them is down.
    """

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
    ]

    record = models.ForeignKey(
        WitnessRecord, on_delete=models.CASCADE, related_name="deliveries"
    )
    peer = models.ForeignKey(
        WitnessPeer, on_delete=models.CASCADE, related_name="deliveries"
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "NES witness delivery"
        verbose_name_plural = "NES witness deliveries"
        ordering = ["status", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["record", "peer"], name="nes_witness_delivery_once"
            ),
        ]

    def __str__(self):
        return f"{self.peer.code} <- {self.record.envelope_uid[:12]} ({self.status})"


class WitnessAlert(models.Model):
    """
    A divergence worth someone's attention.

    Raised by the audit, not by a page view, so it survives nobody looking.
    Kept after resolution because the fact that an alarm once fired is itself
    part of the record.
    """

    KIND_HASH_MISMATCH = "hash_mismatch"
    KIND_NO_ATTESTATION = "no_attestation"
    KIND_PEER_DISAGREES = "peer_disagrees"
    KIND_CHAIN_MISMATCH = "chain_mismatch"
    KIND_NOT_WITNESSED = "not_witnessed"
    KIND_PEER_UNREACHABLE = "peer_unreachable"
    KIND_BAD_SIGNATURE = "bad_signature"
    KIND_CHOICES = [
        (KIND_HASH_MISMATCH, "Stored file does not match any attestation"),
        (KIND_NO_ATTESTATION, "No attestation exists for this document"),
        (KIND_PEER_DISAGREES, "A peer holds a different fingerprint"),
        (KIND_CHAIN_MISMATCH, "Audit-chain head disagrees with the attestation"),
        (KIND_NOT_WITNESSED, "Never distributed to any peer"),
        (KIND_PEER_UNREACHABLE, "A peer could not be reached"),
        (KIND_BAD_SIGNATURE, "An attestation signature did not validate"),
    ]

    SEVERITY_INFO = "info"
    SEVERITY_WARNING = "warning"
    SEVERITY_CRITICAL = "critical"
    SEVERITY_CHOICES = [
        (SEVERITY_INFO, "Information"),
        (SEVERITY_WARNING, "Warning"),
        (SEVERITY_CRITICAL, "Critical"),
    ]

    kind = models.CharField(max_length=24, choices=KIND_CHOICES, db_index=True)
    severity = models.CharField(
        max_length=10, choices=SEVERITY_CHOICES, default=SEVERITY_WARNING,
        db_index=True,
    )

    origin_code = models.CharField(max_length=32, blank=True, default="")
    envelope_uid = models.CharField(max_length=64, blank=True, default="", db_index=True)
    local_envelope = models.ForeignKey(
        "accounts.Envelope",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="witness_alerts",
    )
    peer = models.ForeignKey(
        WitnessPeer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="alerts",
    )

    detail = models.TextField(blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)

    raised_at = models.DateTimeField(default=timezone.now, db_index=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    seen = models.BooleanField(default=False)

    resolved = models.BooleanField(default=False, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_note = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "NES witness alert"
        verbose_name_plural = "NES witness alerts"
        ordering = ["-raised_at"]

    def __str__(self):
        return f"[{self.severity}] {self.get_kind_display()} {self.envelope_uid[:12]}"

    @property
    def status_color(self) -> str:
        if self.resolved:
            return "secondary"
        return {
            self.SEVERITY_CRITICAL: "danger",
            self.SEVERITY_WARNING: "warning",
            self.SEVERITY_INFO: "info",
        }.get(self.severity, "secondary")
