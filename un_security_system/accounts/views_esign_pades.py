# accounts/views_esign_pades.py
"""
UN PASS — NES seal verification pages (PAdES, ETSI EN 319 142).

Two audiences:

* A party to the envelope opens the verify page from the envelope detail
  screen and sees, in plain words, whether the stored PDF is still intact and
  who sealed it. Access is the existing participation check, so nothing new is
  exposed.

* An auditor holding a downloaded copy compares its SHA-256 against the value
  recorded at completion. The hash is shown so that comparison needs nothing
  but `sha256sum` and no access to the signing key.

The validation runs against the bytes **currently stored**, not against the
record, so a file quietly replaced on disk shows up here as broken rather than
as whatever the database remembers.

Routes are registered by accounts/urls_esign_pades.py, which accounts/urls.py
includes — the production URL file keeps one new line.
"""

from __future__ import annotations

import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from . import views_esign
from .models_esign import Envelope
from . import pades_esign as P

logger = logging.getLogger(__name__)


def _stored_pdf_bytes(envelope) -> bytes:
    field = getattr(envelope, "completed_pdf", None)
    if not field:
        return b""
    try:
        field.open("rb")
        try:
            return field.read()
        finally:
            field.close()
    except Exception:
        logger.warning(
            "NES PAdES: could not read the stored PDF for envelope %s",
            getattr(envelope, "pk", "?"),
            exc_info=True,
        )
        return b""


def _seal_record(envelope):
    """The stored EnvelopeSeal, or None. Never raises."""
    try:
        return envelope.seal
    except Exception:
        return None


def _context(envelope) -> dict:
    from .audit_chain_esign import verify_anchor, verify_chain

    raw = _stored_pdf_bytes(envelope)
    seal = _seal_record(envelope)

    result = {"ok": False, "available": True, "signatures": [], "error": ""}
    stored_sha = ""

    if envelope.status != Envelope.STATUS_COMPLETED:
        result["error"] = (
            "This envelope is not completed yet, so there is nothing to verify."
        )
    elif not raw:
        result["error"] = "The completed PDF could not be read from storage."
    else:
        stored_sha = P.sha256_of(raw)
        result = P.verify_pdf(raw)

    # A hash that no longer matches the one recorded at completion means the
    # stored file changed, whatever the signature says.
    recorded_sha = (seal.document_sha256 if seal else "") or ""
    hash_matches = bool(recorded_sha and stored_sha and recorded_sha == stored_sha)

    # The audit trail is the other half of tamper-evidence: the seal protects
    # the document, the chain protects the record of who did what to it.
    chain = verify_chain(envelope)
    anchor = verify_anchor(envelope, seal)

    # And the third part: which signatories proved it with their own key.
    try:
        from . import identity_esign as _identity

        attestations = []
        for a in _identity.envelope_attestations(envelope):
            ok, detail = _identity.verify_stored_attestation(a)
            attestations.append({
                "signature_id": a.signature_id,
                "signer_name": a.signer_name,
                "key_revision": a.key.revision if a.key_id else None,
                "key_fingerprint": a.key_fingerprint,
                "algorithm": a.algorithm,
                "signed_at": a.created_at,
                "verified": ok,
                "detail": detail,
            })
    except Exception:
        attestations = []

    attestations_ok = all(a["verified"] for a in attestations) if attestations else True

    return {
        "envelope": envelope,
        "seal": seal,
        "result": result,
        "stored_sha256": stored_sha,
        "recorded_sha256": recorded_sha,
        "hash_matches": hash_matches,
        "hash_mismatch": bool(recorded_sha and stored_sha and not hash_matches),
        "pades_configured": P.pades_configured()[0],
        "chain": chain,
        "anchor": anchor,
        "attestations": attestations,
        "attestations_ok": attestations_ok,
        # One verdict covering document, trail and anchor together. A green
        # document with a broken trail is not a pass.
        "all_ok": bool(
            result.get("ok")
            and not bool(recorded_sha and stored_sha and not hash_matches)
            and chain["ok"]
            and (not anchor["present"] or anchor["ok"])
            and attestations_ok
        ),
    }


@login_required
@require_GET
def esign_seal_verify(request, pk):
    """Human-readable verification page for a party to the envelope."""
    envelope = views_esign._get_envelope_for_user(request, pk)
    return render(
        request, "accounts/esign/seal_verify.html", _context(envelope)
    )


@login_required
@require_GET
def esign_seal_verify_json(request, pk):
    """Same check, as JSON — for the mobile app and for monitoring."""
    envelope = views_esign._get_envelope_for_user(request, pk)
    ctx = _context(envelope)
    seal = ctx["seal"]

    return JsonResponse({
        "ok": ctx["all_ok"],
        "document_ok": bool(ctx["result"]["ok"] and not ctx["hash_mismatch"]),
        "audit_chain": {
            "ok": ctx["chain"]["ok"],
            "total": ctx["chain"]["total"],
            "chained": ctx["chain"]["chained"],
            "unchained": ctx["chain"]["unchained"],
            "head": ctx["chain"]["head"],
            "problems": ctx["chain"]["problems"],
        },
        "audit_anchor": ctx["anchor"],
        "envelope_id": envelope.envelope_id,
        "status": envelope.status,
        "stored_sha256": ctx["stored_sha256"],
        "recorded_sha256": ctx["recorded_sha256"],
        "hash_matches": ctx["hash_matches"],
        "seal": None if seal is None else {
            "status": seal.status,
            "level_requested": seal.level_requested,
            "level_achieved": seal.level_achieved,
            "applied_at": seal.applied_at.isoformat() if seal.applied_at else None,
            "signer_common_name": seal.signer_common_name,
            "signer_organization": seal.signer_organization,
            "signer_issuer": seal.signer_issuer,
            "timestamped": seal.timestamped,
            "timestamp_authority": seal.timestamp_authority,
            "certificate_expires_at": (
                seal.certificate_expires_at.isoformat()
                if seal.certificate_expires_at else None
            ),
            "detail": seal.detail,
            "policy_oid": seal.policy_oid,
            "policy_uri": seal.policy_uri,
            "commitment_type": seal.commitment_type,
            "certificate_sealed": seal.certificate_sealed,
            "refresh_count": seal.refresh_count,
            "last_refreshed_at": (
                seal.last_refreshed_at.isoformat()
                if seal.last_refreshed_at else None
            ),
            "next_refresh_due": (
                seal.next_refresh_due.isoformat()
                if seal.next_refresh_due else None
            ),
            "refresh_overdue": seal.refresh_overdue,
            "long_term_outlook": seal.long_term_outlook,
        },
        "signatures": [
            {
                "field_name": r["field_name"],
                "intact": r["intact"],
                "valid": r["valid"],
                "trusted": r["trusted"],
                "coverage": r["coverage"],
                "signer": r["signer"],
                "summary": r["summary"],
                "error": r["error"],
            }
            for r in ctx["result"]["signatures"]
        ],
        "error": ctx["result"]["error"],
    })


def _evidence_response(envelope):
    """Build and stream the offline evidence package."""
    from .evidence_esign import envelope_evidence

    blob, report = envelope_evidence(envelope)
    if not blob:
        raise Http404("The evidence package could not be built.")

    response = HttpResponse(
        blob, content_type="application/vnd.etsi.asic-e+zip"
    )
    response["Content-Disposition"] = (
        f'attachment; filename="{envelope.envelope_id}-evidence.asice.zip"'
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    logger.info(
        "NES evidence: built %d-file package (%d bytes) for envelope %s",
        report.get("files", 0), report.get("bytes", 0), envelope.envelope_id,
    )
    return response


@login_required
@require_GET
def esign_evidence(request, pk):
    """
    Download the offline evidence package.

    Logged as a download so the audit trail records that the evidence left
    the system, and who took it.
    """
    from .utils_esign import log_event

    envelope = views_esign._get_envelope_for_user(request, pk)
    response = _evidence_response(envelope)
    log_event(
        envelope, "downloaded", request=request, actor=request.user,
        note=f"{envelope.envelope_id}-evidence.asice.zip",
    )
    return response


@require_GET
def esign_token_evidence(request, token):
    """Evidence package for a recipient holding a tokenized signing link."""
    from .utils_esign import log_event

    recipient = views_esign._recipient_or_404(token)
    if recipient.access_code and not views_esign._access_ok(request, recipient):
        raise Http404()

    envelope = recipient.envelope
    response = _evidence_response(envelope)
    log_event(
        envelope, "downloaded", request=request, recipient=recipient,
        note=f"{envelope.envelope_id}-evidence.asice.zip",
    )
    return response


@require_GET
def esign_token_seal_verify(request, token):
    """
    Verification for a signer or viewer holding a tokenized link, so an
    external recipient can confirm the document without a UN PASS account.
    """
    recipient = views_esign._recipient_or_404(token)
    if recipient.access_code and not views_esign._access_ok(request, recipient):
        raise Http404()

    ctx = _context(recipient.envelope)
    ctx["recipient"] = recipient
    ctx["public"] = True
    ctx["base_template"] = "accounts/esign/base_public.html"
    return render(request, "accounts/esign/seal_verify.html", ctx)
