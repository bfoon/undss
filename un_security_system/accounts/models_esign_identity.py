# accounts/models_esign_identity.py
"""
UN PASS — NES signing identities, and sole control.

The gap this closes
-------------------
UNDP's Electronic Signatures Policy requires that a signatory be a person who
can be uniquely identified and linked to the signature, and **who has sole
control over the authentication key**. It states that an electronic signature
is personal and cannot be shared.

Until now NES stored a picture of a signature. A picture is not sole control:
the server held it, an administrator could apply it, and the policy explicitly
says a pasted image is not an electronic signature at all. The PAdES seal did
not fix this either — it is the *organisation* attesting to the record, which
is why its commitment type is `proof_of_origin` and not `proof_of_approval`.

So each person now has a signing key that **the server cannot use**:

* The keypair is generated in the signatory's browser and never leaves it.
* The private key is encrypted there, under a key derived from a passphrase
  only they know, before anything is uploaded.
* The server stores a public key and an opaque encrypted blob. It has no path
  to the private key — not an administrator, not the database, not a backup.
* Signing happens in the browser: the server issues a challenge, the browser
  decrypts the key with the passphrase and signs, and only the signature
  comes back. **The passphrase never reaches the server.**

That is sole control in the sense the policy means: the key is under the
signatory's control alone, and the signature cannot be produced by anyone
else, including us.

The identifier
--------------
One identity per person, one identifier for life:

    NES-GM-2BE9-W79Q-MTVY-TC

It names the *person's signing identity*, not a particular mark or key. They
may redraw their signature whenever they like and rotate the key after
forgetting a passphrase; the identifier does not move, so every document they
have ever signed still points to the same person. See signature_id.py for how
it is derived and why.

The identifier is immutable. Not by convention — `save()` refuses to change
it, and `manage.py esign_identity --install-guard` installs a database trigger
that rejects the UPDATE outright, because `QuerySet.update()` and raw SQL both
bypass model code.

History, not replacement
------------------------
Changing a mark or rotating a key creates a new *revision*; the old one is
retained and marked not-current. This is not tidiness. A document signed in
2026 must still verify in 2032 against the key that was current in 2026, and
the certificate of completion must be able to show the mark that was actually
applied. Overwriting would quietly invalidate the past.

Install
-------
Add ONE line at the bottom of accounts/models.py:

    from .models_esign_identity import *  # noqa: F401,F403

Then:  python manage.py makemigrations accounts && python manage.py migrate
       python manage.py esign_identity --install-guard
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

__all__ = [
    "SignatureIdentity",
    "SignatureKey",
    "SignatureMark",
    "SignatureAttestation",
]


class SignatureIdentity(models.Model):
    """
    One person's signing identity. One per user, for the life of the account.

    The OneToOne is the enforcement of "a user may have only one signature":
    the database will not hold a second.
    """

    STATUS_ACTIVE = "active"
    STATUS_SUSPENDED = "suspended"
    STATUS_RETIRED = "retired"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_SUSPENDED, "Suspended"),
        (STATUS_RETIRED, "Retired"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="signature_identity",
        editable=False,
    )

    #: The lifetime identifier. Never changes — not when the mark changes, not
    #: when the key is rotated, not when the person's name or office changes.
    signature_id = models.CharField(
        max_length=48, unique=True, editable=False, db_index=True,
    )

    #: Fixed at enrolment. With the office secret, lets the office recompute
    #: `signature_id` and show it issued it rather than someone inventing one.
    enrollment_nonce = models.CharField(max_length=64, editable=False)
    office_code = models.CharField(max_length=16, editable=False, default="XX")
    issued_at = models.DateTimeField(editable=False)

    #: The person's name as it stood at enrolment. Snapshotted because the
    #: certificate of completion should show who signed then, not who the
    #: account belongs to now.
    enrolled_name = models.CharField(max_length=200, blank=True, default="")
    enrolled_email = models.EmailField(blank=True, default="")

    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_ACTIVE,
        db_index=True,
    )
    suspended_reason = models.TextField(blank=True, default="")
    status_changed_at = models.DateTimeField(null=True, blank=True)

    key_revision = models.PositiveIntegerField(default=0)
    mark_revision = models.PositiveIntegerField(default=0)

    last_used_at = models.DateTimeField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "NES signing identity"
        verbose_name_plural = "NES signing identities"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.signature_id} — {self.enrolled_name or self.user}"

    # -- immutability ---------------------------------------------------
    #: Fields that are settled at enrolment and may never change afterwards.
    #: `save()` refuses; the database trigger refuses harder.
    IMMUTABLE_FIELDS = ("signature_id", "enrollment_nonce", "issued_at",
                        "office_code", "user_id")

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                *self.IMMUTABLE_FIELDS
            ).first()
            if previous:
                changed = [
                    f for f in self.IMMUTABLE_FIELDS
                    if previous[f] != getattr(self, f)
                ]
                if changed:
                    raise ValidationError(
                        "A signing identity is issued once and cannot be "
                        f"altered. Refused a change to: {', '.join(changed)}. "
                        "To give someone a different key, rotate the key — "
                        "the identifier stays the same on purpose."
                    )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "A signing identity cannot be deleted — documents already signed "
            "refer to it. Retire it instead."
        )

    # -- current revisions ----------------------------------------------
    @property
    def current_key(self):
        return self.keys.filter(is_current=True).first()

    @property
    def current_mark(self):
        return self.marks.filter(is_current=True).first()

    @property
    def is_usable(self) -> bool:
        """Enrolled, active, and holding both a key and a mark."""
        return bool(
            self.status == self.STATUS_ACTIVE
            and self.current_key is not None
            and self.current_mark is not None
        )

    @property
    def status_color(self) -> str:
        return {
            self.STATUS_ACTIVE: "success",
            self.STATUS_SUSPENDED: "warning",
            self.STATUS_RETIRED: "secondary",
        }.get(self.status, "secondary")

    @property
    def short_id(self) -> str:
        """The distinctive part, for tight spaces: `2BE9-W79Q-MTVY-TC`."""
        parts = self.signature_id.split("-")
        return "-".join(parts[2:]) if len(parts) > 2 else self.signature_id


class SignatureKey(models.Model):
    """
    One revision of a person's signing keypair.

    What is stored here is, deliberately, useless on its own:

    * `public_key_spki` — public, used to verify.
    * `encrypted_private_key` — AES-GCM ciphertext the server has no key for.
    * `kdf_salt`, `kdf_iterations`, `cipher_iv` — parameters the *browser*
      needs to re-derive the unwrapping key from the passphrase.

    The passphrase is never sent, never stored, and never recoverable. A
    forgotten passphrase means rotating to a new key, which is why old keys
    are kept: documents signed under them must still verify.
    """

    identity = models.ForeignKey(
        SignatureIdentity, on_delete=models.CASCADE, related_name="keys",
        editable=False,
    )
    revision = models.PositiveIntegerField(editable=False)

    algorithm = models.CharField(max_length=32, default="ECDSA-P256-SHA256")
    public_key_spki = models.TextField(
        editable=False,
        help_text="Base64 DER SubjectPublicKeyInfo, as exported by WebCrypto.",
    )
    key_fingerprint = models.CharField(
        max_length=64, editable=False, db_index=True,
        help_text="SHA-256 of the public key DER.",
    )

    encrypted_private_key = models.TextField(
        editable=False,
        help_text="Base64 AES-GCM ciphertext of the PKCS8 private key, "
                  "encrypted in the browser. Opaque to this server.",
    )
    kdf = models.CharField(max_length=24, default="PBKDF2-SHA256")
    kdf_salt = models.CharField(max_length=64, editable=False)
    kdf_iterations = models.PositiveIntegerField(default=600000)
    cipher = models.CharField(max_length=24, default="AES-GCM-256")
    cipher_iv = models.CharField(max_length=48, editable=False)

    #: A short hint the person chose, to jog their memory. Never the
    #: passphrase, and never anything derived from it.
    passphrase_hint = models.CharField(max_length=120, blank=True, default="")

    is_current = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    retired_at = models.DateTimeField(null=True, blank=True)
    retired_reason = models.CharField(max_length=200, blank=True, default="")

    created_ip = models.GenericIPAddressField(null=True, blank=True)
    created_user_agent = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        verbose_name = "NES signing key"
        verbose_name_plural = "NES signing keys"
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["identity", "revision"], name="nes_key_revision_unique"
            ),
        ]

    def __str__(self):
        return f"{self.identity.signature_id} key r{self.revision}"

    def save(self, *args, **kwargs):
        if not self.key_fingerprint and self.public_key_spki:
            self.key_fingerprint = self.fingerprint_of(self.public_key_spki)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Signing keys are never deleted — signatures made with them must "
            "stay verifiable. Retire the key instead."
        )

    @staticmethod
    def fingerprint_of(public_key_spki_b64: str) -> str:
        import base64
        try:
            return hashlib.sha256(
                base64.b64decode(public_key_spki_b64)
            ).hexdigest()
        except Exception:
            return ""

    @property
    def short_fingerprint(self) -> str:
        fp = self.key_fingerprint or ""
        return " ".join(fp[i:i + 4] for i in range(0, 16, 4)).upper()


class SignatureMark(models.Model):
    """
    One revision of the visual mark — what actually gets drawn on the page.

    Changing it is a new revision, not an edit. The mark that was stamped onto
    a document in 2026 must remain recoverable even after the person redraws
    it, or the certificate of completion would show something that was never
    applied.

    The mark is *presentation*. The evidential weight sits in the key and the
    attestation; this is what a reader sees.
    """

    KIND_TYPED = "typed"
    KIND_DRAWN = "drawn"
    KIND_UPLOADED = "uploaded"
    KIND_CHOICES = [
        (KIND_TYPED, "Typed"),
        (KIND_DRAWN, "Hand drawn"),
        (KIND_UPLOADED, "Uploaded image"),
    ]

    identity = models.ForeignKey(
        SignatureIdentity, on_delete=models.CASCADE, related_name="marks",
        editable=False,
    )
    revision = models.PositiveIntegerField(editable=False)

    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_TYPED)
    typed_text = models.CharField(max_length=120, blank=True, default="")
    initials_text = models.CharField(max_length=12, blank=True, default="")
    font_key = models.CharField(max_length=24, blank=True, default="dancing")

    image = models.ImageField(upload_to="esign/identity/%Y/%m/")
    initials_image = models.ImageField(
        upload_to="esign/identity/%Y/%m/", blank=True, null=True
    )
    image_sha256 = models.CharField(max_length=64, blank=True, default="")

    is_current = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    replaced_at = models.DateTimeField(null=True, blank=True)

    created_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = "NES signature mark"
        verbose_name_plural = "NES signature marks"
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["identity", "revision"], name="nes_mark_revision_unique"
            ),
        ]

    def __str__(self):
        return f"{self.identity.signature_id} mark r{self.revision}"

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Signature marks are never deleted — documents stamped with them "
            "must stay explainable."
        )


class SignatureAttestation(models.Model):
    """
    One signatory's cryptographic act of signing one envelope.

    This is the thing that makes a NES signature *theirs* rather than a
    picture the server drew. It records the exact statement the browser
    signed, the signature over it, and which key revision produced it.

    Fields are denormalised on purpose: `signature_id`, `signer_name` and
    `key_fingerprint` are copied in, so the record still reads correctly if
    the account is later renamed, or the identity retired.
    """

    identity = models.ForeignKey(
        SignatureIdentity, on_delete=models.PROTECT,
        related_name="attestations", null=True, blank=True,
    )
    key = models.ForeignKey(
        SignatureKey, on_delete=models.PROTECT,
        related_name="attestations", null=True, blank=True,
    )
    envelope = models.ForeignKey(
        "accounts.Envelope", on_delete=models.CASCADE,
        related_name="attestations",
    )
    recipient = models.ForeignKey(
        "accounts.EnvelopeRecipient", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="attestations",
    )

    signature_id = models.CharField(max_length=48, db_index=True)
    signer_name = models.CharField(max_length=200, blank=True, default="")
    signer_email = models.EmailField(blank=True, default="")
    key_fingerprint = models.CharField(max_length=64, blank=True, default="")
    algorithm = models.CharField(max_length=32, default="ECDSA-P256-SHA256")

    #: The exact text the browser signed. Kept verbatim so verification never
    #: depends on rebuilding it from parts that may have drifted.
    statement = models.TextField()
    signature_b64 = models.TextField(
        help_text="Base64 IEEE P1363 (r||s) signature, as WebCrypto emits it."
    )
    nonce = models.CharField(max_length=64, db_index=True)
    document_sha256 = models.CharField(max_length=64, blank=True, default="")

    verified = models.BooleanField(default=False, db_index=True)
    verify_detail = models.TextField(blank=True, default="")
    verified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        verbose_name = "NES signature attestation"
        verbose_name_plural = "NES signature attestations"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["envelope", "signature_id"]),
        ]

    def __str__(self):
        return f"{self.signature_id} on {self.envelope_id}"

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Attestations are evidence and are not deleted."
        )

    @property
    def status_color(self) -> str:
        return "success" if self.verified else "danger"
