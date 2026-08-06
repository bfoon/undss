# accounts/utils_esign.py
"""
UN PASS — eSign helpers: signature image handling, PDF stamping (with the
DocuSign-style envelope token on every page) and the Certificate of Completion.

Requires: reportlab, pypdf, Pillow  (all already used elsewhere in the project)
"""

import base64
import io
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .converters_esign import ConversionError, convert_to_pdf, is_office_file
from .models_esign import Envelope, EnvelopeEvent, EnvelopeRecipient, SignatureField

def esign_brand() -> str:
    """Wordmark printed down the page border and in the certificate header.
    Override with ESIGN_BRAND in settings.py (e.g. "UNDP SoftSign")."""
    return getattr(settings, "ESIGN_BRAND", "UNDP eSign")


UN_BLUE = colors.HexColor("#009EDB")
UN_DARK = colors.HexColor("#005A8B")
GREY = colors.HexColor("#6B7280")
LIGHT = colors.HexColor("#9CA3AF")


# ─────────────────────────────────────────────────────────────────────────────
# Request / audit helpers
# ─────────────────────────────────────────────────────────────────────────────

def client_ip(request):
    if not request:
        return None
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def user_agent(request):
    if not request:
        return ""
    return (request.META.get("HTTP_USER_AGENT") or "")[:300]


def log_event(envelope, event, request=None, recipient=None, actor=None, note="", meta=None):
    """Never let audit logging break a flow."""
    try:
        return EnvelopeEvent.objects.create(
            envelope=envelope,
            recipient=recipient,
            actor=actor if (actor and getattr(actor, "is_authenticated", False)) else None,
            event=event,
            note=(note or "")[:300],
            ip=client_ip(request),
            user_agent=user_agent(request),
            meta=meta or {},
        )
    except Exception:
        return None


def sign_url(request, recipient) -> str:
    path = reverse("accounts:esign_sign", args=[recipient.token])
    if request is not None:
        return request.build_absolute_uri(path)
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    return f"{base}{path}" if base else path


def review_url(request, recipient) -> str:
    path = reverse("accounts:esign_review", args=[recipient.token])
    if request is not None:
        return request.build_absolute_uri(path)
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    return f"{base}{path}" if base else path


# ─────────────────────────────────────────────────────────────────────────────
# Signature images
# ─────────────────────────────────────────────────────────────────────────────

_DATA_URL_RE = re.compile(r"^data:image/(png|jpe?g|webp);base64,(?P<b64>.+)$", re.I | re.S)
MAX_SIGNATURE_BYTES = 4 * 1024 * 1024  # 4 MB


def decode_signature_data_url(data_url: str, max_width: int = 900):
    """
    Accepts a browser-produced data: URL (typed canvas render, freehand canvas
    or uploaded picture) and returns a trimmed, transparent PNG ContentFile.
    Returns None if the payload is unusable.
    """
    if not data_url:
        return None

    m = _DATA_URL_RE.match(data_url.strip())
    if not m:
        return None

    try:
        raw = base64.b64decode(m.group("b64"), validate=False)
    except Exception:
        return None

    if not raw or len(raw) > MAX_SIGNATURE_BYTES:
        return None

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        img = img.convert("RGBA")
    except Exception:
        return None

    img = _make_background_transparent(img)
    img = _trim_transparent(img)

    if img.width == 0 or img.height == 0:
        return None

    if img.width > max_width:
        ratio = max_width / float(img.width)
        img = img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return ContentFile(buf.getvalue())


def _make_background_transparent(img: Image.Image) -> Image.Image:
    """
    Make the paper disappear so a signature sits on the document with no white
    box behind it — the same result whether it was typed, drawn, or photographed.

    Fully transparent above `cut`, fully opaque below `keep`, and a linear
    alpha ramp between the two so edges feather instead of showing a hard
    jagged outline. Tunable with ESIGN_SIGNATURE_CUT / _KEEP.
    """
    cut = int(getattr(settings, "ESIGN_SIGNATURE_CUT", 232))   # >= this -> gone
    keep = int(getattr(settings, "ESIGN_SIGNATURE_KEEP", 150))  # <= this -> solid
    span = max(1, cut - keep)

    try:
        px = list(img.getdata())
        out = []
        for r, g, b, a in px:
            if a == 0:
                out.append((r, g, b, 0))
                continue
            lum = (r * 299 + g * 587 + b * 114) // 1000
            if lum >= cut:
                out.append((r, g, b, 0))
            elif lum <= keep:
                out.append((r, g, b, a))
            else:
                ramp = int(a * (cut - lum) / span)
                out.append((r, g, b, max(0, min(a, ramp))))
        img.putdata(out)
    except Exception:
        pass
    return img


def _trim_transparent(img: Image.Image, pad: int = 6) -> Image.Image:
    try:
        bbox = img.getbbox()
        if not bbox:
            return img
        left, top, right, bottom = bbox
        left = max(0, left - pad)
        top = max(0, top - pad)
        right = min(img.width, right + pad)
        bottom = min(img.height, bottom + pad)
        return img.crop((left, top, right, bottom))
    except Exception:
        return img


# ─────────────────────────────────────────────────────────────────────────────
# Document intake
# ─────────────────────────────────────────────────────────────────────────────

def ensure_pdf_bytes(django_file, filename: str = "") -> bytes:
    """
    Returns PDF bytes for any supported upload.

      • PDF                     → passed through untouched
      • PNG / JPG               → wrapped into a single A4 page by Pillow
      • DOCX/DOC/ODT/XLSX/PPTX… → handed to headless LibreOffice by
                                  converters_esign (optional — see INSTALL §10)

    Prefer `document_pdf_bytes(doc)` on an EnvelopeDocument — it caches the
    result so a document is only ever converted once.
    """
    django_file.seek(0)
    raw = django_file.read()
    django_file.seek(0)

    if raw[:5] == b"%PDF-":
        return raw

    name = filename or getattr(django_file, "name", "") or ""

    if is_office_file(name):
        try:
            return convert_to_pdf(raw, name)
        except ConversionError as exc:
            raise ValueError(str(exc))

    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise ValueError(
            "Unsupported document type. Upload a PDF, an Office document "
            "(DOCX, XLSX, PPTX, ODT) or a PNG/JPG image."
        )

    buf = io.BytesIO()
    page_w, page_h = A4
    c = rl_canvas.Canvas(buf, pagesize=A4)
    ratio = min((page_w - 40) / img.width, (page_h - 40) / img.height)
    w, h = img.width * ratio, img.height * ratio
    tmp = io.BytesIO()
    img.save(tmp, format="PNG")
    tmp.seek(0)
    from reportlab.lib.utils import ImageReader

    c.drawImage(ImageReader(tmp), (page_w - w) / 2, (page_h - h) / 2, w, h)
    c.showPage()
    c.save()
    return buf.getvalue()


def document_pdf_bytes(doc, force: bool = False) -> bytes:
    """
    PDF bytes for an EnvelopeDocument, converting once and caching the result
    in `doc.converted_pdf`. Every consumer (viewer, placement UI, stamping,
    page counting) goes through here, so LibreOffice runs a single time per
    uploaded file rather than on every request.
    """
    cached = getattr(doc, "converted_pdf", None)
    if cached and not force:
        try:
            cached.open("rb")
            raw = cached.read()
            cached.close()
            if raw[:5] == b"%PDF-":
                return raw
        except Exception:
            pass

    raw = ensure_pdf_bytes(doc.file, filename=doc.name or "")

    source_is_pdf = False
    try:
        doc.file.seek(0)
        source_is_pdf = doc.file.read(5) == b"%PDF-"
        doc.file.seek(0)
    except Exception:
        pass

    if not source_is_pdf and hasattr(doc, "converted_pdf"):
        try:
            stem = (doc.name or "document").rsplit(".", 1)[0][:120]
            doc.converted_pdf.save(f"{stem}.pdf", ContentFile(raw), save=True)
        except Exception:
            # Cache write failed (missing column, read-only media). Not fatal —
            # we already have the bytes; we just reconvert next time.
            logging.getLogger(__name__).warning(
                "eSign: could not cache converted PDF for document %s", getattr(doc, "pk", "?")
            )

    return raw


def prepare_document(doc) -> int:
    """
    Convert an uploaded document to PDF NOW, cache it, and record the page count.

    Deliberately lets failures propagate. `pdf_page_count` swallows errors and
    returns 1, which meant a Word file that failed to convert still produced a
    perfectly normal-looking envelope — and the first person to discover it was
    the signer, staring at an error. Conversion problems belong to the sender,
    at upload time, while they can still do something about them.
    """
    raw = document_pdf_bytes(doc)
    if not raw or raw[:5] != b"%PDF-":
        raise ValueError(
            f"{doc.name or 'The document'} could not be converted to PDF."
        )

    doc.page_count = len(PdfReader(io.BytesIO(raw)).pages)
    doc.save(update_fields=["page_count"])
    return doc.page_count


def pdf_page_count(doc_or_file) -> int:
    try:
        raw = _bytes_for(doc_or_file)
        return len(PdfReader(io.BytesIO(raw)).pages)
    except Exception:
        return 1


def pdf_page_sizes(doc_or_file):
    """Returns [(width_pt, height_pt), ...] used by the placement UI."""
    try:
        raw = _bytes_for(doc_or_file)
        sizes = []
        for p in PdfReader(io.BytesIO(raw)).pages:
            box = p.mediabox
            sizes.append((float(box.width), float(box.height)))
        return sizes
    except Exception:
        return [(float(A4[0]), float(A4[1]))]


def _bytes_for(doc_or_file) -> bytes:
    """Accepts either an EnvelopeDocument or a raw Django file."""
    if hasattr(doc_or_file, "converted_pdf"):
        return document_pdf_bytes(doc_or_file)
    return ensure_pdf_bytes(doc_or_file)


# ─────────────────────────────────────────────────────────────────────────────
# Stamping
# ─────────────────────────────────────────────────────────────────────────────

def _draw_envelope_token(c, width, height, envelope):
    """
    Branded page marks, in the style DocuSign uses:
      • the wordmark and full envelope ID printed vertically down the left
        border of every page, and
      • a light grey line across the top carrying the same ID.
    """
    brand = esign_brand()
    border_label = f"{brand} Envelope ID: {envelope.envelope_id}"

    c.saveState()
    c.setFont("Helvetica", 6.5)
    c.setFillColor(LIGHT)
    c.drawString(12 * mm, height - 8 * mm, f"Envelope ID: {envelope.envelope_id}")
    c.drawRightString(width - 12 * mm, height - 8 * mm, brand)
    c.setStrokeColor(colors.HexColor("#E5E7EB"))
    c.setLineWidth(0.4)
    c.line(12 * mm, height - 9.6 * mm, width - 12 * mm, height - 9.6 * mm)
    c.restoreState()

    # Vertical border stamp: brand in a slightly stronger weight, ID after it.
    c.saveState()
    c.translate(6.5 * mm, 20 * mm)
    c.rotate(90)
    c.setFont("Helvetica-Bold", 6.5)
    c.setFillColor(GREY)
    c.drawString(0, 0, brand)
    brand_w = c.stringWidth(brand, "Helvetica-Bold", 6.5)
    c.setFont("Helvetica", 6.5)
    c.setFillColor(LIGHT)
    c.drawString(brand_w + 4, 0, f"Envelope ID: {envelope.envelope_id}")
    c.restoreState()


def _fit_font_size(c, text, max_w, max_h, font="Helvetica", start=11.0):
    size = min(start, max_h * 0.72)
    while size > 5.0 and c.stringWidth(text, font, size) > max_w:
        size -= 0.5
    return max(size, 5.0)


def _draw_field(c, field, width, height, envelope):
    x = field.x * width
    w = field.w * width
    h = field.h * height
    y = height - (field.y * height) - h

    kind = field.kind

    if kind in (SignatureField.KIND_SIGNATURE, SignatureField.KIND_INITIALS):
        if not field.image:
            return
        try:
            from reportlab.lib.utils import ImageReader

            field.image.open("rb")
            data = field.image.read()
            field.image.close()
            reader = ImageReader(io.BytesIO(data))
            iw, ih = reader.getSize()

            caption_h = 9 if kind == SignatureField.KIND_SIGNATURE else 0
            avail_h = max(h - caption_h, 6)
            ratio = min(w / iw, avail_h / ih)
            dw, dh = iw * ratio, ih * ratio
            c.drawImage(
                reader,
                x,
                y + caption_h + (avail_h - dh) / 2.0,
                dw,
                dh,
                mask="auto",
                preserveAspectRatio=True,
                anchor="sw",
            )

            if caption_h:
                rec = field.recipient
                stamp_time = (field.filled_at or timezone.now())
                stamp_time = timezone.localtime(stamp_time)
                c.saveState()
                c.setFont("Helvetica", 5.6)
                c.setFillColor(LIGHT)
                c.drawString(
                    x,
                    y + 2.5,
                    f"Signed by {rec.name} · {rec.email} · "
                    f"{stamp_time:%d %b %Y %H:%M %Z} · Token {rec.short_token}",
                )
                c.restoreState()
        except Exception:
            pass
        return

    if kind == SignatureField.KIND_CHECKBOX:
        c.saveState()
        c.setStrokeColor(GREY)
        c.setLineWidth(0.8)
        box = min(w, h, 12)
        c.rect(x, y + (h - box) / 2, box, box, stroke=1, fill=0)
        if field.value == "1":
            c.setStrokeColor(UN_DARK)
            c.setLineWidth(1.4)
            c.line(x + box * 0.2, y + (h - box) / 2 + box * 0.5,
                   x + box * 0.45, y + (h - box) / 2 + box * 0.22)
            c.line(x + box * 0.45, y + (h - box) / 2 + box * 0.22,
                   x + box * 0.82, y + (h - box) / 2 + box * 0.78)
        c.restoreState()
        return

    text = (field.value or "").strip()
    if not text:
        return

    c.saveState()
    c.setFillColor(colors.black)
    lines = text.splitlines() or [text]
    size = _fit_font_size(c, max(lines, key=len), w, h / max(len(lines), 1))
    c.setFont("Helvetica", size)
    line_h = size * 1.18
    top = y + h - line_h + (line_h - size) / 2
    for i, ln in enumerate(lines[:6]):
        c.drawString(x, top - (i * line_h), ln)
    c.restoreState()


def build_final_pdf(envelope: Envelope) -> bytes:
    """
    Merge every document in the envelope, stamp all completed fields and the
    envelope token on each page, and return the flattened PDF bytes.
    """
    writer = PdfWriter()
    fields = list(
        envelope.fields.select_related("recipient", "document").all()
    )

    for doc in envelope.documents.all():
        raw = document_pdf_bytes(doc)
        reader = PdfReader(io.BytesIO(raw))

        for page_index, page in enumerate(reader.pages, start=1):
            box = page.mediabox
            width, height = float(box.width), float(box.height)

            overlay_buf = io.BytesIO()
            c = rl_canvas.Canvas(overlay_buf, pagesize=(width, height))
            _draw_envelope_token(c, width, height, envelope)

            for f in fields:
                if f.document_id == doc.id and f.page == page_index and f.is_filled:
                    _draw_field(c, f, width, height, envelope)

            c.showPage()
            c.save()
            overlay_buf.seek(0)

            try:
                overlay_page = PdfReader(overlay_buf).pages[0]
                page.merge_page(overlay_page)
            except Exception:
                pass

            writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Certificate of Completion (the audit trail document)
# ─────────────────────────────────────────────────────────────────────────────

def build_certificate_pdf(envelope: Envelope) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=18 * mm,
        bottomMargin=16 * mm,
        title=f"Certificate of Completion — {envelope.envelope_id}",
    )

    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=16, textColor=UN_DARK, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=8, textColor=GREY)
    h2 = ParagraphStyle(
        "h2", parent=ss["Heading2"], fontSize=10.5, textColor=UN_DARK, spaceBefore=10, spaceAfter=4
    )
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=8.4, leading=11)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=7.2, leading=9, textColor=GREY)

    story = []
    story.append(Paragraph("Certificate of Completion", h1))
    story.append(
        Paragraph(
            f"{esign_brand()} — tamper-evident audit trail",
            sub,
        )
    )
    story.append(Spacer(1, 8))

    summary = [
        ["Envelope ID", envelope.envelope_id],
        ["Subject", envelope.subject],
        ["Status", envelope.get_status_display()],
        ["Reference", envelope.reference or "—"],
        ["Initiated by", f"{envelope.created_by} " if envelope.created_by else "—"],
        [
            "Created",
            timezone.localtime(envelope.created_at).strftime("%d %b %Y %H:%M %Z"),
        ],
        [
            "Sent",
            timezone.localtime(envelope.sent_at).strftime("%d %b %Y %H:%M %Z")
            if envelope.sent_at
            else "—",
        ],
        [
            "Completed",
            timezone.localtime(envelope.completed_at).strftime("%d %b %Y %H:%M %Z")
            if envelope.completed_at
            else "—",
        ],
        ["Signing order", "Sequential" if envelope.enforce_order else "Parallel"],
        ["Revision", str(getattr(envelope, "revision", 1))],
        ["Documents", ", ".join(str(d) for d in envelope.documents.all()) or "—"],
    ]
    t = Table(summary, colWidths=[34 * mm, 130 * mm])
    t.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.2),
                ("TEXTCOLOR", (0, 0), (0, -1), UN_DARK),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#E5E7EB")),
            ]
        )
    )
    story.append(t)

    # -- recipients -----------------------------------------------------
    story.append(Paragraph("Recipients", h2))
    rows = [["#", "Name / Email", "Role", "Token", "Status", "IP", "Timestamp"]]
    for r in envelope.recipients.all().order_by("order", "id"):
        ts = r.signed_at or r.viewed_at or r.sent_at
        rows.append(
            [
                str(r.order),
                Paragraph(f"<b>{_esc(r.name)}</b><br/>{_esc(r.email)}", small),
                r.get_role_display(),
                r.short_token,
                r.get_status_display(),
                r.signed_ip or "—",
                timezone.localtime(ts).strftime("%d %b %Y %H:%M") if ts else "—",
            ]
        )
    rt = Table(rows, colWidths=[8 * mm, 52 * mm, 24 * mm, 22 * mm, 20 * mm, 22 * mm, 26 * mm], repeatRows=1)
    rt.setStyle(_table_style())
    story.append(rt)

    # -- audit trail ----------------------------------------------------
    story.append(Paragraph("Audit trail", h2))
    rows = [["Timestamp", "Event", "Actor", "IP address", "Detail"]]
    for e in envelope.events.select_related("recipient", "actor").all():
        actor = e.recipient.name if e.recipient else (str(e.actor) if e.actor else "System")
        rows.append(
            [
                timezone.localtime(e.at).strftime("%d %b %Y %H:%M:%S"),
                e.get_event_display(),
                Paragraph(_esc(actor), small),
                e.ip or "—",
                Paragraph(_esc(e.note or "—"), small),
            ]
        )
    at = Table(rows, colWidths=[30 * mm, 30 * mm, 34 * mm, 22 * mm, 58 * mm], repeatRows=1)
    at.setStyle(_table_style())
    story.append(at)

    # -- comments -------------------------------------------------------
    try:
        comments = list(envelope.comments.filter(is_internal=False))
    except Exception:
        comments = []

    if comments:
        story.append(Paragraph("Comments", h2))
        rows = [["Timestamp", "Author", "Role", "Comment"]]
        for c in comments:
            rows.append([
                timezone.localtime(c.created_at).strftime("%d %b %Y %H:%M"),
                Paragraph(_esc(c.display_name), small),
                c.role_label,
                Paragraph(_esc(c.text), small),
            ])
        ct = Table(rows, colWidths=[26 * mm, 34 * mm, 22 * mm, 82 * mm], repeatRows=1)
        ct.setStyle(_table_style())
        story.append(ct)

    story.append(Spacer(1, 10))
    story.append(
        KeepTogether(
            Paragraph(
                "This certificate is generated automatically and forms part of the "
                "signed record. Each signature is bound to the recipient token shown "
                "above and to the envelope ID printed on every page of the document. "
                "Any modification of the document after completion invalidates this "
                "certificate.",
                small,
            )
        )
    )

    def _footer(c, _doc):
        c.saveState()
        c.setFont("Helvetica", 6.5)
        c.setFillColor(LIGHT)
        c.drawString(16 * mm, 10 * mm, f"{esign_brand()} Envelope ID: {envelope.envelope_id}")
        c.drawRightString(A4[0] - 16 * mm, 10 * mm, f"Page {c.getPageNumber()}")
        c.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _table_style():
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF6FC")),
            ("TEXTCOLOR", (0, 0), (-1, 0), UN_DARK),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.4),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )


def _esc(v) -> str:
    return (
        str(v or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Finalisation
# ─────────────────────────────────────────────────────────────────────────────

def finalize_envelope(envelope: Envelope, request=None) -> Envelope:
    """Stamp the PDF, build the certificate, mark complete. Idempotent-ish."""
    pdf_bytes = build_final_pdf(envelope)
    envelope.completed_at = envelope.completed_at or timezone.now()
    envelope.status = Envelope.STATUS_COMPLETED
    envelope.save(update_fields=["completed_at", "status"])

    envelope.completed_pdf.save(
        f"{envelope.envelope_id}-signed.pdf", ContentFile(pdf_bytes), save=False
    )
    cert = build_certificate_pdf(envelope)
    envelope.certificate_pdf.save(
        f"{envelope.envelope_id}-certificate.pdf", ContentFile(cert), save=False
    )
    envelope.save(update_fields=["completed_pdf", "certificate_pdf"])

    log_event(envelope, "completed", request=request, note="All signatures collected.")
    return envelope


def envelope_is_expired(envelope: Envelope) -> bool:
    return bool(
        envelope.expires_at
        and envelope.status == Envelope.STATUS_SENT
        and envelope.expires_at < timezone.now()
    )


def due_for_reminder(envelope: Envelope) -> bool:
    if not (envelope.reminders_enabled and envelope.status == Envelope.STATUS_SENT):
        return False
    last = envelope.last_reminded_at or envelope.sent_at
    if not last:
        return False
    return timezone.now() - last >= timedelta(days=max(1, envelope.reminder_days))
