# accounts/utils_esign_markup_pdf.py
"""
Export an envelope's markup as a PDF: the original pages with the marks burned
on, and the comments laid out in a margin down the right-hand side — the same
thing you get when you print a Word document with comments shown.

Requires reportlab + pypdf, both already used by utils_esign.py.

Layout
------
Each source page is placed on a wider sheet:

    ┌──────────────────────────┬──────────────┐
    │                          │  ① Comment   │
    │   original page, with    │     card     │
    │   highlights / pen /     ├──────────────┤
    │   boxes drawn on top     │  ② Comment   │
    │                          │     card     │
    └──────────────────────────┴──────────────┘

Marks are numbered per page; the number appears both in a badge on the mark
and on its card, so a reader can match them without following a leader line
across a busy page.

Anything that will not fit — an overflowing page, or marks whose document was
replaced during a rework — is listed on continuation pages at the end rather
than being silently dropped.
"""

import io
import logging

from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas as rl_canvas

logger = logging.getLogger(__name__)

# Marker ink density. Mirrors HL_ALPHA in accounts/esign/_markup.html so the
# export looks like what the user drew on screen.
HL_ALPHA = 0.38

MARGIN_MIN = 168.0
MARGIN_MAX = 250.0
MARGIN_RATIO = 0.34

CARD_PAD = 7.0
CARD_GAP = 7.0
BODY_SIZE = 6.6
BODY_LEAD = 8.2
META_SIZE = 5.6
TITLE_SIZE = 7.0

INK = colors.HexColor("#0F172A")
MUTED = colors.HexColor("#64748B")
HAIRLINE = colors.HexColor("#E2E8F0")
PAPER = colors.HexColor("#FBFCFD")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hex(value, fallback="#F4C000"):
    try:
        return colors.HexColor(value or fallback)
    except Exception:
        return colors.HexColor(fallback)


def _tint(hex_color, alpha):
    """Same hue, translucent — reportlab emits the ExtGState for us."""
    base = _hex(hex_color)
    return colors.Color(base.red, base.green, base.blue, alpha=alpha)


def _wrap(text, font, size, width):
    out = []
    for para in str(text or "").splitlines() or [""]:
        out.extend(simpleSplit(para, font, size, width) or [""])
    return out


def _to_pdf_y(frac_y, frac_h, page_h):
    """Our geometry is top-down 0..1; PDF's origin is bottom-left."""
    return page_h * (1.0 - frac_y - frac_h)


# ─────────────────────────────────────────────────────────────────────────────
# Drawing: the marks themselves
# ─────────────────────────────────────────────────────────────────────────────

def _draw_mark(c, mark, page_w, page_h):
    """Draw one mark on the page area and return its badge anchor (x, y)."""
    geo = mark.get("geometry") or {}
    kind = mark.get("kind")
    ink = _hex(mark.get("color_hex"))

    if kind == "freehand":
        c.saveState()
        c.setStrokeColor(ink)
        c.setLineWidth(1.7)
        c.setLineCap(1)
        c.setLineJoin(1)
        for stroke in geo.get("strokes") or []:
            if len(stroke) < 2:
                continue
            path = c.beginPath()
            first = stroke[0]
            path.moveTo(first["x"] * page_w, page_h * (1.0 - first["y"]))
            for point in stroke[1:]:
                path.lineTo(point["x"] * page_w, page_h * (1.0 - point["y"]))
            c.drawPath(path, stroke=1, fill=0)
        c.restoreState()
        bbox = geo.get("bbox") or {"x": 0, "y": 0, "w": 0.02, "h": 0.02}
        return bbox["x"] * page_w, page_h * (1.0 - bbox["y"])

    if kind == "note":
        # No disc here: the numbered badge drawn afterwards IS the pin, and
        # stacking one on the other reads as a smudge.
        x = geo.get("x", 0) * page_w
        y = page_h * (1.0 - geo.get("y", 0))
        return x, y - 3

    x = geo.get("x", 0) * page_w
    w = geo.get("w", 0) * page_w
    h = geo.get("h", 0) * page_h
    y = _to_pdf_y(geo.get("y", 0), geo.get("h", 0), page_h)

    c.saveState()
    if kind == "highlight":
        # Translucent fill, so the text underneath stays readable — this is the
        # PDF equivalent of the multiply blend used in the browser.
        c.setFillColor(_tint(mark.get("color_hex"), HL_ALPHA))
        c.rect(x, y, w, h, stroke=0, fill=1)
    elif kind == "area":
        c.setStrokeColor(ink)
        c.setLineWidth(1.3)
        c.roundRect(x, y, w, h, 3, stroke=1, fill=0)
    elif kind == "strike":
        c.setStrokeColor(ink)
        c.setLineWidth(1.4)
        c.line(x, y + h / 2.0, x + w, y + h / 2.0)
    c.restoreState()

    return x, y + h


def _draw_badge(c, x, y, number, hex_color, radius=6.2):
    """The numbered dot that ties a mark to its card."""
    size = 6.4 if radius >= 6 else 5.8
    c.saveState()
    c.setFillColor(_hex(hex_color))
    c.circle(x, y + 3, radius, stroke=0, fill=1)
    c.setStrokeColor(colors.white)
    c.setLineWidth(0.7)
    c.circle(x, y + 3, radius, stroke=1, fill=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", size)
    c.drawCentredString(x, y + 3 - size * 0.35, str(number))
    c.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
# Drawing: comment cards
# ─────────────────────────────────────────────────────────────────────────────

def _card_lines(mark, inner_w):
    """Pre-compute wrapped text so a card's height is known before drawing."""
    who = mark.get("author") or "Unknown"
    meta = "%s · page %s · %s" % (
        mark.get("role") or "", mark.get("page"), mark.get("created_display") or ""
    )
    body = _wrap(mark.get("text"), "Helvetica", BODY_SIZE, inner_w)

    replies = []
    for reply in (mark.get("replies") or [])[:3]:
        replies.append(("who", reply.get("author") or "Unknown"))
        for line in _wrap(reply.get("text"), "Helvetica", BODY_SIZE - 0.3, inner_w - 6):
            replies.append(("txt", line))
    extra = max(0, len(mark.get("replies") or []) - 3)
    if extra:
        replies.append(("more", "+%d more repl%s" % (extra, "y" if extra == 1 else "ies")))

    return who, meta, body, replies


def _card_height(mark, inner_w):
    _who, _meta, body, replies = _card_lines(mark, inner_w)
    height = CARD_PAD * 2
    height += 11.5                     # author line (clears the badge)
    height += 8.5                      # meta line
    height += 8.5                      # colour label chip
    height += len(body) * BODY_LEAD
    if replies:
        height += 3.0 + len(replies) * (BODY_LEAD - 0.6)
    if mark.get("resolved"):
        height += 8.0
    return height


def _draw_card(c, mark, number, x, top, width):
    inner_w = width - CARD_PAD * 2
    who, meta, body, replies = _card_lines(mark, inner_w)
    height = _card_height(mark, inner_w)
    bottom = top - height
    ink = _hex(mark.get("color_hex"))

    c.saveState()

    c.setFillColor(colors.white)
    c.setStrokeColor(HAIRLINE)
    c.setLineWidth(0.6)
    c.roundRect(x, bottom, width, height, 4, stroke=1, fill=1)

    # Colour spine — the same colour-as-intent cue the UI uses.
    c.setFillColor(ink)
    c.rect(x, bottom, 2.6, height, stroke=0, fill=1)

    cursor = top - CARD_PAD - 7.0
    text_x = x + CARD_PAD + 3.0

    _draw_badge(c, text_x + 4.0, cursor - 1.0, number, mark.get("color_hex"), radius=5.4)

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", TITLE_SIZE)
    c.drawString(text_x + 14.0, cursor, who[:34])
    cursor -= 11.5

    c.setFillColor(MUTED)
    c.setFont("Helvetica", META_SIZE)
    c.drawString(text_x, cursor, meta[:62])
    cursor -= 8.5

    label = mark.get("color_label") or ""
    if mark.get("stale"):
        label += "  ·  Rev %s" % mark.get("revision")
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", META_SIZE)
    c.drawString(text_x, cursor, label.upper()[:44])
    cursor -= 8.5

    c.setFillColor(INK)
    c.setFont("Helvetica", BODY_SIZE)
    for line in body:
        c.drawString(text_x, cursor, line)
        cursor -= BODY_LEAD

    if replies:
        cursor -= 3.0
        c.setStrokeColor(HAIRLINE)
        c.setLineWidth(0.5)
        c.line(text_x, cursor + 5.0, x + width - CARD_PAD, cursor + 5.0)
        for kind, line in replies:
            if kind == "who":
                c.setFillColor(MUTED)
                c.setFont("Helvetica-Bold", META_SIZE)
            elif kind == "more":
                c.setFillColor(MUTED)
                c.setFont("Helvetica-Oblique", META_SIZE)
            else:
                c.setFillColor(INK)
                c.setFont("Helvetica", BODY_SIZE - 0.3)
            c.drawString(text_x + 4.0, cursor, line[:70])
            cursor -= (BODY_LEAD - 0.6)

    if mark.get("resolved"):
        c.setFillColor(colors.HexColor("#16A34A"))
        c.setFont("Helvetica-Bold", META_SIZE)
        by = mark.get("resolved_by") or ""
        c.drawString(text_x, bottom + CARD_PAD - 1.5,
                     ("RESOLVED" + (" by " + by if by else ""))[:46])

    c.restoreState()
    return bottom


def _draw_chrome(c, page_w, page_h, margin_w, envelope, brand, subtitle):
    """Margin background, divider, and the footer stamp."""
    c.saveState()
    c.setFillColor(PAPER)
    c.rect(page_w, 0, margin_w, page_h, stroke=0, fill=1)
    c.setStrokeColor(HAIRLINE)
    c.setLineWidth(0.7)
    c.line(page_w, 0, page_w, page_h)

    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 6.2)
    c.drawString(page_w + CARD_PAD, page_h - 13, subtitle.upper()[:40])

    c.setFont("Helvetica", 5.4)
    stamp = "%s · %s · rev %s" % (
        brand, getattr(envelope, "envelope_id", "")[:18], getattr(envelope, "revision", 1)
    )
    c.drawString(page_w + CARD_PAD, 9, stamp[:52])
    c.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
# Continuation pages
# ─────────────────────────────────────────────────────────────────────────────

def _overflow_pages(writer, leftovers, page_size, envelope, brand, heading):
    """List marks that could not sit beside their page, on plain sheets."""
    from pypdf import PdfReader

    if not leftovers:
        return

    width, height = page_size
    col_w = min(MARGIN_MAX, (width - 90) / 2.0)
    columns = [50.0, 50.0 + col_w + 20.0] if width > (col_w * 2 + 110) else [50.0]

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(width, height))

    col = 0
    top = height - 64

    def new_sheet():
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(50, height - 44, heading)
        c.setStrokeColor(HAIRLINE)
        c.line(50, height - 52, width - 50, height - 52)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 5.6)
        c.drawString(50, 26, "%s · %s · rev %s" % (
            brand, getattr(envelope, "envelope_id", "")[:24],
            getattr(envelope, "revision", 1)))

    new_sheet()

    for number, mark in leftovers:
        needed = _card_height(mark, col_w - CARD_PAD * 2)
        if top - needed < 46:
            col += 1
            if col >= len(columns):
                c.showPage()
                new_sheet()
                col = 0
            top = height - 64
        top = _draw_card(c, mark, number, columns[col], top, col_w) - CARD_GAP

    c.showPage()
    c.save()
    buf.seek(0)
    for page in PdfReader(buf).pages:
        writer.add_page(page)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def render_markup_pdf(envelope, marks, page_iter, brand="UNDP eSign"):
    """
    Core renderer, kept free of Django so it can be exercised directly.

    `marks`     — list of dicts shaped like EnvelopeAnnotation.as_dict()
    `page_iter` — yields (document_id, document_name, page_number, pypdf page)
    """
    from pypdf import PageObject, PdfReader, PdfWriter

    writer = PdfWriter()

    by_page = {}
    orphans = []
    for mark in marks:
        if mark.get("orphaned") or not mark.get("document_id"):
            orphans.append(mark)
            continue
        by_page.setdefault((mark["document_id"], mark["page"]), []).append(mark)

    for group in by_page.values():
        group.sort(key=lambda m: ((m.get("anchor") or {}).get("y", 0),
                                  (m.get("anchor") or {}).get("x", 0)))

    leftovers = []
    fallback_size = (595.0, 842.0)

    for doc_id, doc_name, page_no, page in page_iter:
        # A page carrying /Rotate would put our overlay on its side; bake the
        # rotation into the content stream first so both agree on up.
        try:
            if page.get("/Rotate"):
                page.transfer_rotation_to_content()
        except Exception:
            logger.exception("markup pdf: could not normalise rotation")

        box = page.mediabox
        page_w, page_h = float(box.width), float(box.height)
        margin_w = max(MARGIN_MIN, min(MARGIN_MAX, page_w * MARGIN_RATIO))
        fallback_size = (page_w + margin_w, page_h)

        group = by_page.get((doc_id, page_no), [])

        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=(page_w + margin_w, page_h))

        subtitle = "%s · p%s" % ((doc_name or "Document")[:22], page_no)
        _draw_chrome(c, page_w, page_h, margin_w, envelope, brand, subtitle)

        card_x = page_w + CARD_PAD
        card_w = margin_w - CARD_PAD * 2
        cursor = page_h - 22

        for index, mark in enumerate(group, start=1):
            anchor_x, anchor_y = _draw_mark(c, mark, page_w, page_h)
            _draw_badge(c, max(7.0, min(anchor_x, page_w - 7.0)),
                        min(anchor_y, page_h - 10.0), index, mark.get("color_hex"))

            needed = _card_height(mark, card_w - CARD_PAD * 2)
            if cursor - needed < 22:
                leftovers.append((index, mark))
                continue

            # No leader lines. The cards stack from the top of the margin while
            # the marks sit wherever the reader put them, so a connector would
            # usually be a long diagonal across body text pointing at the wrong
            # vertical position — worse than no line. The numbers do the tying.
            cursor = _draw_card(c, mark, index, card_x, cursor, card_w) - CARD_GAP

        if not group:
            c.setFillColor(MUTED)
            c.setFont("Helvetica-Oblique", 6.2)
            c.drawString(card_x, page_h - 34, "No comments on this page.")

        c.showPage()
        c.save()
        buf.seek(0)

        sheet = PageObject.create_blank_page(width=page_w + margin_w, height=page_h)
        sheet.merge_page(page)                       # original artwork first
        sheet.merge_page(PdfReader(buf).pages[0])    # then marks and cards
        writer.add_page(sheet)

    _overflow_pages(writer, leftovers, fallback_size, envelope, brand,
                    "Comments continued")
    _overflow_pages(writer, list(enumerate(orphans, start=1)), fallback_size,
                    envelope, brand,
                    "Comments on documents removed during rework")

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def build_markup_pdf(envelope, annotations, brand=None, revision=None):
    """
    Django-facing wrapper: resolve documents, normalise annotations, render.
    """
    from django.conf import settings
    from pypdf import PdfReader

    from .utils_esign import document_pdf_bytes

    brand = brand or getattr(settings, "ESIGN_BRAND", "UNDP eSign")

    rows = list(annotations)
    if revision not in (None, "", "all"):
        try:
            rows = [a for a in rows if a.revision == int(revision)]
        except (TypeError, ValueError):
            pass

    marks = [
        a.as_dict(can_moderate=True, current_revision=envelope.revision) for a in rows
    ]

    def pages():
        for doc in envelope.documents.all():
            try:
                raw = document_pdf_bytes(doc)
                reader = PdfReader(io.BytesIO(raw))
            except Exception:
                logger.exception("markup pdf: could not read document %s", doc.pk)
                continue
            for index, page in enumerate(reader.pages, start=1):
                yield doc.id, str(doc), index, page

    return render_markup_pdf(envelope, marks, pages(), brand=brand)
