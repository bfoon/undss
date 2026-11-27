# accounts/models_esign_sentinel.py
"""
UN PASS — NES Sentinel: independent tamper witnesses.

Why this exists
---------------
Everything that proves a NES document is genuine — the seal, the audit chain,
the anchor — lives on one server. The ledger guard stops the application from
rewriting it; it cannot stop someone who owns the server. Cross-office
witnessing (models_esign_witness.py) needs another full UN PASS install.

A Sentinel is the small version of that: a ~1 MB program a participating
country office installs on any machine it controls — a cloud VM, a server in
its own building, a laptop. It pulls every block of the NES ledger, checks the
server's signature on each one, keeps its own copy, and keeps asking the
server to prove that the files and trails still match what it was told. When
they do not, it raises the alarm itself — to the server, to the document's
owner and watchers, and to its own office — so silencing UN PASS does not
silence the alarm.

The ledger
----------
`SentinelBlock` is an append-only, hash-linked, server-signed log of every
audit-trail entry and every seal fingerprint. It holds hashes and identifiers
only: no names, no file content, no subject lines. `prev_hash` is UNIQUE, so
the database itself refuses a fork, and the ledger guard refuses edits.

Install
-------
Add ONE line at the bottom of accounts/models.py:

    from .models_esign_sentinel import *  # noqa: F401,F403

Then (as the schema owner):  scripts/owner.sh makemigrations accounts
                              scripts/owner.sh migrate
                              scripts/owner.sh nes_sentinel keygen
                              scripts/owner.sh unpass_harden_db --install-guards
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

__all__ = [
    "SentinelNode",
    "SentinelEnrolment",
    "SentinelBlock",
    "SentinelNonce",
    "SentinelAlert",
    "EnvelopeWatcher",
]

GENESIS = "0" * 64


class SentinelNode(models.Model):
    """One installed Sentinel. It is known by its public key, nothing else."""

    STATUS_ACTIVE = "active"
    STATUS_REVOKED = "revoked"
    STATUSES = [(STATUS_ACTIVE, "Active"), (STATUS_REVOKED, "Revoked")]

    name = models.CharField(max_length=120)
    office_code = models.CharField(max_length=40, blank=True, default="")
    #: Raw 32-byte Ed25519 public key, base64. The node keeps the private half;
    #: it never leaves the machine and the server never sees it.
    public_key = models.CharField(max_length=64, unique=True)
    key_fingerprint = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=STATUS_ACTIVE)

    enrolled_at = models.DateTimeField(default=timezone.now)
    enrolled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    last_seq = models.BigIntegerField(default=0)
    platform = models.CharField(max_length=80, blank=True, default="")
    version = models.CharField(max_length=20, blank=True, default="")
    #: What the node said in its last heartbeat (counts, last audit time).
    stats = models.JSONField(default=dict, blank=True)
    silent_alerted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.key_fingerprint[:12]})"

    @property
    def is_active(self) -> bool:
        return self.status == self.STATUS_ACTIVE


class SentinelEnrolment(models.Model):
    """
    A one-time code ICT hands to the office installing a Sentinel.

    Only the SHA-256 of the code is stored, so a database reader cannot use
    an outstanding code. Codes expire and work once.
    """

    code_hash = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    office_code = models.CharField(max_length=40, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    node = models.ForeignKey(
        SentinelNode, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()


class SentinelBlock(models.Model):
    """
    One entry of the published ledger.

        block_hash = sha256("nes-sentinel-block-v1\\n" seq "\\n" prev_hash "\\n" body)
        signature  = Ed25519(server publication key, block_hash)

    `body` is canonical JSON (sorted keys, no spaces) and is stored as text,
    exactly as hashed, so nobody has to reproduce a serializer to verify it.
    """

    KIND_EVENT = "event"
    KIND_SEAL = "seal"
    KINDS = [(KIND_EVENT, "Audit entry"), (KIND_SEAL, "Seal fingerprint")]

    seq = models.BigIntegerField(unique=True)
    kind = models.CharField(max_length=8, choices=KINDS)
    envelope_uid = models.CharField(max_length=32, db_index=True)
    #: The source row's identity: an event id, or a seal's document hash.
    ref = models.CharField(max_length=64)
    body = models.TextField()
    prev_hash = models.CharField(max_length=64, unique=True)
    block_hash = models.CharField(max_length=64, unique=True)
    signature_b64 = models.CharField(max_length=100)
    key_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["seq"]
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "envelope_uid", "ref"], name="nes_sentinel_block_once",
            ),
        ]

    def as_wire(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "envelope": self.envelope_uid,
            "body": self.body,
            "prev_hash": self.prev_hash,
            "block_hash": self.block_hash,
            "signature": self.signature_b64,
        }


class SentinelNonce(models.Model):
    """Request nonces seen in the last few minutes — a replayed request fails."""

    node = models.ForeignKey(SentinelNode, on_delete=models.CASCADE, related_name="+")
    nonce = models.CharField(max_length=64)
    seen_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["node", "nonce"], name="nes_sentinel_nonce_once"),
        ]


class SentinelAlert(models.Model):
    """Something a Sentinel (or the server, about a Sentinel) saw go wrong."""

    SEV_CRITICAL = "critical"
    SEV_WARNING = "warning"
    SEVERITIES = [(SEV_CRITICAL, "Critical"), (SEV_WARNING, "Warning")]

    node = models.ForeignKey(
        SentinelNode, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="alerts",
    )
    envelope_uid = models.CharField(max_length=32, blank=True, default="", db_index=True)
    kind = models.CharField(max_length=40)
    severity = models.CharField(max_length=10, choices=SEVERITIES, default=SEV_CRITICAL)
    detail = models.TextField(blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)
    #: Owner and watchers as the Sentinel last saw them, kept so the alarm
    #: still reaches them when the envelope itself was deleted. Only
    #: addresses of active UN PASS users are kept (see record_alert).
    contacts = models.JSONField(default=list, blank=True)
    #: Same finding from the same node is one alert, however often it is sent.
    fingerprint = models.CharField(max_length=64, db_index=True)
    reported_at = models.DateTimeField(default=timezone.now, db_index=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    resolution_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-reported_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["node", "fingerprint"], name="nes_sentinel_alert_once",
            ),
        ]


class EnvelopeWatcher(models.Model):
    """Someone the owner asked to be told if this document is tampered with."""

    envelope = models.ForeignKey(
        "accounts.Envelope", on_delete=models.CASCADE, related_name="watchers",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+",
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    added_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["added_at"]
        constraints = [
            models.UniqueConstraint(fields=["envelope", "user"], name="nes_watcher_once"),
        ]
