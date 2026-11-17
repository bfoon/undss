# accounts/views_esign_identity.py
"""
UN PASS — NES signing identity views.

Every endpoint here acts on `request.user` and nobody else. There is no
`?user=` parameter, no pk in any path, and no administrative override — not
because an override would be hard to add, but because the moment one exists
somebody is granted it and the signature stops being the signatory's.

    GET  /accounts/esign/signature/                 my signing identity
    POST /accounts/esign/signature/enroll/          create or rotate my key
    POST /accounts/esign/signature/mark/            change my visual mark
    POST /accounts/esign/signature/material/        my encrypted key, to unlock
    POST /accounts/esign/signature/challenge/       a statement to sign
    GET  /accounts/esign/signature/lookup/          resolve a signature ID

The `material` endpoint hands back the signatory's own encrypted private key.
That is intentional and safe: it is AES-GCM ciphertext under a passphrase the
server has never seen, so it is useless to whoever holds it without that
passphrase. It is released only to the owner, over POST with CSRF, so it is
not fetchable by a third-party page.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import identity_esign as I
from . import signature_id as sid

logger = logging.getLogger(__name__)

MAX_JSON = 32 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def _json(request) -> dict:
    raw = request.body[:MAX_JSON + 1]
    if len(raw) > MAX_JSON:
        raise ValidationError("That request was larger than expected.")
    try:
        data = json.loads(raw or b"{}")
    except Exception as exc:
        raise ValidationError("Malformed JSON.") from exc
    if not isinstance(data, dict):
        raise ValidationError("Expected a JSON object.")
    return data


def _fail(exc, status=400):
    detail = (
        "; ".join(exc.messages) if isinstance(exc, ValidationError)
        else str(exc)
    )
    return JsonResponse({"ok": False, "error": detail}, status=status)


# ─────────────────────────────────────────────────────────────────────────────
# My signing identity
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def identity_home(request):
    """The signatory's own page. Nobody can reach anyone else's."""
    identity = I.identity_for(request.user)

    context = {
        "identity": identity,
        "current_key": identity.current_key if identity else None,
        "current_mark": identity.current_mark if identity else None,
        "key_history": (
            identity.keys.order_by("-revision")[:12] if identity else []
        ),
        "mark_history": (
            identity.marks.order_by("-revision")[:12] if identity else []
        ),
        "attestation_count": (
            identity.attestations.filter(verified=True).count()
            if identity else 0
        ),
        "min_iterations": I.MIN_KDF_ITERATIONS,
        "font_choices": _font_choices(),
    }
    return render(request, "accounts/esign/identity.html", context)


def _font_choices():
    try:
        from .models_esign import SignatureProfile

        return SignatureProfile.FONT_CHOICES
    except Exception:
        return [("dancing", "Dancing Script")]


@login_required
@require_POST
def identity_enroll(request):
    """
    Create a signing key, or rotate to a new one.

    Rotation is the same operation on purpose: somebody who has forgotten
    their passphrase needs a new key, and must keep their signature ID.
    """
    try:
        data = _json(request)
        identity, key = I.enroll(
            request.user,
            public_key_spki_b64=data.get("public_key_spki", ""),
            encrypted_private_key_b64=data.get("encrypted_private_key", ""),
            kdf_salt_b64=data.get("kdf_salt", ""),
            cipher_iv_b64=data.get("cipher_iv", ""),
            kdf_iterations=data.get("kdf_iterations", 0),
            passphrase_hint=data.get("passphrase_hint", ""),
            request=request,
        )
    except ValidationError as exc:
        return _fail(exc)
    except PermissionDenied as exc:
        return _fail(exc, status=403)
    except Exception as exc:
        logger.exception("NES identity: enrolment failed")
        return _fail(exc, status=500)

    first_time = key.revision == 1
    logger.info(
        "NES identity: %s key revision %s for %s",
        "issued" if first_time else "rotated", key.revision,
        identity.signature_id,
    )
    return JsonResponse({
        "ok": True,
        "signature_id": identity.signature_id,
        "key_revision": key.revision,
        "key_fingerprint": key.key_fingerprint,
        "first_time": first_time,
        "message": (
            f"Your signature ID is {identity.signature_id}. It is yours for "
            "as long as you have an account and will not change, even if you "
            "change your signature or your key."
            if first_time else
            f"A new key was created. Your signature ID is unchanged: "
            f"{identity.signature_id}. Documents you signed with the old key "
            "still verify."
        ),
    })


@login_required
@require_POST
def identity_mark(request):
    """Change the visual mark. Same ID, new revision, old one kept."""
    identity = I.identity_for(request.user)
    if identity is None:
        return JsonResponse({
            "ok": False,
            "error": "Create your signing key first — the mark belongs to it.",
        }, status=400)

    try:
        I.require_owner(request.user, identity)
        data = _json(request)

        kind = str(data.get("kind") or "drawn")
        image = _decode_png(data.get("image_data_url"), "signature")
        initials = (
            _decode_png(data.get("initials_data_url"), "initials")
            if data.get("initials_data_url") else None
        )

        mark = I.set_mark(
            identity,
            kind=kind,
            image_content=image,
            initials_content=initials,
            typed_text=data.get("typed_text", ""),
            initials_text=data.get("initials_text", ""),
            font_key=data.get("font_key", "dancing"),
            request=request,
        )
    except ValidationError as exc:
        return _fail(exc)
    except PermissionDenied as exc:
        return _fail(exc, status=403)
    except Exception as exc:
        logger.exception("NES identity: mark update failed")
        return _fail(exc, status=500)

    return JsonResponse({
        "ok": True,
        "mark_revision": mark.revision,
        "signature_id": identity.signature_id,
        "image_url": mark.image.url if mark.image else "",
        "message": (
            f"Your signature has been updated. Revision {mark.revision}; "
            f"your signature ID is still {identity.signature_id}."
        ),
    })


def _decode_png(data_url, label):
    """Turn a `data:image/png;base64,…` URL into a ContentFile."""
    from django.core.files.base import ContentFile

    text = str(data_url or "")
    if "," in text:
        header, _, payload = text.partition(",")
        if "base64" not in header or "image/" not in header:
            raise ValidationError(f"The {label} must be a base64 image.")
    else:
        payload = text

    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise ValidationError(f"The {label} image is not valid base64.") from exc

    if not raw:
        raise ValidationError(f"The {label} image is empty.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValidationError(f"The {label} image is too large.")
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValidationError(f"The {label} image must be a PNG.")

    return ContentFile(raw)


@login_required
@require_POST
def identity_material(request):
    """
    Hand the signatory their own encrypted key so the browser can unlock it.

    Ciphertext only. Without the passphrase — which this server has never
    seen and cannot derive — it does not yield a usable key.
    """
    identity = I.identity_for(request.user)
    if identity is None or identity.current_key is None:
        return JsonResponse(
            {"ok": False, "error": "You have no signing key yet."}, status=404
        )
    try:
        I.require_owner(request.user, identity)
    except PermissionDenied as exc:
        return _fail(exc, status=403)

    if identity.status != identity.STATUS_ACTIVE:
        return JsonResponse({
            "ok": False,
            "error": f"This signing identity is {identity.get_status_display().lower()}.",
        }, status=403)

    key = identity.current_key
    return JsonResponse({
        "ok": True,
        "signature_id": identity.signature_id,
        "key_revision": key.revision,
        "algorithm": key.algorithm,
        "encrypted_private_key": key.encrypted_private_key,
        "kdf_salt": key.kdf_salt,
        "cipher_iv": key.cipher_iv,
        "kdf_iterations": key.kdf_iterations,
        "passphrase_hint": key.passphrase_hint,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Challenge
# ─────────────────────────────────────────────────────────────────────────────

def envelope_document_digest(envelope) -> str:
    """
    SHA-256 over the source documents, in order.

    This is what the signatory actually looked at, so it is what their
    signature should bind to. Binding to the final stamped PDF is impossible
    here: it does not exist until everyone has signed, and it changes as each
    person does.
    """
    digest = hashlib.sha256()
    try:
        from .utils_esign import document_pdf_bytes

        for document in envelope.documents.all().order_by("order", "id"):
            digest.update(document_pdf_bytes(document))
    except Exception:
        logger.warning(
            "NES identity: could not digest documents for envelope %s",
            getattr(envelope, "pk", "?"), exc_info=True,
        )
        return ""
    return digest.hexdigest()


@login_required
@require_POST
def identity_challenge(request):
    """
    Issue a statement for the signatory to sign.

    The nonce goes into the session as well as the statement: the attestation
    is only accepted back if the nonce matches one this server issued to this
    session, which stops a statement being prepared elsewhere and replayed.
    """
    from .models_esign import EnvelopeRecipient

    identity = I.identity_for(request.user)
    if identity is None or not identity.is_usable:
        return JsonResponse({
            "ok": False,
            "error": "You need an active signing key and signature first.",
        }, status=400)

    try:
        data = _json(request)
    except ValidationError as exc:
        return _fail(exc)

    token = str(data.get("recipient_token") or "")
    recipient = EnvelopeRecipient.objects.filter(token=token).first()
    if recipient is None:
        return JsonResponse(
            {"ok": False, "error": "Unknown signing link."}, status=404
        )

    # The signed-in user must be the person this envelope was sent to.
    if recipient.user_id and recipient.user_id != request.user.pk:
        logger.warning(
            "NES identity: user %s requested a challenge for recipient %s "
            "belonging to user %s",
            request.user.pk, recipient.pk, recipient.user_id,
        )
        return JsonResponse({
            "ok": False,
            "error": "This signing link belongs to someone else.",
        }, status=403)

    envelope = recipient.envelope
    nonce = I.new_challenge_nonce()
    issued_at = timezone.now()

    field_summary = str(data.get("field_summary") or "")[:200]
    statement = I.build_statement(
        signature_id=identity.signature_id,
        envelope_uid=envelope.envelope_id,
        document_sha256=envelope_document_digest(envelope),
        nonce=nonce,
        signer_name=identity.enrolled_name or str(request.user),
        issued_at_iso=issued_at.isoformat(),
        field_summary=field_summary,
    )

    request.session[f"nes_challenge_{recipient.token}"] = {
        "nonce": nonce,
        "statement": statement,
        "issued_at": issued_at.isoformat(),
    }
    request.session.modified = True

    return JsonResponse({
        "ok": True,
        "signature_id": identity.signature_id,
        "statement": statement,
        "nonce": nonce,
        "expires_in": I.CHALLENGE_TTL_SECONDS,
    })


def consume_challenge(request, recipient):
    """
    Take the challenge issued to this session, if it is still good.

    Returns `(statement, nonce)` or `(None, None)`. Removed on read so one
    challenge signs one submission.
    """
    key = f"nes_challenge_{recipient.token}"
    stored = request.session.get(key)
    if not isinstance(stored, dict):
        return None, None

    request.session.pop(key, None)
    request.session.modified = True

    try:
        from django.utils.dateparse import parse_datetime

        issued = parse_datetime(stored.get("issued_at") or "")
        if issued is None:
            return None, None
        age = (timezone.now() - issued).total_seconds()
        if age > I.CHALLENGE_TTL_SECONDS or age < -60:
            return None, None
    except Exception:
        return None, None

    return stored.get("statement") or None, stored.get("nonce") or None


# ─────────────────────────────────────────────────────────────────────────────
# Lookup
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_GET
def identity_lookup(request):
    """
    Resolve a signature ID to the person it belongs to.

    Deliberately thin: the name the identity was enrolled under, its status
    and whether it is in use. No email, no account, no document history —
    this answers "is this a real signature ID and whose is it", which is what
    somebody holding a signed document needs, and nothing more.
    """
    query = str(request.GET.get("signature_id") or "").strip()
    if not query:
        return JsonResponse(
            {"ok": False, "error": "Give a signature ID to look up."},
            status=400,
        )

    try:
        canonical = sid.normalise(query)
    except sid.SignatureIdError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    identity = I.identity_by_signature_id(canonical)
    if identity is None:
        return JsonResponse({
            "ok": True, "found": False, "signature_id": canonical,
            "detail": "No signing identity has that ID at this office.",
        })

    return JsonResponse({
        "ok": True,
        "found": True,
        "signature_id": identity.signature_id,
        "name": identity.enrolled_name,
        "status": identity.status,
        "issued_at": identity.issued_at.isoformat(),
        "office": identity.office_code,
        "key_revision": identity.key_revision,
        "mark_revision": identity.mark_revision,
        "documents_signed": identity.attestations.filter(verified=True).count(),
    })


@login_required
@require_POST
def identity_retire(request):
    """
    Retire my own signing identity.

    The identity and its history remain — documents refer to them — but it
    can no longer be used to sign. Only the owner may do this, and it cannot
    be undone from here, which is why the ID is still never reissued.
    """
    identity = I.identity_for(request.user)
    if identity is None:
        return redirect("accounts:esign_identity_home")

    try:
        I.require_owner(request.user, identity)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect("accounts:esign_identity_home")

    if str(request.POST.get("confirm") or "").strip() != identity.signature_id:
        messages.error(
            request,
            "To retire your signing identity, type its ID exactly to confirm.",
        )
        return redirect("accounts:esign_identity_home")

    identity.status = identity.STATUS_RETIRED
    identity.status_changed_at = timezone.now()
    identity.save(update_fields=["status", "status_changed_at", "updated_at"])

    messages.success(
        request,
        f"{identity.signature_id} has been retired. Documents you already "
        "signed are unaffected and still verify.",
    )
    return redirect("accounts:esign_identity_home")
