# accounts/agent_docs_esign.py
"""
UN PASS — Agentic Flows: reading documents and pages, and writing new files.

read_text()      any upload -> plain text the AI can read (PDF, Word, Excel,
                 PowerPoint, text, HTML; other Office types via LibreOffice)
fetch_url()      a Knowledge link -> its readable text. Guarded: http(s) only,
                 no private or internal addresses unless ICT allows them, a
                 size cap and a timeout, and every redirect is checked again.
convert()        -> PDF, Word, Excel, CSV, text, Markdown or HTML, from either
                 the document or an earlier card's output
cover_page()     a one-page summary sheet, put in front of the document
"""

from __future__ import annotations

import csv
import html
import io
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_FETCH_BYTES = 3 * 1024 * 1024
MAX_PDF_PAGES = 300

TARGETS = {
    "pdf": ("PDF", "application/pdf"),
    "docx": ("Word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "xlsx": ("Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "csv": ("CSV", "text/csv"),
    "txt": ("Text", "text/plain"),
    "md": ("Markdown", "text/markdown"),
    "html": ("Web page", "text/html"),
}


class DocError(ValueError):
    """A sentence a person can act on."""


def _ext(name):
    name = str(name or "").lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def stem(name):
    """'Contract.docx' -> 'Contract'; 'Contract.docx - terms' stays as it is."""
    base = str(name or "document").rsplit("/", 1)[-1].strip()
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", base)
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", base).strip(" .")
    return base[:120] or "document"


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────

class _TextHTML(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "button", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def html_to_text(raw_html: str):
    parser = _TextHTML()
    try:
        parser.feed(raw_html or "")
        parser.close()
    except Exception:  # noqa: BLE001 - broken markup still gives what it gave
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return parser.title.strip(), text.strip()


def pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                raise DocError("The PDF is password-protected. Unlock it in PDF tools first.")
        out = []
        for i, page in enumerate(reader.pages[:MAX_PDF_PAGES]):
            try:
                t = page.extract_text() or ""
            except Exception:  # noqa: BLE001
                t = ""
            if t.strip():
                out.append(f"[Page {i + 1}]\n{t.strip()}")
        return "\n\n".join(out)
    except DocError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocError(f"The PDF could not be read ({exc.__class__.__name__}).")


def docx_text(raw: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(raw))
    out = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            style = (p.style.name or "").lower() if p.style is not None else ""
            out.append(("# " if style.startswith("heading") or style == "title" else "") + t)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                out.append(" | ".join(cells))
    return "\n".join(out)


def xlsx_text(raw: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets[:10]:
        out.append(f"# Sheet: {ws.title}")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= 2000:
                out.append("… (more rows)")
                break
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                out.append("\t".join(cells).rstrip())
    return "\n".join(out)


def read_text(raw: bytes, name: str) -> str:
    """Any supported file -> text. Images and scans give '' (no OCR here)."""
    if not raw:
        return ""
    ext = _ext(name)
    if raw[:5] == b"%PDF-" or ext == "pdf":
        return pdf_text(raw)
    if ext == "docx":
        return docx_text(raw)
    if ext in ("xlsx", "xlsm"):
        return xlsx_text(raw)
    if ext in ("txt", "md", "csv", "tsv", "json", "xml", "log"):
        return raw.decode("utf-8", "replace")
    if ext in ("html", "htm"):
        return html_to_text(raw.decode("utf-8", "replace"))[1]
    if ext in ("png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"):
        return ""
    # doc, odt, rtf, pptx, xls… -> PDF via the converter NES already uses
    from .converters_esign import ConversionError, convert_to_pdf, is_office_file

    if is_office_file(name):
        try:
            return pdf_text(convert_to_pdf(raw, name))
        except ConversionError as exc:
            raise DocError(f"{name} could not be read: {exc}")
    raise DocError(f"{name}: this kind of file can't be read by an agent.")


# ─────────────────────────────────────────────────────────────────────────────
# Knowledge links — fetched safely
# ─────────────────────────────────────────────────────────────────────────────

def _allowed_hosts():
    return [h.lower().lstrip(".") for h in getattr(settings, "AGENT_LINK_ALLOWED_HOSTS", []) or []]


def _host_allowed_explicitly(host: str) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in _allowed_hosts())


def check_url(url: str) -> str:
    """Raises DocError unless `url` is safe to fetch from the server."""
    parsed = urllib.parse.urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DocError(f"“{url}” is not a web address (it must start with http:// or https://).")
    if parsed.username or parsed.password:
        raise DocError("Web addresses with a username or password in them aren't allowed.")
    host = parsed.hostname
    if _host_allowed_explicitly(host):
        return url
    if getattr(settings, "AGENT_LINK_ALLOW_PRIVATE", False):
        return url
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise DocError(f"The address {host} could not be found.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise DocError(f"{host} is an internal address. ICT can allow it by adding it to AGENT_LINK_ALLOWED_HOSTS.")
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, timeout: int = None):
    """-> {"url", "title", "text"}; raises DocError."""
    check_url(url)
    timeout = timeout or int(getattr(settings, "AGENT_LINK_TIMEOUT", 20))
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={
        "User-Agent": "UN-PASS-Agent/1.0 (+document assistant)",
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
    })
    try:
        with opener.open(req, timeout=timeout) as res:          # noqa: S310 - checked above
            ctype = (res.headers.get("Content-Type") or "").lower()
            raw = res.read(MAX_FETCH_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise DocError(f"{url} answered HTTP {exc.code}.")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
        raise DocError(f"{url} couldn't be reached ({getattr(exc, 'reason', exc)}).")
    if len(raw) > MAX_FETCH_BYTES:
        raw = raw[:MAX_FETCH_BYTES]
    title, text = "", ""
    path = urllib.parse.urlparse(url).path
    if "pdf" in ctype or raw[:5] == b"%PDF-":
        text = pdf_text(raw)
    elif "wordprocessingml" in ctype or path.lower().endswith(".docx"):
        text = docx_text(raw)
    elif "html" in ctype or "xml" in ctype or not ctype:
        charset = re.search(r"charset=([\w-]+)", ctype)
        title, text = html_to_text(raw.decode(charset.group(1) if charset else "utf-8", "replace"))
    elif ctype.startswith("text/"):
        text = raw.decode("utf-8", "replace")
    else:
        raise DocError(f"{url} is a {ctype.split(';')[0] or 'file'} the agent can't read.")
    if not text.strip():
        raise DocError(f"{url} has no readable text (it may need a sign-in, or be built with scripts).")
    return {"url": url, "title": (title or path.rsplit("/", 1)[-1] or url)[:200], "text": text}


# ─────────────────────────────────────────────────────────────────────────────
# Writing files
# ─────────────────────────────────────────────────────────────────────────────

def _fonts():
    from .form_pdf_esign import _font_names

    return _font_names("helvetica")


def _blocks(text):
    """Light Markdown: '# heading', '• / - / * bullets', blank-line paragraphs."""
    for line in (text or "").splitlines():
        s = line.rstrip()
        if not s.strip():
            yield ("gap", "")
            continue
        m = re.match(r"^\s*(#{1,3})\s+(.*)$", s)
        if m:
            yield ("h", m.group(2).strip())
            continue
        m = re.match(r"^\s*(?:[-•*]|\d+[.)])\s+(.*)$", s)
        if m:
            yield ("li", m.group(1).strip())
            continue
        if re.match(r"^[A-Z][A-Za-z /&-]{2,40}:$", s.strip()):
            yield ("h", s.strip().rstrip(":"))
            continue
        yield ("p", s.strip())


def text_to_pdf(text: str, title: str = "", subtitle: str = "") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    regular, bold = _fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=title or "Document", author="UN PASS agent")
    body = ParagraphStyle("b", fontName=regular, fontSize=10.5, leading=15, spaceAfter=4)
    head = ParagraphStyle("h", fontName=bold, fontSize=12, leading=16, spaceBefore=8, spaceAfter=4,
                          textColor=colors.HexColor("#005A8B"))
    ttl = ParagraphStyle("t", fontName=bold, fontSize=17, leading=22, textColor=colors.HexColor("#0F172A"), spaceAfter=2)
    sub = ParagraphStyle("s", fontName=regular, fontSize=9, leading=12, textColor=colors.HexColor("#64748B"), spaceAfter=10)
    li = ParagraphStyle("li", parent=body, leftIndent=12, bulletIndent=2)
    flow = []
    if title:
        flow.append(Paragraph(html.escape(title), ttl))
    if subtitle:
        flow.append(Paragraph(html.escape(subtitle), sub))
    for kind, value in _blocks(text):
        v = html.escape(value)
        if kind == "h":
            flow.append(Paragraph(v, head))
        elif kind == "li":
            flow.append(Paragraph(v, li, bulletText="•"))
        elif kind == "gap":
            flow.append(Spacer(1, 4))
        else:
            flow.append(Paragraph(v, body))
    if not flow:
        flow.append(Paragraph("(empty)", body))
    doc.build(flow)
    return buf.getvalue()


def text_to_docx(text: str, title: str = "") -> bytes:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    if title:
        doc.add_heading(title, level=1)
    for kind, value in _blocks(text):
        if kind == "h":
            doc.add_heading(value, level=2)
        elif kind == "li":
            doc.add_paragraph(value, style="List Bullet")
        elif kind == "p":
            if value.startswith("[Page ") and value.endswith("]"):
                if len(doc.paragraphs) > 1:
                    doc.add_page_break()
                continue
            doc.add_paragraph(value)
    doc.core_properties.author = "UN PASS agent"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def text_to_html(text: str, title: str = "") -> bytes:
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title or 'Document')}</title>",
             "<style>body{font:15px/1.6 Segoe UI,Helvetica,Arial,sans-serif;max-width:780px;margin:40px auto;color:#0F172A;padding:0 16px}"
             "h1{color:#005A8B}h2{color:#005A8B;font-size:1.1rem;margin-top:1.4em}</style></head><body>"]
    if title:
        parts.append(f"<h1>{html.escape(title)}</h1>")
    in_list = False
    for kind, value in _blocks(text):
        if kind != "li" and in_list:
            parts.append("</ul>")
            in_list = False
        v = html.escape(value)
        if kind == "h":
            parts.append(f"<h2>{v}</h2>")
        elif kind == "li":
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{v}</li>")
        elif kind == "p":
            parts.append(f"<p>{v}</p>")
    if in_list:
        parts.append("</ul>")
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")


def rows_from(text: str, data: dict):
    """Tabular data for Excel/CSV: an Extract card's fields, or the text's lines."""
    fields = (data or {}).get("fields")
    if isinstance(fields, dict) and fields:
        return [["Field", "Value"]] + [[k, "" if v is None else str(v)] for k, v in fields.items()]
    rows = (data or {}).get("rows")
    if isinstance(rows, list) and rows and isinstance(rows[0], list):
        return rows
    out = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if "\t" in line:
            out.append(line.split("\t"))
        elif " | " in line:
            out.append([c.strip() for c in line.split(" | ")])
        elif re.match(r"^[^:]{1,60}:\s", line.strip()):
            k, v = line.strip().split(":", 1)
            out.append([k.strip("•-* "), v.strip()])
        else:
            out.append([line.strip("•-* ")])
    return out or [["(no data)"]]


def rows_to_xlsx(rows, title="Data") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r"[\[\]:*?/\\]", "", title)[:31] or "Data"
    for row in rows:
        ws.append([str(c)[:32000] for c in row])
    if rows:
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="005A8B")
    widths = {}
    for row in rows:
        for i, c in enumerate(row):
            widths[i] = min(80, max(widths.get(i, 8), len(str(c)) + 2))
    for i, w in widths.items():
        ws.column_dimensions[ws.cell(row=1, column=i + 1).column_letter].width = w
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def rows_to_csv(rows) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")       # BOM so Excel opens accents right


def to_pdf(raw: bytes, name: str) -> bytes:
    """The document itself as a PDF (Office via LibreOffice, images, text)."""
    ext = _ext(name)
    if raw[:5] == b"%PDF-":
        return raw
    if ext in ("png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"):
        from .pdf_tools_esign import images_to_pdf

        return images_to_pdf([(name, raw)])
    if ext in ("txt", "md", "csv", "json", "log"):
        return text_to_pdf(raw.decode("utf-8", "replace"), title=stem(name))
    if ext in ("html", "htm"):
        title, text = html_to_text(raw.decode("utf-8", "replace"))
        return text_to_pdf(text, title=title or stem(name))
    from .converters_esign import ConversionError, convert_to_pdf

    try:
        return convert_to_pdf(raw, name)
    except ConversionError as exc:
        raise DocError(f"{name} could not be turned into a PDF: {exc}")


def _soffice_to(raw: bytes, name: str, target: str):
    """Office -> another Office format with LibreOffice, keeping the layout. None if unavailable."""
    import os
    import subprocess
    import tempfile

    from .converters_esign import _soffice_binary

    binary = _soffice_binary()
    if not binary:
        return None
    with tempfile.TemporaryDirectory(prefix="agent-convert-") as tmp:
        src = os.path.join(tmp, "source." + (_ext(name) or "bin"))
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        with open(src, "wb") as fh:
            fh.write(raw)
        cmd = [binary, "--headless", "--norestore", "--nolockcheck", f"-env:UserInstallation=file://{tmp}/p",
               "--convert-to", target, "--outdir", out, src]
        try:
            subprocess.run(cmd, capture_output=True, timeout=int(getattr(settings, "ESIGN_CONVERT_TIMEOUT", 120)), check=False)
        except subprocess.TimeoutExpired:
            return None
        produced = os.path.join(out, "source." + target)
        if os.path.exists(produced):
            with open(produced, "rb") as fh:
                return fh.read()
    return None


def convert(target: str, *, raw: bytes = None, name: str = "", text: str = "", data: dict = None, title: str = ""):
    """
    -> (bytes, filename, note). Works from the original file (`raw`) when there
    is one and the target can keep its layout, otherwise from `text`.
    """
    target = (target or "pdf").lower()
    if target not in TARGETS:
        raise DocError(f"Unknown target format “{target}”.")
    base = stem(title or name or "document")
    note = ""

    if raw is not None and not text:
        ext = _ext(name)
        if target == "pdf":
            return to_pdf(raw, name), f"{base}.pdf", ""
        if target == "docx" and ext in ("doc", "odt", "rtf", "txt", "html", "htm"):
            out = _soffice_to(raw, name, "docx")
            if out:
                return out, f"{base}.docx", ""
        if target == "xlsx" and ext in ("xls", "ods", "csv"):
            out = _soffice_to(raw, name, "xlsx")
            if out:
                return out, f"{base}.xlsx", ""
        if ext == target:
            return raw, f"{base}.{target}", "Already in that format."
        text = read_text(raw, name)
        if raw[:5] == b"%PDF-" and target in ("docx", "md", "txt", "html"):
            note = "Text and headings were carried over; the original page layout was not."
        if not text.strip():
            raise DocError("The document has no readable text (it may be a scanned image), so it can't be converted.")

    if target == "pdf":
        return text_to_pdf(text, title=title, subtitle=f"Prepared by a UN PASS agent · {timezone.localdate():%d %b %Y}"), f"{base}.pdf", note
    if target == "docx":
        return text_to_docx(text, title=title), f"{base}.docx", note
    if target == "html":
        return text_to_html(text, title=title), f"{base}.html", note
    if target == "md":
        return ((f"# {title}\n\n" if title else "") + text).encode("utf-8"), f"{base}.md", note
    if target == "txt":
        return ((f"{title}\n\n" if title else "") + text).encode("utf-8"), f"{base}.txt", note
    rows = rows_from(text, data or {})
    if target == "xlsx":
        return rows_to_xlsx(rows, title=title or "Data"), f"{base}.xlsx", note
    return rows_to_csv(rows), f"{base}.csv", note


def cover_page(summary: str, *, title: str, meta: list, original_pdf: bytes = None) -> bytes:
    """A summary sheet (with an 'AI-generated' band) in front of the document."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    regular, bold = _fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Summary — {title}", author="UN PASS agent")
    band = Table([[Paragraph("<font color='#FFFFFF'><b>DOCUMENT SUMMARY</b> · prepared by a UN PASS agent — "
                             "check it against the document before relying on it</font>",
                             ParagraphStyle("band", fontName=bold, fontSize=8.5, leading=11))]], colWidths=[174 * mm])
    band.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#005A8B")),
                              ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    ttl = ParagraphStyle("t", fontName=bold, fontSize=16, leading=20, spaceBefore=10, spaceAfter=6)
    body = ParagraphStyle("b", fontName=regular, fontSize=10.5, leading=15, spaceAfter=3)
    head = ParagraphStyle("h", fontName=bold, fontSize=11, leading=15, spaceBefore=6, spaceAfter=3,
                          textColor=colors.HexColor("#005A8B"))
    li = ParagraphStyle("li", parent=body, leftIndent=12)
    small = ParagraphStyle("m", fontName=regular, fontSize=8.5, leading=11, textColor=colors.HexColor("#475569"))
    flow = [band, Paragraph(html.escape(title), ttl)]
    if meta:
        t = Table([[Paragraph(html.escape(k), small), Paragraph(html.escape(str(v)), small)] for k, v in meta],
                  colWidths=[38 * mm, 136 * mm])
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#E2E8F0")),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        flow += [t, Spacer(1, 8)]
    for kind, value in _blocks(summary):
        v = html.escape(value)
        if kind == "h":
            flow.append(Paragraph(v, head))
        elif kind == "li":
            flow.append(Paragraph(v, li, bulletText="•"))
        elif kind == "p":
            flow.append(Paragraph(v, body))
    doc.build(flow)
    sheet = buf.getvalue()
    if original_pdf:
        from .pdf_tools_esign import append_pdf

        return append_pdf(sheet, original_pdf)
    return sheet
