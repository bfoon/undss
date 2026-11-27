# accounts/esign_finalize.py
"""
UN PASS — NES: finishing envelopes that could not be finished, and re-issuing
documents that were finished without their marks.

What went wrong (November 2026)
-------------------------------
`requirements.txt` did not pin pypdf. Rebuilding the image installed pypdf 7,
which refuses to merge a stamp onto a page that still belongs to the file it
was read from. `build_final_pdf` wrapped that merge in `except: pass`, so every
envelope completed after the rebuild was saved, sealed and e-mailed with
**no Envelope ID and no signatures on its pages**. Nothing in the logs said so.

Three things now stop that happening again:

* the stamp is merged after the page is attached to the writer (pypdf 3–7),
  and a stamp that fails raises `StampError` instead of passing silently;
* a signer whose signature completes an envelope that then cannot be built
  is not shown a server error — the envelope waits, ICT is e-mailed, and
  the `nes-finalize-retry` job finishes it as soon as it can;
* `missing_stamp()` checks a finished file really carries its Envelope ID,
  and `manage.py esign_reissue` repairs the documents already affected.
"""
from __future__ import annotations

import hashlib
import io
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

STUCK_KEY = "unpass:esign:stuck:{pk}"


# ─────────────────────────────────────────────────────────────────────────────
# Envelopes that are fully signed but could not be completed
# ─────────────────────────────────────────────────────────────────────────────

def _ict() -> list[str]:
    try:
        from .witness_esign import alert_recipients

        return alert_recipients()
    except Exception:
        return [e for _n, e in (getattr(settings, "ADMINS", None) or []) if e]


def report_stuck(envelope, exc: Exception):
    """Log it, and e-mail ICT once per envelope per day. Never raises."""
    logger.exception("NES: envelope %s is fully signed but could not be finalised",
                     getattr(envelope, "pk", "?"), exc_info=exc)
    try:
        from .scheduler import redis_client

        client = redis_client()
        key = STUCK_KEY.format(pk=envelope.pk)
        if client is not None and not client.set(key, "1", nx=True, ex=86400):
            return
        from .asset_email import send_email_async

        to = _ict()
        if not to:
            return
        subject = f"[NES] Signed, but the final document could not be built — {envelope.subject[:80]}"
        send_email_async(
            subject=subject,
            to_emails=to,
            html_template="emails/expiry/digest.html",
            context={
                "subject": subject,
                "heading": "An envelope is fully signed but not completed",
                "intro": (
                    "Every signature is in, but UN PASS could not build the final "
                    "signed document. The signers were told it is being prepared. "
                    "Fix the cause below; the nes-finalize-retry job completes the "
                    "envelope within ten minutes of it working again."
                ),
                "columns": ["Envelope", "Subject", "Error"],
                "rows": [[envelope.envelope_id, envelope.subject, f"{type(exc).__name__}: {exc}"[:300]]],
                "footer": "Check it now with: manage.py unpass_doctor --pdf",
                "brand": getattr(settings, "ESIGN_BRAND", "NES"),
            },
        )
    except Exception:
        logger.exception("NES: could not report stuck envelope %s", getattr(envelope, "pk", "?"))


def stuck_envelopes(limit: int = 200):
    from .models_esign import Envelope

    out = []
    for env in Envelope.objects.filter(status=Envelope.STATUS_SENT).order_by("id")[:2000]:
        if env.all_signed():
            out.append(env)
            if len(out) >= limit:
                break
    return out


def retry_stuck(limit: int = 50) -> dict:
    """Scheduled: complete every envelope that is signed but not completed."""
    from . import esign_notify
    from .utils_esign import finalize_envelope

    report = {"found": 0, "completed": 0, "failed": 0, "errors": []}
    for env in stuck_envelopes(limit):
        report["found"] += 1
        try:
            finalize_envelope(env, request=None)
        except Exception as exc:  # noqa: BLE001
            report["failed"] += 1
            report["errors"].append(f"{env.envelope_id}: {type(exc).__name__}: {exc}"[:300])
            report_stuck(env, exc)
            continue
        try:
            esign_notify.notify_completed(None, env)
        except Exception:
            logger.exception("NES: completed %s but could not e-mail it", env.pk)
        report["completed"] += 1
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Finished documents that are missing their marks
# ─────────────────────────────────────────────────────────────────────────────

def _read(field) -> bytes:
    if not field:
        return b""
    try:
        field.open("rb")
        try:
            return field.read()
        finally:
            field.close()
    except Exception:
        return b""


def missing_stamp(envelope) -> bool | None:
    """
    True if the completed PDF does not carry its Envelope ID on page 1.
    None when it cannot be told (no file, unreadable, or a workflow child
    envelope, which deliberately carries no ID).
    """
    from pypdf import PdfReader

    from .utils_esign import _is_workflow_child_envelope

    if _is_workflow_child_envelope(envelope):
        return None
    raw = _read(envelope.completed_pdf)
    if not raw:
        return None
    try:
        text = (PdfReader(io.BytesIO(raw)).pages[0].extract_text() or "").replace(" ", "")
    except Exception:
        return None
    return envelope.envelope_id not in text


def reissue(envelope, *, email: bool = False) -> dict:
    """
    Rebuild the final PDF with its Envelope ID and signatures, re-seal it,
    and record why in the audit trail. The audit anchor is NOT touched — the
    ledger guard would refuse, rightly — so the trail still proves it was
    not cut short; the new file is a new, recorded version of the document.
    """
    from django.core.files.base import ContentFile

    from .utils_esign import build_final_pdf, log_event

    old = _read(envelope.completed_pdf)
    old_sha = hashlib.sha256(old).hexdigest() if old else ""
    pdf = build_final_pdf(envelope)

    info = None
    try:
        from .pades_esign import record_seal, seal_pdf

        pdf, info = seal_pdf(pdf, envelope)
    except Exception:
        logger.exception("NES: could not re-seal envelope %s", envelope.pk)

    envelope.completed_pdf.save(f"{envelope.envelope_id}-signed.pdf", ContentFile(pdf), save=False)
    envelope.save(update_fields=["completed_pdf"])
    new_sha = hashlib.sha256(pdf).hexdigest()

    if info is not None:
        info.pop("audit", None)           # keep the anchor taken at completion
        try:
            record_seal(envelope, info)
        except Exception:
            logger.exception("NES: could not record the re-seal of %s", envelope.pk)

    log_event(
        envelope, "resent",
        note="Final document re-issued: the first copy was missing its Envelope ID "
             "and signatures because of a PDF library update.",
        meta={"reissued": True, "old_sha256": old_sha, "new_sha256": new_sha,
              "at": timezone.now().isoformat()},
    )
    if email:
        from . import esign_notify

        esign_notify.notify_completed(None, envelope)
    return {"envelope": envelope.envelope_id, "old_sha256": old_sha, "new_sha256": new_sha,
            "sealed": bool(info and info.get("status") == "sealed")}


# ─────────────────────────────────────────────────────────────────────────────
# A self-test the doctor can run
# ─────────────────────────────────────────────────────────────────────────────

def stamp_selftest() -> tuple[bool, str]:
    """Stamp a throw-away page exactly as build_final_pdf does, and read it back."""
    try:
        import pypdf
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas as rl_canvas

        from .utils_esign import stamp_page

        base = io.BytesIO()
        c = rl_canvas.Canvas(base, pagesize=(595, 842))
        c.drawString(72, 700, "body")
        c.showPage()
        c.save()
        overlay = io.BytesIO()
        c = rl_canvas.Canvas(overlay, pagesize=(595, 842))
        c.drawString(72, 800, "Envelope ID: SELFTEST0042")
        c.showPage()
        c.save()

        writer = PdfWriter()
        target = writer.add_page(PdfReader(io.BytesIO(base.getvalue())).pages[0])
        stamp_page(target, PdfReader(io.BytesIO(overlay.getvalue())).pages[0], what="self-test page")
        out = io.BytesIO()
        writer.write(out)
        text = PdfReader(io.BytesIO(out.getvalue())).pages[0].extract_text() or ""
        if "SELFTEST0042" not in text.replace(" ", ""):
            return False, f"pypdf {pypdf.__version__}: the stamp did not survive"
        return True, f"pypdf {pypdf.__version__}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
