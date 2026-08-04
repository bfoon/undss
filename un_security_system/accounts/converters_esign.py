# accounts/converters_esign.py
"""
Office → PDF conversion for eSign.

Two paths, picked automatically at runtime:

1. **LibreOffice** (`soffice` on PATH) — full fidelity, handles every Office
   format. Needs an apt package in the Dockerfile; see INSTALL.md §10.

2. **Pure-Python DOCX renderer** — `python-docx` + ReportLab, both already
   pip-installable from requirements.txt. No system packages, no image bloat.
   Handles .docx only, and re-renders rather than reproducing Word's exact
   layout: headings, paragraphs, bold/italic/underline, tables, page breaks
   and lists all come through, but bespoke styling, headers/footers, columns
   and embedded images do not.

If neither is available the module degrades quietly — PDF and image uploads
keep working and the envelope builder explains what's missing.
"""

import io
import os
import shutil
import subprocess
import tempfile

from django.conf import settings

__all__ = [
    "ConversionError",
    "SUPPORTED_OFFICE_EXT",
    "PURE_PYTHON_EXT",
    "is_office_file",
    "convert_to_pdf",
    "conversion_backend_available",
    "supported_upload_ext",
]


class ConversionError(Exception):
    """Raised when a document could not be turned into a PDF."""


#: Everything LibreOffice can take.
SUPPORTED_OFFICE_EXT = (
    ".docx", ".doc", ".odt", ".rtf", ".txt",
    ".xlsx", ".xls", ".ods", ".csv",
    ".pptx", ".ppt", ".odp",
)

#: What the pip-only fallback can take.
PURE_PYTHON_EXT = (".docx",)


def is_office_file(filename: str) -> bool:
    return str(filename or "").lower().endswith(SUPPORTED_OFFICE_EXT)


def _ext(filename: str) -> str:
    name = str(filename or "").lower()
    return ("." + name.rsplit(".", 1)[-1]) if "." in name else ""


# ─────────────────────────────────────────────────────────────────────────────
# Backend detection
# ─────────────────────────────────────────────────────────────────────────────

def _soffice_binary():
    configured = getattr(settings, "ESIGN_SOFFICE_BIN", "soffice")
    return shutil.which(configured) or shutil.which("libreoffice")


def _python_docx_available() -> bool:
    try:
        import docx  # noqa: F401
        return True
    except Exception:
        return False


def supported_upload_ext() -> tuple:
    """Which office extensions this deployment can actually accept right now."""
    if _soffice_binary():
        return SUPPORTED_OFFICE_EXT
    if _python_docx_available():
        return PURE_PYTHON_EXT
    return ()


def conversion_backend_available():
    """(ok, description) — used by the envelope builder and handy from a shell."""
    binary = _soffice_binary()
    if binary:
        return True, f"LibreOffice at {binary} — all Office formats supported"
    if _python_docx_available():
        return True, (
            "Pure-Python DOCX renderer (python-docx + ReportLab). Word .docx "
            "files are accepted; install libreoffice-writer in the web image "
            "for other formats and exact layout fidelity."
        )
    return False, (
        "No converter available — add python-docx to requirements.txt for "
        ".docx support, or libreoffice-writer to the web image for every "
        "Office format. PDF and image uploads work either way."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def convert_to_pdf(raw: bytes, filename: str) -> bytes:
    """Convert an office document to PDF bytes."""
    if not raw:
        raise ConversionError("The uploaded file is empty.")

    if _soffice_binary():
        out = _convert_via_soffice(raw, filename)
    elif _ext(filename) in PURE_PYTHON_EXT and _python_docx_available():
        out = _convert_docx_pure(raw)
    elif _ext(filename) in SUPPORTED_OFFICE_EXT and _python_docx_available():
        raise ConversionError(
            f"{_ext(filename).upper().lstrip('.')} files need LibreOffice. "
            "Upload this document as a PDF, or install libreoffice-writer in "
            "the web image."
        )
    else:
        raise ConversionError(conversion_backend_available()[1])

    if out[:5] != b"%PDF-":
        raise ConversionError("Conversion produced something that is not a PDF.")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Backend 1 — headless LibreOffice
# ─────────────────────────────────────────────────────────────────────────────

def _convert_via_soffice(raw: bytes, filename: str) -> bytes:
    binary = _soffice_binary()
    timeout = int(getattr(settings, "ESIGN_CONVERT_TIMEOUT", 120))
    base = os.path.basename(filename) or "document.docx"
    stem = os.path.splitext(base)[0] or "document"

    with tempfile.TemporaryDirectory(prefix="esign-convert-") as tmp:
        src = os.path.join(tmp, base)
        outdir = os.path.join(tmp, "out")
        profile = os.path.join(tmp, "profile")
        os.makedirs(outdir, exist_ok=True)

        with open(src, "wb") as fh:
            fh.write(raw)

        cmd = [
            binary,
            "--headless", "--norestore", "--nolockcheck",
            "--nodefault", "--nofirststartwizard",
            # A private profile per call, so two gunicorn workers converting at
            # the same time don't deadlock on a shared LibreOffice lock file.
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf",
            "--outdir", outdir,
            src,
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise ConversionError(f"LibreOffice timed out after {timeout}s.")

        produced = os.path.join(outdir, stem + ".pdf")
        if not os.path.exists(produced):
            err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            raise ConversionError(err[:200] or "LibreOffice produced no output file.")

        with open(produced, "rb") as fh:
            return fh.read()


# ─────────────────────────────────────────────────────────────────────────────
# Backend 2 — pure Python (.docx only), pip-installable
# ─────────────────────────────────────────────────────────────────────────────

def _convert_docx_pure(raw: bytes) -> bytes:
    from docx import Document
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph as DocxParagraph
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    doc = Document(io.BytesIO(raw))
    ss = getSampleStyleSheet()

    styles = {
        "body": ParagraphStyle("body", parent=ss["Normal"], fontSize=10,
                               leading=14, spaceAfter=6),
        "h0": ParagraphStyle("h0", parent=ss["Title"], fontSize=18,
                             leading=22, spaceAfter=10),
        "h1": ParagraphStyle("h1", parent=ss["Heading1"], fontSize=14,
                             leading=18, spaceBefore=10, spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12,
                             leading=16, spaceBefore=8, spaceAfter=5),
        "h3": ParagraphStyle("h3", parent=ss["Heading3"], fontSize=11,
                             leading=15, spaceBefore=6, spaceAfter=4),
        "cell": ParagraphStyle("cell", parent=ss["Normal"], fontSize=9, leading=12),
        "bullet": ParagraphStyle("bullet", parent=ss["Normal"], fontSize=10,
                                 leading=14, leftIndent=14, bulletIndent=4,
                                 spaceAfter=3),
    }
    align = {"CENTER": TA_CENTER, "RIGHT": TA_RIGHT, "JUSTIFY": TA_JUSTIFY}

    def esc(text: str) -> str:
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def runs_to_markup(paragraph) -> str:
        parts = []
        for run in paragraph.runs:
            text = esc(run.text)
            if not text:
                # A page break lives inside an otherwise empty run.
                continue
            if run.bold:
                text = f"<b>{text}</b>"
            if run.italic:
                text = f"<i>{text}</i>"
            if run.underline:
                text = f"<u>{text}</u>"
            parts.append(text)
        return "".join(parts) or esc(paragraph.text)

    def has_page_break(paragraph) -> bool:
        xml = paragraph._p.xml
        return 'w:br' in xml and 'type="page"' in xml

    def style_for(paragraph):
        name = (paragraph.style.name or "").lower()
        if name.startswith("title"):
            return styles["h0"]
        if name.startswith("heading 1"):
            return styles["h1"]
        if name.startswith("heading 2"):
            return styles["h2"]
        if name.startswith("heading"):
            return styles["h3"]
        if "list" in name:
            return styles["bullet"]
        base = styles["body"]
        try:
            fmt = paragraph.paragraph_format.alignment
            if fmt is not None and str(fmt).split(".")[-1].split(" ")[0] in align:
                key = str(fmt).split(".")[-1].split(" ")[0]
                return ParagraphStyle(f"body-{key}", parent=base, alignment=align[key])
        except Exception:
            pass
        return base

    def render_table(tbl) -> Table:
        rows = []
        for row in tbl.rows:
            cells = []
            for cell in row.cells:
                text = "<br/>".join(esc(p.text) for p in cell.paragraphs) or "&nbsp;"
                cells.append(Paragraph(text, styles["cell"]))
            rows.append(cells)
        if not rows:
            return None

        usable = A4[0] - 36 * mm
        cols = max(len(r) for r in rows)
        rows = [r + [Paragraph("&nbsp;", styles["cell"])] * (cols - len(r)) for r in rows]

        t = Table(rows, colWidths=[usable / cols] * cols, repeatRows=1)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9CA3AF")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    story = []
    body = doc.element.body

    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]

        if tag == "p":
            paragraph = DocxParagraph(child, doc)
            if has_page_break(paragraph):
                story.append(PageBreak())
            markup = runs_to_markup(paragraph)
            if not markup.strip():
                story.append(Spacer(1, 6))
                continue
            style = style_for(paragraph)
            if style is styles["bullet"]:
                story.append(Paragraph(markup, style, bulletText="•"))
            else:
                story.append(Paragraph(markup, style))

        elif tag == "tbl":
            table = render_table(DocxTable(child, doc))
            if table is not None:
                story.append(Spacer(1, 4))
                story.append(table)
                story.append(Spacer(1, 8))

    if not story:
        story.append(Paragraph("(This document appears to be empty.)", styles["body"]))

    buf = io.BytesIO()
    SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    ).build(story)
    return buf.getvalue()
