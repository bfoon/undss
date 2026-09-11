# accounts/pdf_tools_esign.py
"""
UN PASS — eSign Studio PDF operations.

Every function here takes bytes and returns bytes (or a list of named bytes).
Nothing touches the database or the request, which keeps the tools easy to test
and lets the workflow engine reuse them.

Requires pypdf >= 4, reportlab, Pillow — already used by utils_esign.py.

Rotation, once, properly
------------------------
`page.rotate(90)` only sets the /Rotate flag. Anything later drawn on that page
with ReportLab — a watermark, a page number, an edit, an eSign field — is
placed in the *unrotated* coordinate space and lands in the wrong corner.
`normalise_rotation` bakes the flag into the page content, so what you see is
what every later stamp measures. Every operation here normalises its output.
"""

import base64
import io
import logging
import re
import zipfile

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

logger = logging.getLogger(__name__)


class PdfToolError(ValueError):
    """A problem the person can fix: wrong password, bad page range, not a PDF."""


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────

def is_pdf(raw: bytes) -> bool:
    return bool(raw) and raw[:1024].lstrip()[:5] == b"%PDF-"


def open_pdf(raw: bytes, password: str = "") -> PdfReader:
    if not is_pdf(raw):
        raise PdfToolError("That file is not a PDF.")
    try:
        reader = PdfReader(io.BytesIO(raw))
    except PdfReadError as exc:
        raise PdfToolError(f"The PDF could not be read: {exc}")
    if reader.is_encrypted:
        try:
            ok = reader.decrypt(password or "")
        except Exception:  # noqa: BLE001
            ok = 0
        if not ok:
            raise PdfToolError(
                "This PDF is password protected. Use Unlock PDF with its password first."
            )
    return reader


def page_count(raw: bytes) -> int:
    try:
        return len(open_pdf(raw).pages)
    except Exception:  # noqa: BLE001
        return 0


def page_sizes(raw: bytes):
    """[(width_pt, height_pt), ...] as the page is *displayed* (rotation applied)."""
    out = []
    for p in open_pdf(raw).pages:
        w, h = float(p.cropbox.width), float(p.cropbox.height)
        if (int(p.get("/Rotate", 0) or 0) % 180) == 90:
            w, h = h, w
        out.append((w, h))
    return out


def normalise_rotation(page):
    """Bake /Rotate into the content stream so overlays line up."""
    try:
        if int(page.get("/Rotate", 0) or 0) % 360:
            page.transfer_rotation_to_content()
    except Exception:  # noqa: BLE001
        logger.warning("eSign Studio: could not normalise page rotation", exc_info=True)
    return page


def _write(writer: PdfWriter) -> bytes:
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Page ranges
# ─────────────────────────────────────────────────────────────────────────────

_RANGE_RE = re.compile(r"^\s*(\d+|last)\s*(?:-\s*(\d+|last)?\s*)?$", re.I)


def parse_ranges(text: str, total: int):
    """
    "1-3, 5, 8-" -> [[1,2,3],[5],[8..total]]  (1-based, groups preserved)

    `last` works anywhere a number does. An open-ended range ("8-") runs to the
    end. Raises PdfToolError with the exact piece that could not be read.
    """
    if not (text or "").strip():
        raise PdfToolError("Enter the pages, for example 1-3, 5, 8-.")

    groups = []
    for piece in text.split(","):
        if not piece.strip():
            continue
        m = _RANGE_RE.match(piece)
        if not m:
            raise PdfToolError(f"“{piece.strip()}” is not a page or a range.")

        def num(v):
            return total if v.lower() == "last" else int(v)

        start = num(m.group(1))
        has_dash = "-" in piece
        end = num(m.group(2)) if m.group(2) else (total if has_dash else start)
        if start < 1 or end < 1 or start > total or end > total:
            raise PdfToolError(f"“{piece.strip()}” is outside this document (1–{total}).")
        step = 1 if end >= start else -1
        groups.append(list(range(start, end + step, step)))

    if not groups:
        raise PdfToolError("Enter the pages, for example 1-3, 5, 8-.")
    return groups


def flat_pages(text: str, total: int):
    seen, out = set(), []
    for group in parse_ranges(text, total):
        for p in group:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Assemble
# ─────────────────────────────────────────────────────────────────────────────

def merge(files) -> bytes:
    """files: iterable of PDF bytes, in order."""
    writer = PdfWriter()
    count = 0
    for raw in files:
        for page in open_pdf(raw).pages:
            writer.add_page(page)
            normalise_rotation(writer.pages[-1])
            count += 1
    if not count:
        raise PdfToolError("Add at least one PDF to merge.")
    return _write(writer)


def apply_page_plan(raw: bytes, plan) -> bytes:
    """
    Rebuild a document from a plan — the organiser's single save.

    plan: [{"src": 0, "rotate": 90}, {"blank": true, "size": [w, h]}, ...]
      src     zero-based index into the current document (can repeat: duplicate)
      rotate  0 / 90 / 180 / 270, clockwise
      blank   insert an empty page; size defaults to the neighbouring page
    Pages not mentioned are removed.
    """
    reader = open_pdf(raw)
    total = len(reader.pages)
    if not isinstance(plan, list) or not plan:
        raise PdfToolError("A document must keep at least one page.")

    writer = PdfWriter()
    last_size = (float(A4[0]), float(A4[1]))

    for entry in plan:
        if not isinstance(entry, dict):
            raise PdfToolError("Invalid page entry.")
        rotate = int(entry.get("rotate") or 0) % 360
        if rotate not in (0, 90, 180, 270):
            raise PdfToolError("Rotation must be 0, 90, 180 or 270 degrees.")

        if entry.get("blank"):
            size = entry.get("size") or last_size
            try:
                w, h = float(size[0]), float(size[1])
            except (TypeError, ValueError, IndexError):
                w, h = last_size
            writer.add_blank_page(width=w, height=h)
        else:
            try:
                src = int(entry.get("src"))
            except (TypeError, ValueError):
                raise PdfToolError("Invalid page entry.")
            if src < 0 or src >= total:
                raise PdfToolError(f"Page {src + 1} does not exist.")
            writer.add_page(reader.pages[src])

        page = writer.pages[-1]
        if rotate:
            page.rotate(rotate)
        normalise_rotation(page)
        last_size = (float(page.mediabox.width), float(page.mediabox.height))

    return _write(writer)


def select_pages(raw: bytes, pages_1_based) -> bytes:
    reader = open_pdf(raw)
    writer = PdfWriter()
    for p in pages_1_based:
        writer.add_page(reader.pages[p - 1])
        normalise_rotation(writer.pages[-1])
    if not len(writer.pages):
        raise PdfToolError("No pages selected.")
    return _write(writer)


def remove_pages(raw: bytes, pages_1_based) -> bytes:
    total = page_count(raw)
    drop = set(pages_1_based)
    keep = [p for p in range(1, total + 1) if p not in drop]
    if not keep:
        raise PdfToolError("You can't remove every page — a PDF needs at least one.")
    return select_pages(raw, keep)


def rotate_pages(raw: bytes, pages_1_based, degrees: int) -> bytes:
    degrees = int(degrees) % 360
    total = page_count(raw)
    targets = set(pages_1_based) if pages_1_based else set(range(1, total + 1))
    plan = [{"src": i, "rotate": degrees if (i + 1) in targets else 0} for i in range(total)]
    return apply_page_plan(raw, plan)


def split(raw: bytes, mode: str = "ranges", ranges: str = "", every: int = 1, stem: str = "document"):
    """
    Returns [(filename, bytes), ...].

    mode="ranges"  one file per comma-separated group: "1-3, 4-6, 7-"
    mode="every"   a new file every N pages
    """
    reader = open_pdf(raw)
    total = len(reader.pages)

    if mode == "every":
        every = max(1, int(every or 1))
        groups = [list(range(s, min(s + every, total + 1))) for s in range(1, total + 1, every)]
    else:
        groups = parse_ranges(ranges, total)

    if len(groups) < 2 and len(groups[0]) == total:
        raise PdfToolError("That would produce one file identical to the original. Choose smaller ranges.")

    out = []
    for group in groups:
        writer = PdfWriter()
        for p in group:
            writer.add_page(reader.pages[p - 1])
            normalise_rotation(writer.pages[-1])
        label = f"{group[0]}" if len(group) == 1 else f"{group[0]}-{group[-1]}"
        out.append((f"{stem}-pages-{label}.pdf", _write(writer)))
    return out


def zip_files(named_bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used = set()
        for name, data in named_bytes:
            base, n = name, 2
            while name in used:
                stem, dot, ext = base.rpartition(".")
                name = f"{stem}-{n}.{ext}" if dot else f"{base}-{n}"
                n += 1
            used.add(name)
            zf.writestr(name, data)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Convert
# ─────────────────────────────────────────────────────────────────────────────

def images_to_pdf(images, page: str = "a4", margin_mm: float = 10.0) -> bytes:
    """
    images: [(filename, bytes), ...]
    page="a4"    each image centred on A4, turned landscape when the image is wide
    page="fit"   each page is exactly the size of its image
    """
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    count = 0
    margin = max(0.0, float(margin_mm)) * 72 / 25.4

    for name, data in images:
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
        except Exception:  # noqa: BLE001
            raise PdfToolError(f"{name} is not an image this tool can read (PNG, JPG, WEBP, GIF, BMP, TIFF).")

        iw, ih = img.size
        if page == "fit":
            pw, ph = iw * 0.75, ih * 0.75          # 96 dpi px -> pt
            area_w, area_h, ox, oy = pw, ph, 0, 0
        else:
            pw, ph = landscape(A4) if iw > ih else A4
            area_w, area_h, ox, oy = pw - 2 * margin, ph - 2 * margin, margin, margin

        c.setPageSize((pw, ph))
        ratio = min(area_w / iw, area_h / ih)
        dw, dh = iw * ratio, ih * ratio
        tmp = io.BytesIO()
        img.save(tmp, format="JPEG", quality=90)
        tmp.seek(0)
        c.drawImage(ImageReader(tmp), ox + (area_w - dw) / 2, oy + (area_h - dh) / 2, dw, dh)
        c.showPage()
        count += 1

    if not count:
        raise PdfToolError("Add at least one image.")
    c.save()
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Optimise and protect
# ─────────────────────────────────────────────────────────────────────────────

COMPRESS_LEVELS = {
    # name: (max image edge in px, JPEG quality)
    "light": (2400, 85),
    "balanced": (1600, 72),
    "strong": (1100, 55),
}


def compress(raw: bytes, level: str = "balanced") -> bytes:
    """
    Recompress content streams, downscale and re-encode large images, and drop
    duplicate objects. Text and vector content are untouched, so the result is
    still searchable and sharp.
    """
    max_edge, quality = COMPRESS_LEVELS.get(level, COMPRESS_LEVELS["balanced"])
    reader = open_pdf(raw)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    for page in writer.pages:
        normalise_rotation(page)
        try:
            for image in page.images:
                try:
                    pil = image.image
                    if pil is None:
                        continue
                    if pil.mode not in ("RGB", "L"):
                        pil = pil.convert("RGB")
                    if max(pil.size) > max_edge:
                        pil.thumbnail((max_edge, max_edge), Image.LANCZOS)
                    image.replace(pil, quality=quality)
                except Exception:  # noqa: BLE001 - one odd image must not stop the rest
                    continue
        except Exception:  # noqa: BLE001
            pass
        try:
            page.compress_content_streams()
        except Exception:  # noqa: BLE001
            pass

    try:
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    except Exception:  # noqa: BLE001
        pass

    out = _write(writer)
    # Never hand back something bigger than what came in.
    return out if len(out) < len(raw) else raw


def protect(raw: bytes, password: str, allow_printing: bool = True) -> bytes:
    if not password or len(password) < 4:
        raise PdfToolError("Use a password of at least 4 characters.")
    reader = open_pdf(raw)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
        normalise_rotation(writer.pages[-1])
    permissions = None
    try:
        from pypdf.constants import UserAccessPermissions as P

        permissions = P.EXTRACT_TEXT_AND_GRAPHICS
        if allow_printing:
            permissions |= P.PRINT | P.PRINT_TO_REPRESENTATION
    except Exception:  # noqa: BLE001
        permissions = None
    kwargs = {"user_password": password, "owner_password": None, "algorithm": "AES-256"}
    if permissions is not None:
        kwargs["permissions_flag"] = permissions
    try:
        writer.encrypt(**kwargs)
    except Exception:  # noqa: BLE001 - AES needs `cryptography`; fall back to RC4-128
        kwargs.pop("algorithm", None)
        writer.encrypt(**kwargs)
    return _write(writer)


def unlock(raw: bytes, password: str) -> bytes:
    if not is_pdf(raw):
        raise PdfToolError("That file is not a PDF.")
    reader = PdfReader(io.BytesIO(raw))
    if not reader.is_encrypted:
        raise PdfToolError("This PDF has no password. Nothing to unlock.")
    try:
        ok = reader.decrypt(password or "")
    except Exception:  # noqa: BLE001
        ok = 0
    if not ok:
        raise PdfToolError("That password is not correct for this PDF.")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
        normalise_rotation(writer.pages[-1])
    return _write(writer)


# ─────────────────────────────────────────────────────────────────────────────
# Stamps: watermark and page numbers
# ─────────────────────────────────────────────────────────────────────────────

def _overlay_canvas(page):
    """
    A ReportLab canvas for drawing on top of `page` in its *visible* space.

    -> (buffer, canvas, width, height). Origin is the bottom-left of the crop
    box (what viewers show) and width/height are the crop box's. The overlay
    page itself spans every coordinate the original can use, because content
    placed outside an overlay's own page box is clipped when merged — on a
    trimmed or scanned PDF whose box doesn't start at 0,0 that used to cut
    stamps and edits in half.
    """
    box = page.cropbox
    media = page.mediabox
    w, h = float(box.width), float(box.height)
    extent_w = max(float(media.right), float(box.right), w)
    extent_h = max(float(media.top), float(box.top), h)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(extent_w, extent_h))
    c.translate(float(box.left), float(box.bottom))
    return buf, c, w, h


def _overlay(pages_writer, draw):
    """Run draw(canvas, width, height, index) over every page and merge it on."""
    for index, page in enumerate(pages_writer.pages):
        normalise_rotation(page)
        buf, c, w, h = _overlay_canvas(page)
        drew = draw(c, w, h, index)
        c.showPage()
        c.save()
        if drew is False:
            continue
        buf.seek(0)
        page.merge_page(PdfReader(buf).pages[0])


def _hex(value, fallback="#9CA3AF"):
    try:
        return colors.HexColor(value or fallback)
    except Exception:  # noqa: BLE001
        return colors.HexColor(fallback)


def watermark(raw: bytes, text: str, *, size: int = 54, opacity: float = 0.18,
              angle: int = 45, color: str = "#6B7280", position: str = "center",
              pages: str = "") -> bytes:
    text = (text or "").strip()
    if not text:
        raise PdfToolError("Enter the watermark text.")
    reader = open_pdf(raw)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    total = len(writer.pages)
    targets = set(flat_pages(pages, total)) if (pages or "").strip() else None
    tint = _hex(color)
    fill = colors.Color(tint.red, tint.green, tint.blue, alpha=max(0.03, min(1.0, float(opacity))))
    size = max(8, min(200, int(size)))

    def draw(c, w, h, index):
        if targets and (index + 1) not in targets:
            return False
        c.saveState()
        c.setFillColor(fill)
        c.setFont("Helvetica-Bold", size)
        if position == "tile":
            step_x = c.stringWidth(text, "Helvetica-Bold", size) + size * 2
            step_y = size * 4
            c.translate(w / 2, h / 2)
            c.rotate(angle)
            span = int(max(w, h) * 1.5)
            for yy in range(-span, span, int(step_y)):
                for xx in range(-span, span, int(step_x)):
                    c.drawString(xx, yy, text)
        else:
            cy = {"top": h * 0.82, "bottom": h * 0.18}.get(position, h / 2)
            c.translate(w / 2, cy)
            c.rotate(angle if position == "center" else 0)
            c.drawCentredString(0, -size / 3, text)
        c.restoreState()
        return True

    _overlay(writer, draw)
    return _write(writer)


def page_numbers(raw: bytes, *, position: str = "bottom-center", fmt: str = "Page {n} of {total}",
                 start: int = 1, size: int = 9, skip_first: bool = False,
                 color: str = "#374151") -> bytes:
    reader = open_pdf(raw)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    total = len(writer.pages)
    fmt = (fmt or "{n}")[:60]
    size = max(6, min(24, int(size)))
    start = int(start or 1)
    ink = _hex(color, "#374151")
    shown_total = total - (1 if skip_first else 0) + (start - 1)

    def draw(c, w, h, index):
        if skip_first and index == 0:
            return False
        n = index + start - (1 if skip_first else 0)
        try:
            label = fmt.format(n=n, total=shown_total)
        except (KeyError, IndexError, ValueError):
            label = str(n)
        margin = 28
        vert, _, horiz = position.partition("-")
        y = h - margin if vert == "top" else margin - size / 2
        c.saveState()
        c.setFont("Helvetica", size)
        c.setFillColor(ink)
        if horiz == "left":
            c.drawString(margin + 8, y, label)
        elif horiz == "right":
            c.drawRightString(w - margin - 8, y, label)
        else:
            c.drawCentredString(w / 2, y, label)
        c.restoreState()
        return True

    _overlay(writer, draw)
    return _write(writer)


# ─────────────────────────────────────────────────────────────────────────────
# Editor: burn the browser's edits into the document
# ─────────────────────────────────────────────────────────────────────────────

_DATA_URL_RE = re.compile(r"^data:image/(png|jpe?g|webp);base64,(?P<b64>.+)$", re.I | re.S)
MAX_EDIT_IMAGE_BYTES = 6 * 1024 * 1024

FONTS = {
    ("helvetica", False, False): "Helvetica",
    ("helvetica", True, False): "Helvetica-Bold",
    ("helvetica", False, True): "Helvetica-Oblique",
    ("helvetica", True, True): "Helvetica-BoldOblique",
    ("times", False, False): "Times-Roman",
    ("times", True, False): "Times-Bold",
    ("times", False, True): "Times-Italic",
    ("times", True, True): "Times-BoldItalic",
    ("courier", False, False): "Courier",
    ("courier", True, False): "Courier-Bold",
    ("courier", False, True): "Courier-Oblique",
    ("courier", True, True): "Courier-BoldOblique",
}


def decode_image_data_url(data_url: str):
    m = _DATA_URL_RE.match((data_url or "").strip())
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group("b64"), validate=False)
    except Exception:  # noqa: BLE001
        return None
    if not raw or len(raw) > MAX_EDIT_IMAGE_BYTES:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img
    except Exception:  # noqa: BLE001
        return None


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rgba(value, alpha=1.0):
    base = _hex(value, "#000000")
    return colors.Color(base.red, base.green, base.blue, alpha=max(0.0, min(1.0, _f(alpha, 1.0))))


def _draw_edit(c, item, w, h):
    """Coordinates are fractions of the displayed page, origin top-left."""
    kind = item.get("type")
    x = _f(item.get("x")) * w
    y_top = _f(item.get("y")) * h
    iw = _f(item.get("w")) * w
    ih = _f(item.get("h")) * h
    y = h - y_top - ih
    opacity = _f(item.get("opacity"), 1.0)

    c.saveState()
    try:
        if kind in ("whiteout",):
            c.setFillColor(_rgba(item.get("fill") or "#FFFFFF", 1.0))
            c.rect(x, y, iw, ih, stroke=0, fill=1)

        elif kind == "highlight":
            c.setFillColor(_rgba(item.get("color") or "#FACC15", opacity if item.get("opacity") else 0.35))
            c.rect(x, y, iw, ih, stroke=0, fill=1)

        elif kind in ("rect", "ellipse"):
            stroke_w = max(0.0, _f(item.get("stroke_width"), 1.5))
            fill = item.get("fill") or ""
            c.setLineWidth(stroke_w)
            c.setStrokeColor(_rgba(item.get("color") or "#DC2626", opacity))
            if fill:
                c.setFillColor(_rgba(fill, _f(item.get("fill_opacity"), 0.2)))
            if kind == "rect":
                c.rect(x, y, iw, ih, stroke=1 if stroke_w else 0, fill=1 if fill else 0)
            else:
                c.ellipse(x, y, x + iw, y + ih, stroke=1 if stroke_w else 0, fill=1 if fill else 0)

        elif kind in ("line", "arrow"):
            x1, y1 = _f(item.get("x1")) * w, h - _f(item.get("y1")) * h
            x2, y2 = _f(item.get("x2")) * w, h - _f(item.get("y2")) * h
            stroke_w = max(0.3, _f(item.get("stroke_width"), 2.0))
            c.setLineWidth(stroke_w)
            c.setLineCap(1)
            ink = _rgba(item.get("color") or "#DC2626", opacity)
            c.setStrokeColor(ink)
            c.line(x1, y1, x2, y2)
            if kind == "arrow":
                import math

                ang = math.atan2(y2 - y1, x2 - x1)
                head = max(6.0, stroke_w * 4)
                c.setFillColor(ink)
                p = c.beginPath()
                p.moveTo(x2, y2)
                p.lineTo(x2 - head * math.cos(ang - 0.45), y2 - head * math.sin(ang - 0.45))
                p.lineTo(x2 - head * math.cos(ang + 0.45), y2 - head * math.sin(ang + 0.45))
                p.close()
                c.drawPath(p, stroke=0, fill=1)

        elif kind == "ink":
            c.setLineWidth(max(0.3, _f(item.get("stroke_width"), 2.0)))
            c.setLineCap(1)
            c.setLineJoin(1)
            c.setStrokeColor(_rgba(item.get("color") or "#1D4ED8", opacity))
            for stroke in item.get("strokes") or []:
                if len(stroke) < 2:
                    continue
                path = c.beginPath()
                path.moveTo(_f(stroke[0][0]) * w, h - _f(stroke[0][1]) * h)
                for pt in stroke[1:]:
                    path.lineTo(_f(pt[0]) * w, h - _f(pt[1]) * h)
                c.drawPath(path, stroke=1, fill=0)

        elif kind in ("text", "date"):
            text = str(item.get("text") or "")
            if text.strip():
                family = (item.get("font") or "helvetica").lower()
                font = FONTS.get((family, bool(item.get("bold")), bool(item.get("italic"))), "Helvetica")
                size = max(4.0, min(144.0, _f(item.get("size"), 12.0)))
                c.setFont(font, size)
                c.setFillColor(_rgba(item.get("color") or "#111827", opacity))
                if item.get("bg"):
                    c.saveState()
                    c.setFillColor(_rgba(item.get("bg"), 1.0))
                    c.rect(x, y, iw, ih, stroke=0, fill=1)
                    c.restoreState()
                line_h = size * 1.2
                align = item.get("align") or "left"
                top = h - y_top - size * 0.95
                for i, line in enumerate(text.splitlines()[:80]):
                    ly = top - i * line_h
                    if align == "center":
                        c.drawCentredString(x + iw / 2, ly, line)
                    elif align == "right":
                        c.drawRightString(x + iw, ly, line)
                    else:
                        c.drawString(x + 1, ly, line)

        elif kind in ("check", "cross", "dot"):
            c.setStrokeColor(_rgba(item.get("color") or "#15803D", opacity))
            c.setFillColor(_rgba(item.get("color") or "#15803D", opacity))
            c.setLineWidth(max(1.0, min(iw, ih) * 0.12))
            c.setLineCap(1)
            if kind == "check":
                c.line(x + iw * 0.12, y + ih * 0.5, x + iw * 0.4, y + ih * 0.18)
                c.line(x + iw * 0.4, y + ih * 0.18, x + iw * 0.9, y + ih * 0.86)
            elif kind == "cross":
                c.line(x + iw * 0.15, y + ih * 0.15, x + iw * 0.85, y + ih * 0.85)
                c.line(x + iw * 0.15, y + ih * 0.85, x + iw * 0.85, y + ih * 0.15)
            else:
                r = min(iw, ih) * 0.3
                c.circle(x + iw / 2, y + ih / 2, r, stroke=0, fill=1)

        elif kind == "image":
            img = decode_image_data_url(item.get("src") or "")
            if img is not None:
                tmp = io.BytesIO()
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                img.save(tmp, format="PNG")
                tmp.seek(0)
                c.drawImage(ImageReader(tmp), x, y, iw, ih, mask="auto")
    finally:
        c.restoreState()


def burn_edits(raw: bytes, pages, flattened=None) -> bytes:
    """
    pages:     {"1": [item, ...], "3": [...]}  1-based page -> edits
    flattened: {"2": "data:image/jpeg;base64,..."} pages the browser rendered
               with the edits already composited. Those pages are replaced by
               the image, which removes the text underneath a white-out for good
               (and makes that page's text unselectable — that is the point).
    """
    reader = open_pdf(raw)
    pages = pages or {}
    flattened = flattened or {}
    writer = PdfWriter()

    for index, source in enumerate(reader.pages):
        key = str(index + 1)
        writer.add_page(source)
        page = writer.pages[-1]
        normalise_rotation(page)
        # The crop box is what a viewer shows, so it is what the edit
        # coordinates are fractions of. On most PDFs it equals the media box;
        # on trimmed or scanned ones it doesn't, and using the media box would
        # shift every edit.
        box = page.cropbox
        w, h = float(box.width), float(box.height)

        if key in flattened:
            img = decode_image_data_url(flattened[key])
            if img is None:
                raise PdfToolError(f"Page {key} could not be flattened. Try again with fewer pages.")
            buf = io.BytesIO()
            c = rl_canvas.Canvas(buf, pagesize=(w, h))
            tmp = io.BytesIO()
            img.convert("RGB").save(tmp, format="JPEG", quality=88)
            tmp.seek(0)
            c.drawImage(ImageReader(tmp), 0, 0, w, h)
            c.showPage()
            c.save()
            buf.seek(0)
            # Swap the page for the image page: nothing of the original content
            # stream survives, so covered text cannot be copied back out.
            writer.remove_page(len(writer.pages) - 1)
            writer.add_page(PdfReader(buf).pages[0])
            continue

        items = pages.get(key) or []
        if not items:
            continue
        buf, c, w, h = _overlay_canvas(page)
        for item in items[:500]:
            _draw_edit(c, item, w, h)
        c.showPage()
        c.save()
        buf.seek(0)
        page.merge_page(PdfReader(buf).pages[0])

    return _write(writer)


# ─────────────────────────────────────────────────────────────────────────────
# Signature page for documents routed through a workflow signature step
# ─────────────────────────────────────────────────────────────────────────────

def signature_page(signers, *, title="Signatures", subtitle="", reference="", page_size=None):
    """
    Closing page(s) with one signature block per signer. Runs onto more pages
    when there are more signers than fit — nobody is ever left without a box.

    Returns (pdf_bytes, boxes) where boxes[i] = {"page": n, "sig": (x,y,w,h),
    "date": (x,y,w,h)}; n is 1-based within these pages, coordinates are
    fractions of the page with a top-left origin — the shape SignatureField uses.
    """
    pw, ph = page_size or A4
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(pw, ph))
    blue = colors.HexColor("#005A8B")
    grey = colors.HexColor("#6B7280")
    line = colors.HexColor("#CBD5E1")
    margin = 56
    block_h = 112
    gutter = 24
    cols = 2 if len(signers) > 3 else 1
    col_w = (pw - 2 * margin - (gutter if cols == 2 else 0)) / cols

    def header(page_no):
        top = ph - 72
        c.setFillColor(blue)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(margin, top, title if page_no == 1 else f"{title} (continued)")
        c.setFont("Helvetica", 9)
        c.setFillColor(grey)
        if subtitle:
            c.drawString(margin, top - 16, subtitle[:120])
        if reference:
            c.drawRightString(pw - margin, top, reference)
        c.setStrokeColor(line)
        c.setLineWidth(0.6)
        c.line(margin, top - 26, pw - margin, top - 26)
        return top - 52

    page_no = 1
    y_row = header(page_no)
    boxes = []

    for i, s in enumerate(signers):
        col = i % cols
        if col == 0 and i:
            y_row -= block_h
        if col == 0 and y_row - block_h < 48:
            c.showPage()
            page_no += 1
            y_row = header(page_no)

        bx = margin + col * (col_w + gutter)
        by = y_row
        sig_h = 50
        c.setFillColor(colors.HexColor("#F8FAFC"))
        c.setStrokeColor(line)
        c.setLineWidth(0.6)
        c.roundRect(bx, by - sig_h, col_w, sig_h, 4, stroke=1, fill=1)
        c.setFillColor(colors.HexColor("#94A3B8"))
        c.setFont("Helvetica", 7)
        c.drawString(bx + 6, by - 11, "Sign here")
        c.setFillColor(colors.HexColor("#0F172A"))
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(bx, by - sig_h - 14, (s.get("name") or "")[:60])
        c.setFont("Helvetica", 8)
        c.setFillColor(grey)
        role = " · ".join(v for v in [s.get("title") or "", s.get("step") or ""] if v)
        if role:
            c.drawString(bx, by - sig_h - 26, role[:80])
        c.drawString(bx, by - sig_h - 40, "Date:")
        date_w = min(col_w, 200) - 28
        c.setStrokeColor(line)
        c.line(bx + 28, by - sig_h - 42, bx + 28 + date_w, by - sig_h - 42)

        boxes.append({
            "page": page_no,
            "sig": (bx / pw, (ph - by) / ph, col_w / pw, sig_h / ph),
            "date": ((bx + 28) / pw, (ph - (by - sig_h - 31)) / ph, date_w / pw, 12 / ph),
        })

    c.showPage()
    c.save()
    return buf.getvalue(), boxes


def append_pdf(first: bytes, second: bytes) -> bytes:
    return merge([first, second])
