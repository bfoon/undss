# accounts/identity_esign.py
"""
UN PASS — NES signing identity service.

Everything that creates or changes a signing identity goes through here, and
every function that changes anything takes the acting user and checks it.

The one rule
------------
**Only the owner.** Not an administrator, not ICT, not a superuser, not the
person who set up the envelope. A signature that someone else can change is
not that person's signature, and the policy this implements says so in as many
words: an electronic signature is personal and cannot be shared.

So the guard is a hard failure, not a permission check that an escalated role
can satisfy. There is no `can_manage_other_identity` flag anywhere, because
the moment one exists somebody will be granted it.

What the server can and cannot do
---------------------------------
Can: issue an identifier, store a public key, store an opaque blob, verify a
signature, retire or suspend an identity.

Cannot: read a private key, derive one from what it stores, sign on anyone's
behalf, or recover a forgotten passphrase. Those are not restrictions in code
that could be lifted — the material simply is not here.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from . import signature_id as sid

logger = logging.getLogger(__name__)

ATTESTATION_VERSION = "nes-attestation-v1"

#: Minimum PBKDF2 work the server will accept from a browser. A client could
#: claim fewer iterations to make its own life easier and everyone's key
#: cheaper to attack, so the floor is enforced here rather than trusted.
MIN_KDF_ITERATIONS = 310000

#: An ECDSA P-256 public key in SPKI DER is 91 bytes; the encrypted PKCS8
#: blob is a little over 150. Generous ceilings, but not unbounded.
MAX_PUBLIC_KEY_BYTES = 512
MAX_BLOB_BYTES = 4096

#: How long a signing challenge stays good. Long enough to type a passphrase,
#: short enough that a captured challenge is not useful later.
CHALLENGE_TTL_SECONDS = 15 * 60


# ─────────────────────────────────────────────────────────────────────────────
# Ownership
# ─────────────────────────────────────────────────────────────────────────────

def require_owner(acting_user, identity) -> None:
    """
    Refuse unless `acting_user` *is* the identity's owner.

    Deliberately ignores staff and superuser status. An administrator who
    could change someone's signature could sign as them, which would make the
    whole construction decorative.
    """
    if acting_user is None or not getattr(acting_user, "is_authenticated", False):
        raise PermissionDenied("You must be signed in to manage a signature.")
    if identity is not None and identity.user_id != acting_user.pk:
        logger.warning(
            "NES identity: %s attempted to modify the signing identity of "
            "user %s — refused",
            getattr(acting_user, "pk", "?"), identity.user_id,
        )
        raise PermissionDenied(
            "A signing identity can only be changed by the person it belongs "
            "to. This is not an administrative restriction that can be "
            "lifted — it is what makes the signature theirs."
        )


def identity_for(user):
    """The user's identity, or None. Read-only, so no ownership check."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    from .models_esign_identity import SignatureIdentity

    return SignatureIdentity.objects.filter(user=user).first()


def identity_by_signature_id(signature_id: str):
    """Look one up by identifier, tolerating how a person typed it."""
    from .models_esign_identity import SignatureIdentity

    try:
        canonical = sid.normalise(signature_id)
    except sid.SignatureIdError:
        return None
    return SignatureIdentity.objects.filter(signature_id=canonical).first()


# ─────────────────────────────────────────────────────────────────────────────
# Validating what the browser sends
# ─────────────────────────────────────────────────────────────────────────────

def _b64_bytes(value: str, *, field: str, limit: int) -> bytes:
    try:
        raw = base64.b64decode(str(value or ""), validate=True)
    except Exception as exc:
        raise ValidationError(f"{field} is not valid base64.") from exc
    if not raw:
        raise ValidationError(f"{field} is empty.")
    if len(raw) > limit:
        raise ValidationError(f"{field} is larger than expected.")
    return raw


def validate_public_key(public_key_spki_b64: str) -> bytes:
    """
    Confirm the uploaded key really is an ECDSA P-256 public key.

    Checked rather than trusted: a client that uploaded something else would
    produce an identity whose signatures could never verify, and the failure
    would surface much later, at signing time, looking like data corruption.
    """
    raw = _b64_bytes(
        public_key_spki_b64, field="The public key", limit=MAX_PUBLIC_KEY_BYTES
    )
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import (
            load_der_public_key,
        )

        key = load_der_public_key(raw)
    except Exception as exc:
        raise ValidationError(
            "That public key could not be read."
        ) from exc

    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValidationError("The signing key must be an ECDSA key.")
    if key.curve.name != "secp256r1":
        raise ValidationError(
            f"The signing key must be on P-256; this one is on "
            f"{key.curve.name}."
        )
    return raw


def validate_kdf(iterations) -> int:
    try:
        count = int(iterations)
    except Exception as exc:
        raise ValidationError("The key-derivation iteration count is not a "
                              "number.") from exc
    if count < MIN_KDF_ITERATIONS:
        raise ValidationError(
            f"The key-derivation work factor is too low ({count}). At least "
            f"{MIN_KDF_ITERATIONS} iterations are required, so that a stolen "
            "backup cannot be brute-forced cheaply."
        )
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Enrolment and rotation
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def enroll(
    user, *, public_key_spki_b64: str, encrypted_private_key_b64: str,
    kdf_salt_b64: str, cipher_iv_b64: str, kdf_iterations,
    passphrase_hint: str = "", request=None,
):
    """
    Issue a signing identity for `user`, or add a key to an existing one.

    Called by the user for themselves; `require_owner` enforces that at the
    view. Safe to call when an identity already exists — it rotates the key
    rather than refusing, because that is what a person who forgot their
    passphrase needs, and the identifier must survive it.
    """
    from .models_esign_identity import SignatureIdentity

    validate_public_key(public_key_spki_b64)
    iterations = validate_kdf(kdf_iterations)
    _b64_bytes(encrypted_private_key_b64, field="The encrypted key",
               limit=MAX_BLOB_BYTES)
    _b64_bytes(kdf_salt_b64, field="The salt", limit=64)
    _b64_bytes(cipher_iv_b64, field="The IV", limit=32)

    identity = SignatureIdentity.objects.filter(user=user).first()

    if identity is None:
        identity = _issue_identity(user)

    if identity.status == SignatureIdentity.STATUS_RETIRED:
        raise ValidationError(
            "This signing identity has been retired and cannot take a new "
            "key."
        )

    key = _add_key(
        identity,
        public_key_spki_b64=public_key_spki_b64,
        encrypted_private_key_b64=encrypted_private_key_b64,
        kdf_salt_b64=kdf_salt_b64,
        cipher_iv_b64=cipher_iv_b64,
        kdf_iterations=iterations,
        passphrase_hint=passphrase_hint,
        request=request,
    )
    return identity, key


def _issue_identity(user):
    """
    Mint the lifetime identifier.

    Retries on the vanishingly unlikely collision by drawing a new nonce —
    the identifier is derived, so a fresh nonce yields a fresh identifier
    without touching anything else.
    """
    from .models_esign_identity import SignatureIdentity

    office = sid.office_prefix()
    issued_at = timezone.now()
    issued_iso = issued_at.isoformat()

    for _attempt in range(8):
        nonce = sid.new_nonce()
        candidate = sid.compute_signature_id(
            office=office, user_pk=user.pk, nonce=nonce,
            issued_at_iso=issued_iso,
        )
        if SignatureIdentity.objects.filter(signature_id=candidate).exists():
            continue

        full_name = (
            getattr(user, "get_full_name", lambda: "")() or ""
        ).strip() or getattr(user, "username", "")

        return SignatureIdentity.objects.create(
            user=user,
            signature_id=candidate,
            enrollment_nonce=nonce,
            office_code=office,
            issued_at=issued_at,
            enrolled_name=full_name[:200],
            enrolled_email=(getattr(user, "email", "") or "")[:254],
        )

    raise ValidationError(
        "A signature ID could not be issued. Please try again."
    )


def _add_key(identity, *, public_key_spki_b64, encrypted_private_key_b64,
             kdf_salt_b64, cipher_iv_b64, kdf_iterations,
             passphrase_hint="", request=None):
    from .models_esign_identity import SignatureKey

    SignatureKey.objects.filter(identity=identity, is_current=True).update(
        is_current=False,
        retired_at=timezone.now(),
        retired_reason="Replaced by a newer key",
    )

    revision = identity.key_revision + 1
    key = SignatureKey.objects.create(
        identity=identity,
        revision=revision,
        public_key_spki=public_key_spki_b64,
        key_fingerprint=SignatureKey.fingerprint_of(public_key_spki_b64),
        encrypted_private_key=encrypted_private_key_b64,
        kdf_salt=kdf_salt_b64,
        cipher_iv=cipher_iv_b64,
        kdf_iterations=kdf_iterations,
        passphrase_hint=(passphrase_hint or "")[:120],
        is_current=True,
        created_ip=_client_ip(request),
        created_user_agent=_user_agent(request),
    )

    identity.key_revision = revision
    identity.save(update_fields=["key_revision", "updated_at"])

    # Tell the owner, once the key is really stored. A key added in their
    # name that they did not add is the one attack the server cannot
    # recognise on its own — but the person it impersonates can, from their
    # own inbox. See accounts/key_watch.py for the sweep that catches keys
    # added without going through here.
    try:
        from .key_watch import announce

        transaction.on_commit(lambda: announce(key))
    except Exception:
        logger.warning("NES: could not schedule the key notice for %s",
                       identity.signature_id, exc_info=True)
    return key


@transaction.atomic
def set_mark(identity, *, kind, image_content, initials_content=None,
             typed_text="", initials_text="", font_key="dancing",
             request=None):
    """
    Record a new visual mark. The previous one is kept, not overwritten.

    `image_content` is a Django `ContentFile`/`File` of the rendered PNG.
    """
    from django.core.files.base import File
    from .models_esign_identity import SignatureMark

    if image_content is None:
        raise ValidationError("A signature image is required.")

    SignatureMark.objects.filter(identity=identity, is_current=True).update(
        is_current=False, replaced_at=timezone.now()
    )

    revision = identity.mark_revision + 1
    mark = SignatureMark(
        identity=identity,
        revision=revision,
        kind=kind,
        typed_text=(typed_text or "")[:120],
        initials_text=(initials_text or "")[:12],
        font_key=(font_key or "dancing")[:24],
        is_current=True,
        created_ip=_client_ip(request),
    )

    name = f"{identity.signature_id}-r{revision}.png"
    mark.image.save(name, image_content, save=False)
    if initials_content is not None:
        mark.initials_image.save(
            f"{identity.signature_id}-r{revision}-initials.png",
            initials_content, save=False,
        )

    try:
        mark.image.open("rb")
        try:
            mark.image_sha256 = hashlib.sha256(mark.image.read()).hexdigest()
        finally:
            mark.image.close()
    except Exception:
        mark.image_sha256 = ""

    mark.save()

    identity.mark_revision = revision
    identity.save(update_fields=["mark_revision", "updated_at"])
    return mark


# ─────────────────────────────────────────────────────────────────────────────
# Challenge and verification
# ─────────────────────────────────────────────────────────────────────────────

def new_challenge_nonce() -> str:
    return secrets.token_hex(16)


def build_statement(
    *, signature_id: str, envelope_uid: str, document_sha256: str,
    nonce: str, signer_name: str = "", issued_at_iso: str = "",
    field_summary: str = "",
) -> str:
    """
    The exact text the signatory's browser signs.

    Written to be read, not parsed. If this ever ends up in front of a
    tribunal, it should be obvious what the person put their key to: which
    identity, which envelope, which bytes, and when. The document hash is the
    load-bearing part — it binds the signature to the content, so the
    statement cannot be replayed against a different document.
    """
    return (
        f"{ATTESTATION_VERSION}\n"
        f"signature_id={signature_id}\n"
        f"signer={signer_name}\n"
        f"envelope={envelope_uid}\n"
        f"document_sha256={document_sha256}\n"
        f"fields={field_summary}\n"
        f"nonce={nonce}\n"
        f"issued_at={issued_at_iso}\n"
    )


def verify_signature(public_key_spki_b64: str, statement: str,
                     signature_b64: str) -> tuple[bool, str]:
    """
    Check an ECDSA P-256 signature produced by WebCrypto.

    WebCrypto emits IEEE P1363 — a bare 64-byte `r||s` — while the
    `cryptography` library expects DER. Converting is the whole trick, and
    getting it wrong fails in a way that looks like a bad passphrase, so it
    is done explicitly here.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric import (
            utils as asym_utils,
        )
        from cryptography.hazmat.primitives.serialization import (
            load_der_public_key,
        )

        public_key = load_der_public_key(base64.b64decode(public_key_spki_b64))
        raw = base64.b64decode(signature_b64, validate=True)

        if len(raw) != 64:
            return False, (
                f"A P-256 signature is 64 bytes; this one is {len(raw)}."
            )

        r = int.from_bytes(raw[:32], "big")
        s = int.from_bytes(raw[32:], "big")
        der = asym_utils.encode_dss_signature(r, s)

        try:
            public_key.verify(
                der, statement.encode("utf-8"), ec.ECDSA(hashes.SHA256())
            )
        except InvalidSignature:
            return False, (
                "The signature does not match this statement and key. Either "
                "the document changed after it was signed, or the signature "
                "was not made by this identity."
            )
        return True, ""
    except Exception as exc:
        logger.warning("NES identity: verification error", exc_info=True)
        return False, f"The signature could not be checked: {exc}"


@transaction.atomic
def record_attestation(
    *, identity, key, envelope, recipient=None, statement: str,
    signature_b64: str, nonce: str, document_sha256: str = "", request=None,
):
    """
    Verify a signatory's signature and store it.

    Stored whether or not it verifies: a failed attestation is evidence that
    something went wrong at a particular moment, and silently dropping it
    would hide exactly the event worth investigating. `verified` carries the
    outcome and the caller decides whether to let the signing proceed.
    """
    from .models_esign_identity import SignatureAttestation

    ok, detail = verify_signature(
        key.public_key_spki, statement, signature_b64
    )

    attestation = SignatureAttestation.objects.create(
        identity=identity,
        key=key,
        envelope=envelope,
        recipient=recipient,
        signature_id=identity.signature_id,
        signer_name=identity.enrolled_name or str(identity.user),
        signer_email=identity.enrolled_email,
        key_fingerprint=key.key_fingerprint,
        algorithm=key.algorithm,
        statement=statement,
        signature_b64=signature_b64,
        nonce=nonce,
        document_sha256=document_sha256 or "",
        verified=ok,
        verify_detail=detail,
        verified_at=timezone.now() if ok else None,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )

    if ok:
        identity.last_used_at = timezone.now()
        identity.use_count = (identity.use_count or 0) + 1
        identity.save(update_fields=["last_used_at", "use_count", "updated_at"])

    return attestation, ok, detail


def verify_stored_attestation(attestation) -> tuple[bool, str]:
    """
    Re-check a stored attestation from scratch.

    Used by the verification page and the evidence package: it proves the
    record still holds rather than trusting the `verified` flag written when
    it was created.
    """
    if attestation.key_id is None:
        return False, "The key that made this signature is no longer on file."
    return verify_signature(
        attestation.key.public_key_spki,
        attestation.statement,
        attestation.signature_b64,
    )


def envelope_attestations(envelope):
    from .models_esign_identity import SignatureAttestation

    return (
        SignatureAttestation.objects
        .filter(envelope=envelope)
        .select_related("identity", "key", "recipient")
        .order_by("created_at", "id")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _client_ip(request):
    if request is None:
        return None
    try:
        from .utils_esign import client_ip

        return client_ip(request)
    except Exception:
        return None


def _user_agent(request):
    if request is None:
        return ""
    try:
        from .utils_esign import user_agent

        return user_agent(request)
    except Exception:
        return ""
