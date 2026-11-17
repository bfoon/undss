# accounts/models_esign_pades.py
"""
UN PASS — NES PAdES seal records (ETSI EN 319 142).

What this adds
--------------
NES already produces a flattened `completed_pdf` with every signature drawn
onto the page, plus a Certificate of Completion describing who did what and
when. That is strong evidence, but it is *visual* evidence: nothing in the file
itself proves the bytes were not edited afterwards, and no PDF reader can tell
the recipient the document is authentic.

PAdES (PDF Advanced Electronic Signatures) fixes that by embedding a
cryptographic signature *inside* the PDF, covering the whole byte range. Adobe
Acrobat, Foxit and any ETSI-conformant validator then show the document as
signed and unmodified, with the signing organisation named — and they keep
showing it after the file has been emailed, downloaded or archived, with no
access to UN PASS at all.

This module stores what was done, per envelope. The PDF itself carries the
signature; this table is the queryable record of it, so an administrator can
answer "is every completed envelope this quarter sealed, and at what level?"
without opening 400 files.

Conformance levels (ETSI EN 319 142-1, baseline profile)
--------------------------------------------------------
    B-B    signature + signing certificate. Proves integrity and origin.
    B-T    adds a trusted timestamp from a TSA (RFC 3161), proving *when*.
    B-LT   adds the revocation data (OCSP/CRL) needed to validate it later,
           embedded in the PDF's DSS. Long-term: still verifiable after the
           signing certificate expires.
    B-LTA  adds a document timestamp over the whole thing, so the chain can be
           refreshed indefinitely. Archival.

NES targets B-LT by default when a timestamp authority is configured, and
degrades cleanly: B-T without revocation data, B-B without a TSA. The level
actually achieved is recorded here rather than assumed.

Design notes
------------
* No import from models_esign.py — the relation is a lazy string reference, so
  accounts/models.py can import this module in any order.
* One row per envelope (OneToOne). Re-sealing overwrites it.
* `status` is deliberately recorded even on failure. A completed envelope whose
  seal failed is still a valid NES envelope with a full audit trail; the seal
  is an enhancement, never a gate. Silent failure would be worse than a row
  that says "failed, and here is why".

Install
-------
Add ONE line at the bottom of accounts/models.py, after the existing
`from .models_esign import *`:

    from .models_esign_pades import *  # noqa: F401,F403

Then:  python manage.py makemigrations accounts && python manage.py migrate
"""

from django.db import models
from django.utils import timezone

__all__ = [
    "PADES_LEVELS",
    "EnvelopeSeal",
]


# ETSI EN 319 142-1 baseline levels, weakest first. The order matters:
# `EnvelopeSeal.level_rank` uses it to compare what was achieved against what
# was asked for.
PADES_LEVELS = [
    ("b-b", "PAdES B-B — signature and certificate"),
    ("b-t", "PAdES B-T — with trusted timestamp"),
    ("b-lt", "PAdES B-LT — with long-term validation data"),
    ("b-lta", "PAdES B-LTA — with archival document timestamp"),
]

_LEVEL_ORDER = [code for code, _label in PADES_LEVELS]


class EnvelopeSeal(models.Model):
    """The PAdES seal applied to one envelope's completed PDF."""

    STATUS_SEALED = "sealed"
    STATUS_SKIPPED = "skipped"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_SEALED, "Sealed"),
        (STATUS_SKIPPED, "Not sealed — not configured"),
        (STATUS_FAILED, "Seal failed"),
    ]

    envelope = models.OneToOneField(
        "accounts.Envelope",
        on_delete=models.CASCADE,
        related_name="seal",
    )

    status = models.CharField(
        max_length=12,
        choices=STATUS_CHOICES,
        default=STATUS_SKIPPED,
        db_index=True,
    )

    # What was asked for, and what the signing run actually achieved. They
    # differ whenever a TSA or OCSP responder was unreachable.
    level_requested = models.CharField(
        max_length=8, choices=PADES_LEVELS, blank=True, default=""
    )
    level_achieved = models.CharField(
        max_length=8, choices=PADES_LEVELS, blank=True, default="", db_index=True
    )

    applied_at = models.DateTimeField(null=True, blank=True)

    # -- the signature itself -------------------------------------------
    field_name = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Name of the PDF signature field holding the seal.",
    )
    digest_algorithm = models.CharField(max_length=16, blank=True, default="")
    document_sha256 = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="SHA-256 of the sealed PDF as stored. Lets an auditor "
                  "confirm a downloaded copy is byte-identical.",
    )

    # -- the signing certificate ----------------------------------------
    signer_common_name = models.CharField(max_length=200, blank=True, default="")
    signer_organization = models.CharField(max_length=200, blank=True, default="")
    signer_serial = models.CharField(max_length=80, blank=True, default="")
    signer_issuer = models.CharField(max_length=300, blank=True, default="")
    certificate_expires_at = models.DateTimeField(null=True, blank=True)

    # -- timestamping ---------------------------------------------------
    timestamped = models.BooleanField(default=False)
    timestamp_authority = models.CharField(max_length=300, blank=True, default="")

    # -- the Certificate of Completion -----------------------------------
    #: The certificate is the human-readable record of who signed and when, so
    #: leaving it unsealed would protect the document and not the evidence
    #: about it. Sealed with the same key, recorded separately.
    certificate_sealed = models.BooleanField(default=False)
    certificate_sha256 = models.CharField(max_length=64, blank=True, default="")

    # -- declared policy (ETSI signed attributes) ------------------------
    policy_oid = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Signature policy OID asserted in the signature.",
    )
    policy_uri = models.CharField(
        max_length=300, blank=True, default="",
        help_text="Where that policy document is published.",
    )
    commitment_type = models.CharField(
        max_length=40, blank=True, default="",
        help_text="What the seal asserts, e.g. proof_of_origin.",
    )

    # -- long-term validity maintenance ----------------------------------
    #: A signature stays verifiable only while its validation material can
    #: still be assembled. Document timestamps extend that window; each
    #: refresh adds one. See pades_esign.refresh_ltv().
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    refresh_count = models.PositiveIntegerField(default=0)
    next_refresh_due = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="When this seal should next be re-timestamped.",
    )
    refresh_detail = models.TextField(blank=True, default="")

    # -- audit-trail anchor ----------------------------------------------
    #: Head and length of the hash chain at completion, plus a detached CMS
    #: signature over that statement. Without these, entries removed from the
    #: END of the trail would leave a chain that is still self-consistent.
    audit_chain_head = models.CharField(max_length=64, blank=True, default="")
    audit_chain_length = models.PositiveIntegerField(default=0)
    audit_anchor = models.TextField(
        blank=True, default="",
        help_text="Base64 detached CMS signature over the chain statement.",
    )
    audit_anchored_at = models.DateTimeField(null=True, blank=True)
    audit_anchor_timestamped = models.BooleanField(default=False)
    audit_detail = models.TextField(blank=True, default="")

    # -- anything worth saying in plain words ---------------------------
    detail = models.TextField(
        blank=True,
        default="",
        help_text="Why a seal was skipped, or how it failed.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "NES document seal"
        verbose_name_plural = "NES document seals"
        ordering = ["-applied_at", "-created_at"]

    def __str__(self):
        label = self.get_level_achieved_display() or self.get_status_display()
        return f"{self.envelope_id}: {label}"

    # -- display helpers ------------------------------------------------
    @property
    def is_sealed(self) -> bool:
        return self.status == self.STATUS_SEALED

    @property
    def level_rank(self) -> int:
        """Position of the achieved level, or -1 when nothing was achieved."""
        try:
            return _LEVEL_ORDER.index(self.level_achieved)
        except ValueError:
            return -1

    @property
    def met_target(self) -> bool:
        """True when the run reached the level that was asked for."""
        if not self.level_requested or not self.level_achieved:
            return False
        try:
            return _LEVEL_ORDER.index(self.level_achieved) >= _LEVEL_ORDER.index(
                self.level_requested
            )
        except ValueError:
            return False

    @property
    def status_color(self) -> str:
        if self.status == self.STATUS_SEALED:
            return "success" if self.met_target else "warning"
        if self.status == self.STATUS_FAILED:
            return "danger"
        return "secondary"

    @property
    def short_level(self) -> str:
        """`PAdES B-LT` — the bit that belongs on a badge."""
        if not self.level_achieved:
            return ""
        return "PAdES " + self.level_achieved.upper().replace("B-", "B-")

    @property
    def certificate_expired(self) -> bool:
        return bool(
            self.certificate_expires_at
            and self.certificate_expires_at < timezone.now()
        )

    # -- long-term validity ---------------------------------------------
    @property
    def refresh_overdue(self) -> bool:
        return bool(
            self.next_refresh_due and self.next_refresh_due < timezone.now()
        )

    @property
    def is_archival(self) -> bool:
        """B-LTA: carries a document timestamp chain that can be extended."""
        return self.level_achieved == "b-lta"

    @property
    def long_term_outlook(self) -> str:
        """
        One sentence on how long this seal can be expected to verify. The
        honest answer differs sharply by level, and "verifiable" without that
        qualifier is the thing auditors most often assume wrongly.
        """
        if not self.is_sealed:
            return "Not sealed — nothing to verify cryptographically."
        if self.level_achieved == "b-lta":
            return (
                "Archival. The document timestamp chain can be extended "
                "indefinitely, so this stays verifiable as long as it is "
                "refreshed."
            )
        if self.level_achieved == "b-lt":
            return (
                "Long-term. Revocation data is embedded, so this verifies "
                "after the signing certificate expires. Refresh to reach "
                "archival."
            )
        if self.level_achieved == "b-t":
            return (
                "Timestamped. Signing time is proven, but revocation data is "
                "not embedded, so validation gets harder once the "
                "certificate expires."
            )
        return (
            "Integrity only. Tampering is detectable, but there is no proof "
            "of signing time and no embedded validation data — expect this to "
            "become hard to validate after the certificate expires."
        )

    # -- audit anchor ---------------------------------------------------
    @property
    def has_audit_anchor(self) -> bool:
        return bool(self.audit_chain_head and self.audit_anchor)
