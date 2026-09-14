"""
accounts/pdf_delivery.py
========================

Safe eSign PDF delivery for UNPASS.

Why this exists
---------------
The existing eSign engine already knows how to build a correct final PDF from
the envelope's source documents and completed fields. The problem is that a
completed envelope may point at a missing/corrupt `completed_pdf` media object.
When that happens both the web download and the Android viewer fail even though
the envelope itself still contains enough data to rebuild the signed PDF.

These views validate stored PDF bytes before serving them. If a completed PDF
or certificate is missing/bad, it is regenerated, saved back to the envelope,
and then served.

This module deliberately leaves the large existing `views_esign.py` untouched.
`accounts/urls.py` routes the relevant PDF endpoints here.
"""

import io
import logging

from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404
from django.views.decorators.http import require_GET
from pypdf import PdfReader

from .models_esign import Envelope
from .utils_esign import (
    build_certificate_pdf,
    build_final_pdf,
    log_event,
)
from . import views_esign


logger = logging.getLogger(__name__)


def _valid_pdf(raw: bytes) -> bool:
    """True only when the bytes look like and can be parsed as a PDF."""
    if not raw or not raw.startswith(b"%PDF-"):
        return False
    try:
        PdfReader(io.BytesIO(raw), strict=False)
        return True
    except Exception:
        return False


def _read_file_field(field) -> bytes:
    if not field:
        return b""
    try:
        field.open("rb")
        raw = field.read()
        field.close()
        return raw
    except Exception:
        try:
            field.close()
        except Exception:
            pass
        return b""


def _repair_generated_pdf(envelope: Envelope, kind: str) -> bytes:
    """
    Return a valid generated PDF.

    `kind` is `final` or `certificate`. Existing valid media is reused.
    Missing/corrupt media is rebuilt only for a completed envelope.
    """
    is_certificate = kind == "certificate"
    field_name = "certificate_pdf" if is_certificate else "completed_pdf"
    stored = getattr(envelope, field_name, None)
    raw = _read_file_field(stored)

    if _valid_pdf(raw):
        return raw

    if envelope.status != Envelope.STATUS_COMPLETED:
        raise Http404("The signed document is not available until completion.")

    logger.warning(
        "eSign: %s for completed envelope %s is missing or invalid; rebuilding",
        field_name,
        envelope.pk,
    )

    try:
        rebuilt = (
            build_certificate_pdf(envelope)
            if is_certificate
            else build_final_pdf(envelope)
        )
    except Exception as exc:
        logger.exception(
            "eSign: could not rebuild %s for envelope %s",
            field_name,
            envelope.pk,
        )
        raise Http404("The PDF could not be regenerated.") from exc

    if not _valid_pdf(rebuilt):
        logger.error(
            "eSign: rebuilt %s for envelope %s is not a valid PDF",
            field_name,
            envelope.pk,
        )
        raise Http404("The regenerated file is not a valid PDF.")

    filename = (
        f"{envelope.envelope_id}-certificate.pdf"
        if is_certificate
        else f"{envelope.envelope_id}-signed.pdf"
    )

    try:
        getattr(envelope, field_name).save(
            filename,
            ContentFile(rebuilt),
            save=False,
        )
        envelope.save(update_fields=[field_name])
    except Exception:
        # The current request can still succeed using valid in-memory bytes.
        logger.exception(
            "eSign: rebuilt %s for envelope %s but could not save it",
            field_name,
            envelope.pk,
        )

    return rebuilt


def _pdf_response(raw: bytes, filename: str, *, inline: bool):
    if not _valid_pdf(raw):
        raise Http404("The stored file is not a valid PDF.")

    response = FileResponse(
        io.BytesIO(raw),
        content_type="application/pdf",
    )
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = (
        f'{disposition}; filename="{filename}"'
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_GET
def esign_preview(request, pk):
    """
    Sender/participant preview.

    For completed envelopes this previews the repaired signed PDF.
    For open envelopes it builds a fresh preview from current fields.
    """
    envelope = views_esign._get_envelope_for_user(request, pk)

    if envelope.status == Envelope.STATUS_COMPLETED:
        raw = _repair_generated_pdf(envelope, "final")
        name = f"{envelope.envelope_id}-signed.pdf"
    else:
        raw = build_final_pdf(envelope)
        if not _valid_pdf(raw):
            raise Http404("The preview could not be generated.")
        name = f"{envelope.envelope_id}-preview.pdf"

    return _pdf_response(raw, name, inline=True)


@login_required
@require_GET
def esign_download(request, pk, kind):
    """Authenticated sender/participant PDF download."""
    envelope = views_esign._get_envelope_for_user(request, pk)

    normalized = "certificate" if kind == "certificate" else "final"
    raw = _repair_generated_pdf(envelope, normalized)
    name = (
        f"{envelope.envelope_id}-certificate.pdf"
        if normalized == "certificate"
        else f"{envelope.envelope_id}-signed.pdf"
    )

    log_event(
        envelope,
        "downloaded",
        request=request,
        actor=request.user,
        note=name,
    )

    # `?inline=1` lets a web button/viewer display the final PDF while the
    # normal download URL retains attachment behaviour.
    inline = request.GET.get("inline") in {"1", "true", "yes"}
    return _pdf_response(raw, name, inline=inline)


@require_GET
def esign_token_document(request, token, doc_id):
    """
    Document endpoint used by signer links.

    Before completion it delegates to the existing source-document view.
    After completion it always serves the repaired final signed PDF so a signer
    cannot be sent to a broken stored media object.
    """
    recipient = views_esign._recipient_or_404(token)
    if recipient.access_code and not views_esign._access_ok(request, recipient):
        raise Http404()

    envelope = recipient.envelope

    if envelope.status == Envelope.STATUS_COMPLETED:
        raw = _repair_generated_pdf(envelope, "final")
        return _pdf_response(
            raw,
            f"{envelope.envelope_id}-signed.pdf",
            inline=True,
        )

    return views_esign.esign_token_document(
        request,
        token,
        doc_id,
    )


@require_GET
def esign_token_download(request, token, kind):
    """Tokenized final/certificate download with repair."""
    recipient = views_esign._recipient_or_404(token)
    if recipient.access_code and not views_esign._access_ok(request, recipient):
        raise Http404()

    envelope = recipient.envelope
    normalized = "certificate" if kind == "certificate" else "final"
    raw = _repair_generated_pdf(envelope, normalized)

    name = (
        f"{envelope.envelope_id}-certificate.pdf"
        if normalized == "certificate"
        else f"{envelope.envelope_id}-signed.pdf"
    )

    log_event(
        envelope,
        "downloaded",
        request=request,
        recipient=recipient,
        note=name,
    )

    inline = request.GET.get("inline") in {"1", "true", "yes"}
    return _pdf_response(raw, name, inline=inline)
