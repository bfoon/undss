# accounts/form_pdf_esign.py
"""
UN PASS — eSign Studio forms: schema, validation and PDF rendering.

A form is a flat list of elements laid out on a 12-column grid. Each element
has a width (3, 4, 6, 8, 9 or 12); elements flow left to right and wrap to a
new row, the same way on screen and on paper. That is what makes the designer
honest: what you arrange is what prints.

Signature elements are the bridge to eSign. When a form is rendered, the exact
position of every signature box is returned alongside the PDF, so a signature
step can place real eSign fields precisely where the form designer drew them.

Unicode
-------
ReportLab's built-in fonts cover Latin-1 (English, French, Spanish,
Portuguese). For Arabic, Russian or Chinese text set:

    ESIGN_PDF_FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ESIGN_PDF_FONT_BOLD    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
"""

import base64
import io
import logging
import re
from datetime import date, datetime

from django.conf import settings
from django.utils import timezone

from .form_logic_esign import clean_v2_element_metadata
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas as rl_canvas

logger = logging.getLogger(__name__)

INPUT_TYPES = ("text", "textarea", "number", "email", "date", "select", "radio",
               "checkboxes", "yesno", "table")
LAYOUT_TYPES = ("heading", "paragraph", "divider", "spacer", "image", "signature")
ALL_TYPES = INPUT_TYPES + LAYOUT_TYPES
WIDTHS = (3, 4, 6, 8, 9, 12)
PREFILL_KEYS = {
    "": "Nothing",
    "user.full_name": "My full name",
    "user.email": "My email",
    "user.job_title": "My job title",
    "user.agency": "My agency",
    "user.office": "My country office",
    "today": "Today's date",
}
FONT_FAMILIES = ("helvetica", "times", "courier")
MAX_ELEMENTS = 300
MAX_IMAGE_CHARS = 550_000          # ~400 KB of image once base64-decoded

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_KEY_RE = re.compile(r"[^a-z0-9_]+")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_IMG_RE = re.compile(r"^data:image/(png|jpe?g|webp|gif);base64,[A-Za-z0-9+/=\s]+$")


class FormError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Schema cleaning — never trust what the designer posts
# ─────────────────────────────────────────────────────────────────────────────

def _hex(v, default):
    v = (v or "").strip()
    return v if _HEX_RE.match(v) else default


def _s(v, limit):
    return str(v if v is not None else "").strip()[:limit]


def _int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def slug_key(text, fallback="field"):
    key = _KEY_RE.sub("_", (text or "").lower()).strip("_")[:40]
    return key or fallback


def default_theme():
    return {
        "accent": "#009EDB",
        "header_bg": "#005A8B",
        "header_text": "#FFFFFF",
        "label_color": "#334155",
        "font": "helvetica",
        "density": "comfortable",
        "rounded": True,
    }


def clean_schema(raw):
    raw = raw if isinstance(raw, dict) else {}
    theme_in = raw.get("theme") or {}
    base = default_theme()
    theme = {
        "accent": _hex(theme_in.get("accent"), base["accent"]),
        "header_bg": _hex(theme_in.get("header_bg"), base["header_bg"]),
        "header_text": _hex(theme_in.get("header_text"), base["header_text"]),
        "label_color": _hex(theme_in.get("label_color"), base["label_color"]),
        "font": theme_in.get("font") if theme_in.get("font") in FONT_FAMILIES else "helvetica",
        "density": "compact" if theme_in.get("density") == "compact" else "comfortable",
        "rounded": bool(theme_in.get("rounded", True)),
    }

    header_in = raw.get("header") or {}
    logo = str(header_in.get("logo") or "")
    header = {
        "title": _s(header_in.get("title"), 200) or "Untitled form",
        "subtitle": _s(header_in.get("subtitle"), 300),
        "logo": logo if (len(logo) <= MAX_IMAGE_CHARS and _IMG_RE.match(logo)) else "",
        "align": "center" if header_in.get("align") == "center" else "left",
        "show_reference": bool(header_in.get("show_reference", True)),
    }

    elements, used_ids, used_keys = [], set(), set()
    for el in (raw.get("elements") or [])[:MAX_ELEMENTS]:
        if not isinstance(el, dict) or el.get("type") not in ALL_TYPES:
            continue
        clean = _clean_element(el)
        if clean["id"] in used_ids:
            clean["id"] = f"{clean['id']}_{len(used_ids)}"
        used_ids.add(clean["id"])
        if clean["type"] in INPUT_TYPES:
            key, n = clean["key"], 2
            while clean["key"] in used_keys:
                clean["key"] = f"{key}_{n}"
                n += 1
            used_keys.add(clean["key"])
        elements.append(clean)

    return {"version": 1, "theme": theme, "header": header, "elements": elements}


def _clean_element(el):
    t = el["type"]
    eid = el.get("id") if _ID_RE.match(str(el.get("id") or "")) else f"el_{abs(hash(str(el))) % 10**8}"
    width = _int(el.get("width"), 3, 12, 12)
    if width not in WIDTHS:
        width = min(WIDTHS, key=lambda w: abs(w - width))
    out = {"id": eid, "type": t, "width": width}
    # Whether it appears on the printed PDF. A heading answers for its whole
    # section unless a question overrides it. Missing means yes, so forms made
    # before this setting print exactly as they always did.
    if el.get("print_pdf") is not None:
        out["print_pdf"] = bool(el.get("print_pdf"))
    out.update(clean_v2_element_metadata(el))

    if t in INPUT_TYPES:
        label = _s(el.get("label"), 200) or "Untitled field"
        out.update({
            "label": label,
            "key": slug_key(el.get("key") or label),
            "required": bool(el.get("required")),
            "help": _s(el.get("help"), 300),
            "placeholder": _s(el.get("placeholder"), 120),
            "fill_by": _s(el.get("fill_by"), 40) or "inherit",
            "prefill": el.get("prefill") if el.get("prefill") in PREFILL_KEYS else "",
        })
        if t in ("select", "radio", "checkboxes"):
            opts = [_s(o, 120) for o in (el.get("options") or []) if _s(o, 120)]
            out["options"] = opts[:50] or ["Option 1", "Option 2"]
        if t == "number":
            for k in ("min", "max"):
                try:
                    out[k] = float(el[k]) if el.get(k) not in (None, "") else None
                except (TypeError, ValueError):
                    out[k] = None
            out["decimals"] = _int(el.get("decimals"), 0, 4, 0)
        if t == "textarea":
            out["rows"] = _int(el.get("rows"), 2, 12, 4)
        if t == "table":
            cols = []
            for i, c in enumerate((el.get("columns") or [])[:10]):
                if not isinstance(c, dict):
                    continue
                clabel = _s(c.get("label"), 80) or f"Column {i + 1}"
                cols.append({
                    "key": slug_key(c.get("key") or clabel, f"c{i + 1}"),
                    "label": clabel,
                    "kind": c.get("kind") if c.get("kind") in ("text", "number", "date") else "text",
                    "width": _int(c.get("width"), 1, 6, 2),
                })
            seen = set()
            for c in cols:
                base_key, n = c["key"], 2
                while c["key"] in seen:
                    c["key"] = f"{base_key}_{n}"
                    n += 1
                seen.add(c["key"])
            out["columns"] = cols or [{"key": "item", "label": "Item", "kind": "text", "width": 4},
                                      {"key": "qty", "label": "Qty", "kind": "number", "width": 1}]
            out["rows"] = _int(el.get("rows"), 1, 50, 3)
            out["show_total"] = bool(el.get("show_total"))
            out["allow_add"] = bool(el.get("allow_add", True))
        return out

    if t == "heading":
        style = el.get("style") or {}
        out.update({
            "text": _s(el.get("text"), 200) or "Section",
            # A heading owns the section beneath it: whoever fills it in, and the
            # rules that apply, cover every field down to the next heading.
            "fill_by": _s(el.get("fill_by"), 40) or "submitter",
            "style": {
                "size": style.get("size") if style.get("size") in ("sm", "md", "lg") else "md",
                "color": _hex(style.get("color"), "#005A8B"),
                "bg": _hex(style.get("bg"), "") if style.get("bg") else "",
                "align": style.get("align") if style.get("align") in ("left", "center", "right") else "left",
            },
        })
    elif t == "paragraph":
        style = el.get("style") or {}
        out.update({
            "text": _s(el.get("text"), 4000),
            "style": {
                "color": _hex(style.get("color"), "#334155"),
                "size": style.get("size") if style.get("size") in ("sm", "md") else "md",
                "bg": _hex(style.get("bg"), "") if style.get("bg") else "",
                "align": style.get("align") if style.get("align") in ("left", "center", "right") else "left",
            },
        })
    elif t == "divider":
        out["style"] = {"color": _hex((el.get("style") or {}).get("color"), "#CBD5E1")}
    elif t == "spacer":
        out["height"] = _int(el.get("height"), 4, 160, 16)
    elif t == "image":
        src = str(el.get("src") or "")
        out.update({
            "src": src if (len(src) <= MAX_IMAGE_CHARS and _IMG_RE.match(src)) else "",
            "height": _int(el.get("height"), 16, 320, 64),
            "align": el.get("align") if el.get("align") in ("left", "center", "right") else "left",
        })
    elif t == "signature":
        out.update({
            "label": _s(el.get("label"), 120) or "Signature",
            "role": _s(el.get("role"), 80),
            "step": _s(el.get("step"), 40),
            "show_date": bool(el.get("show_date", True)),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Values
# ─────────────────────────────────────────────────────────────────────────────

def prefill_value(key, user):
    if not key:
        return ""
    if key == "today":
        return timezone.localdate().isoformat()
    if not user or not getattr(user, "is_authenticated", False):
        return ""
    if key == "user.full_name":
        return user.get_full_name() or user.username
    if key == "user.email":
        return user.email or ""
    if key == "user.job_title":
        return getattr(user, "job_title", "") or ""
    if key == "user.agency":
        agency = getattr(user, "agency", None) or getattr(getattr(user, "country_office", None), "agency", None)
        return str(agency or "")
    if key == "user.office":
        return str(getattr(user, "country_office", "") or "")
    return ""


def initial_values(schema, user):
    values = {}
    for el in schema.get("elements") or []:
        if el["type"] in INPUT_TYPES and el.get("prefill"):
            values[el["key"]] = prefill_value(el["prefill"], user)
    return values


def effective_fill(schema):
    """
    {element id: who fills it in}. A field set to "inherit" takes its section's
    assignment — the heading above it — so a whole section can be handed to a
    workflow step in one move. Anything before the first heading, or in a
    section that doesn't say, belongs to the person who starts the form.
    """
    out, section = {}, "submitter"
    for el in (schema or {}).get("elements") or []:
        if el.get("type") == "heading":
            section = str(el.get("fill_by") or "submitter")
            out[el["id"]] = section
            continue
        own = str(el.get("fill_by") or "inherit")
        out[el["id"]] = section if own == "inherit" else own
    return out


def resolved_schema(schema):
    """A copy of the schema with every `fill_by` replaced by its effective value."""
    import copy

    resolved = copy.deepcopy(schema or {})
    scopes = effective_fill(resolved)
    for el in resolved.get("elements") or []:
        el["fill_by"] = scopes.get(el["id"], "submitter")
    return resolved


def fields_for_step(schema, node_id):
    """The input fields a workflow step collects, section assignments included."""
    scopes = effective_fill(schema)
    return [el for el in (schema or {}).get("elements") or []
            if el["type"] in INPUT_TYPES and scopes.get(el["id"]) == node_id]


def sections_of(schema):
    """
    [{heading, fill_by, fields, overrides}] — one entry per section, plus an
    opening entry with no heading for anything above the first one.
    """
    out = [{"heading": None, "fill_by": "submitter", "fields": [], "overrides": 0}]
    for el in (schema or {}).get("elements") or []:
        if el["type"] == "heading":
            out.append({"heading": el, "fill_by": str(el.get("fill_by") or "submitter"),
                        "fields": [], "overrides": 0})
            continue
        if el["type"] in INPUT_TYPES:
            out[-1]["fields"].append(el)
            if str(el.get("fill_by") or "inherit") != "inherit":
                out[-1]["overrides"] += 1
    return [s for s in out if s["heading"] is not None or s["fields"]]


def editable_for(el, scope):
    """scope is "submitter" or a workflow node id."""
    return (el.get("fill_by") or "submitter") == scope


def read_values(schema, post, existing=None, scope="submitter", extras=None):
    """
    Pull values out of a POST for the elements this scope may edit, validate
    them, and merge over `existing`. Returns (values, errors) where errors maps
    a field key to a readable message.

    Conditional states are applied afterwards: an answer the rules grey out or
    hide for this person is cleared and never validated, so a section that
    doesn't apply can't block the form with "this is required".
    """
    values = dict(existing or {})
    errors = {}
    schema = resolved_schema(schema)

    for el in schema.get("elements") or []:
        if el["type"] not in INPUT_TYPES or not editable_for(el, scope):
            continue
        key, name, t = el["key"], f"f_{el['key']}", el["type"]
        label = el.get("label") or key

        if t == "checkboxes":
            chosen = [v for v in post.getlist(name) if v in el.get("options", [])]
            values[key] = chosen
            if el.get("required") and not chosen:
                errors[key] = f"Choose at least one option for {label}."
            continue

        if t == "table":
            try:
                n = max(0, min(200, int(post.get(f"{name}__rows") or 0)))
            except (TypeError, ValueError):
                n = 0
            rows = []
            for r in range(n):
                row = {}
                for col in el["columns"]:
                    cell = _s(post.get(f"{name}__{r}__{col['key']}"), 500)
                    if cell and col["kind"] == "number":
                        try:
                            float(cell.replace(",", ""))
                        except ValueError:
                            errors[key] = f"{col['label']} in {label} must be a number (row {r + 1})."
                    row[col["key"]] = cell
                if any(row.values()):
                    rows.append(row)
            values[key] = rows
            if el.get("required") and not rows:
                errors[key] = f"Add at least one row to {label}."
            continue

        raw = post.get(name)
        val = _s(raw, 5000 if t == "textarea" else 500)

        if t in ("select", "radio") and val and val not in el.get("options", []):
            val = ""
        if t == "yesno" and val not in ("yes", "no"):
            val = ""
        if t == "email" and val:
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", val):
                errors[key] = f"{label} must be an email address."
        if t == "number" and val:
            try:
                num = float(val.replace(",", ""))
                if el.get("min") is not None and num < el["min"]:
                    errors[key] = f"{label} must be at least {_num(el['min'])}."
                if el.get("max") is not None and num > el["max"]:
                    errors[key] = f"{label} must be at most {_num(el['max'])}."
            except ValueError:
                errors[key] = f"{label} must be a number."
        if t == "date" and val:
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                errors[key] = f"{label} must be a date."

        values[key] = val
        if el.get("required") and not val and key not in errors:
            errors[key] = f"{label} is required."

    return _apply_states(schema, values, errors, scope, extras)


def _apply_states(schema, values, errors, scope, extras=None):
    """Clear and stop validating anything the rules switch off for this person."""
    from .form_logic_esign import annotate_states

    extras = extras or {}
    annotated = annotate_states(schema, values, scope=scope, extras=extras)
    for el in annotated.get("elements") or []:
        state = (el.get("runtime_state") or {}).get("state")
        key = el.get("key")
        if not key or state not in ("disabled", "hidden") or not editable_for(el, scope):
            continue
        values[key] = [] if el.get("type") in ("checkboxes", "table") else ""
        errors.pop(key, None)
    return values, errors


def _num(v):
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:,.2f}"
    except (TypeError, ValueError):
        return str(v)


def display_value(el, value):
    """A value as a person would read it — used on screen, in CSV and in the PDF."""
    t = el["type"]
    if value in (None, "", []):
        return ""
    if t == "yesno":
        return {"yes": "Yes", "no": "No"}.get(value, "")
    if t == "checkboxes":
        return ", ".join(value) if isinstance(value, list) else str(value)
    if t == "date":
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d %b %Y")
        except (TypeError, ValueError):
            return str(value)
    if t == "number":
        try:
            f = float(str(value).replace(",", ""))
            decimals = el.get("decimals") or 0
            return f"{f:,.{decimals}f}"
        except ValueError:
            return str(value)
    if t == "table":
        return f"{len(value)} row(s)" if isinstance(value, list) else ""
    return str(value)


def table_total(el, rows, col_key):
    total = 0.0
    for r in rows or []:
        try:
            total += float(str(r.get(col_key) or 0).replace(",", ""))
        except ValueError:
            continue
    return total


def summary_rows(schema, values, *, for_pdf=False):
    """
    [(label, display)] for every input — detail pages, emails, and the workflow
    record page.

    With for_pdf, the same rules the printed form follows apply here too: a
    question switched off with "Print on the PDF", or hidden by a rule, is left
    out, and one kept back reads "Already filled" rather than showing the
    answer. Otherwise nothing is filtered — the answers page on screen shows
    everything the viewer is entitled to see.
    """
    from .form_logic_esign import print_plan

    plan = print_plan(schema, values) if for_pdf else {}
    out = []
    for el in schema.get("elements") or []:
        if el["type"] not in INPUT_TYPES:
            continue
        how = plan.get(el["id"], "print")
        if how == "skip":
            continue
        label = el.get("label") or el["key"]
        if how == "not_applicable":
            out.append((label, NOT_APPLICABLE))
        elif how == "already_filled":
            out.append((label, ALREADY_FILLED))
        else:
            out.append((label, display_value(el, values.get(el["key"]))))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────────────────────────────────────

PAGE_W, PAGE_H = A4
MARGIN_X = 42.0
MARGIN_TOP = 36.0
MARGIN_BOTTOM = 46.0
GUTTER = 12.0
_fonts_ready = {}


def _font_names(family):
    reg_path = getattr(settings, "ESIGN_PDF_FONT_REGULAR", "")
    bold_path = getattr(settings, "ESIGN_PDF_FONT_BOLD", "")
    if family == "helvetica" and reg_path:
        if "unicode" not in _fonts_ready:
            try:
                from reportlab.pdfbase import pdfmetrics
                from reportlab.pdfbase.ttfonts import TTFont

                pdfmetrics.registerFont(TTFont("EsignSans", reg_path))
                pdfmetrics.registerFont(TTFont("EsignSans-Bold", bold_path or reg_path))
                _fonts_ready["unicode"] = True
            except Exception:  # noqa: BLE001
                logger.warning("eSign Studio: could not register ESIGN_PDF_FONT_* — using Helvetica")
                _fonts_ready["unicode"] = False
        if _fonts_ready.get("unicode"):
            return "EsignSans", "EsignSans-Bold"
    return {
        "times": ("Times-Roman", "Times-Bold"),
        "courier": ("Courier", "Courier-Bold"),
    }.get(family, ("Helvetica", "Helvetica-Bold"))


def _color(v, default="#000000"):
    try:
        return colors.HexColor(v or default)
    except Exception:  # noqa: BLE001
        return colors.HexColor(default)


def _tint(hex_color, amount):
    """Mix a colour with white. amount 0 = colour, 1 = white."""
    c = _color(hex_color)
    return colors.Color(c.red + (1 - c.red) * amount, c.green + (1 - c.green) * amount,
                        c.blue + (1 - c.blue) * amount)


def _image_reader(data_url):
    try:
        b64 = data_url.split(",", 1)[1]
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        img.load()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return ImageReader(buf), img.size
    except Exception:  # noqa: BLE001
        return None, (0, 0)


NOT_APPLICABLE = "Not applicable"
ALREADY_FILLED = "Already filled"


class _Painter:
    """Two passes: lay out once to count pages, then draw with page totals known."""

    def __init__(self, schema, values, *, reference="", submitted_at=None, submitter="",
                 blank=False, status_label=""):
        self.schema = schema
        self.values = values or {}
        self.theme = schema.get("theme") or default_theme()
        self.header = schema.get("header") or {}
        self.reference = reference
        self.submitted_at = submitted_at
        self.submitter = submitter
        self.blank = blank
        # Rules decide what reaches paper: hidden questions are left out,
        # greyed-out ones print "Not applicable", and ones the owner chose to
        # keep back print "Already filled" instead of the answer.
        from .form_logic_esign import print_plan

        self.plan = {} if blank else print_plan(schema, self.values)
        self.status_label = status_label
        self.regular, self.bold = _font_names(self.theme.get("font", "helvetica"))
        self.compact = self.theme.get("density") == "compact"
        self.row_gap = 8.0 if self.compact else 13.0
        self.accent = self.theme.get("accent") or "#009EDB"
        self.label_color = _color(self.theme.get("label_color"), "#334155")
        self.content_w = PAGE_W - 2 * MARGIN_X
        self.col_w = (self.content_w - GUTTER * 11) / 12.0

    # -- geometry ---------------------------------------------------------
    def width_for(self, span):
        return self.col_w * span + GUTTER * (span - 1)

    # -- measuring --------------------------------------------------------
    def _wrap(self, text, font, size, width):
        lines = []
        for para in str(text or "").splitlines() or [""]:
            lines.extend(simpleSplit(para, font, size, max(width, 10)) or [""])
        return lines

    def measure(self, el, w):
        t = el["type"]
        label_h = 13.0
        if t == "heading":
            size = {"lg": 15.0, "md": 12.5, "sm": 10.5}[el["style"]["size"]]
            lines = self._wrap(el["text"], self.bold, size, w - (16 if el["style"]["bg"] else 0))
            pad = 8.0 if el["style"]["bg"] else 2.0
            return len(lines) * size * 1.25 + pad * 2
        if t == "paragraph":
            size = 9.5 if el["style"]["size"] == "md" else 8.2
            pad = 8.0 if el["style"].get("bg") else 0.0
            lines = el.get("_lines") or self._wrap(el["text"], self.regular, size, w - pad * 2)
            return len(lines) * size * 1.35 + pad * 2
        if t == "divider":
            return 10.0
        if t == "spacer":
            return float(el["height"])
        if t == "image":
            return float(el["height"]) if el.get("src") else 0.0
        if t == "signature":
            custom = float((el.get("presentation") or {}).get("height") or 50.0)
            return label_h + max(20.0, min(180.0, custom)) + (18.0 if el.get("show_date") else 4.0)
        if t == "textarea":
            text = display_value(el, self.values.get(el["key"]))
            lines = el.get("_lines") or (self._wrap(text, self.regular, 9.5, w - 12) if text and not self.blank else [])
            body = max(len(lines) * 12.5 + 10, (el.get("rows", 4) * 13.0 if (self.blank or not text) else 22))
            return label_h + body + self._help_h(el, w)
        if t in ("radio", "checkboxes"):
            return label_h + self._option_rows(el, w) * 14.0 + 6 + self._help_h(el, w)
        if t == "table":
            rows = self._table_rows(el)
            return label_h + 18.0 * (1 + len(rows) + (1 if el.get("show_total") else 0)) + 4 + self._help_h(el, w)
        text = el.get("_fixed") or display_value(el, self.values.get(el["key"]))
        lines = self._wrap(text, self.regular, 9.5, w - 12) if text and not self.blank else [""]
        return label_h + max(22.0, len(lines) * 12.5 + 9) + self._help_h(el, w)

    def _help_h(self, el, w):
        if not el.get("help") or not self.blank:
            return 0.0
        return len(self._wrap(el["help"], self.regular, 7, w)) * 9.0

    def _option_rows(self, el, w):
        per_row = max(1, int(w // 150))
        return max(1, -(-len(el.get("options", [])) // per_row))

    def _table_rows(self, el):
        if "_rows" in el:
            return el["_rows"]
        rows = self.values.get(el["key"]) if not self.blank else None
        rows = rows if isinstance(rows, list) else []
        if self.blank or not rows:
            return [{} for _ in range(el.get("rows", 3))]
        return rows

    def _split_tall(self, elements):
        """
        Break tables, long answers and long paragraphs that are taller than a
        page into consecutive pieces. Each piece becomes its own full-width row,
        so a 40-row table simply continues on the next page with its header.
        """
        page_room = PAGE_H - MARGIN_TOP - MARGIN_BOTTOM - self._header_h(first=False) - 24
        out = []
        for el in elements:
            w = self.width_for(el["width"])
            if self.measure(el, w) <= page_room:
                out.append(el)
                continue
            t = el["type"]
            if t in ("textarea", "paragraph"):
                if t == "paragraph":
                    size = 9.5 if el["style"]["size"] == "md" else 8.2
                    pad = 8.0 if el["style"].get("bg") else 0.0
                    lines = self._wrap(el["text"], self.regular, size, w - pad * 2)
                    per = max(1, int((page_room - pad * 2) // (size * 1.35)))
                else:
                    text = display_value(el, self.values.get(el["key"]))
                    lines = self._wrap(text, self.regular, 9.5, w - 12)
                    per = max(1, int((page_room - 30) // 12.5))
                for i in range(0, len(lines), per):
                    piece = dict(el, _lines=lines[i:i + per])
                    if i and t == "textarea":
                        piece["label"] = f"{el.get('label', '')} (continued)"
                    out.append(piece)
                continue
            out.append(el)
        return out

    # -- layout -----------------------------------------------------------
    def layout(self):
        """-> list of pages, each a list of (element, x, y_top, w, h)."""
        pages = [[]]
        top_first = PAGE_H - MARGIN_TOP - self._header_h(first=True)
        top_next = PAGE_H - MARGIN_TOP - self._header_h(first=False)
        floor = MARGIN_BOTTOM
        state = {"y": top_first}

        def new_page():
            pages.append([])
            state["y"] = top_next

        def place(row):
            if len(row) == 1 and row[0]["type"] == "table":
                return place_table(row[0])
            heights = [self.measure(el, self.width_for(el["width"])) for el in row]
            row_h = max(heights) if heights else 0
            if state["y"] - row_h < floor and pages[-1]:
                new_page()
            put(row, heights, row_h)

        def put(row, heights, row_h):
            x = MARGIN_X
            for el, h in zip(row, heights):
                w = self.width_for(el["width"])
                pages[-1].append((el, x, state["y"], w, h))
                x += w + GUTTER
            state["y"] -= row_h + self.row_gap

        def place_table(el):
            """Start a table in the space left; continue it, header and all, overleaf."""
            all_rows = self._table_rows(el)
            rows, first = all_rows, True
            while True:
                piece = dict(el, _rows=rows, _rows_all=all_rows)
                if not first:
                    piece["label"] = f"{el.get('label', '')} (continued)"
                w = self.width_for(el["width"])
                h = self.measure(piece, w)
                if state["y"] - h >= floor:
                    return put([piece], [h], h)
                footer_rows = 1 if el.get("show_total") else 0
                fit = int((state["y"] - floor - 13 - 18 * (1 + footer_rows) - 4) // 18)
                if fit >= 3 or (fit >= 1 and not pages[-1]):
                    head = dict(piece, _rows=rows[:fit], show_total=False)
                    put([head], [self.measure(head, w)], self.measure(head, w))
                    rows, first = rows[fit:], False
                    new_page()
                    continue
                if pages[-1]:
                    new_page()
                    continue
                return put([piece], [h], h)          # nothing better is possible

        row, span = [], 0
        normal_elements = [
            self._for_print(el) for el in (self.schema.get("elements") or [])
            if not (el.get("canvas") or {}).get("enabled") and self.plan.get(el["id"], "print") != "skip"
        ]
        for el in self._split_tall(normal_elements):
            if span + el["width"] > 12 and row:
                place(row)
                row, span = [], 0
            row.append(el)
            span += el["width"]
        if row:
            place(row)

        # Free-position fields use a fixed 794x1123 designer canvas. Convert
        # that coordinate system to the actual A4 PDF page. Mobile filling still
        # follows logical element order; only the PDF/designer use x/y.
        designer_w, designer_h = 794.0, 1123.0
        for el in self.schema.get("elements") or []:
            cv = el.get("canvas") or {}
            if not cv.get("enabled") or self.plan.get(el["id"], "print") == "skip":
                continue
            el = self._for_print(el)
            page_no = max(1, int(cv.get("page") or 1))
            while len(pages) < page_no:
                pages.append([])
            x = float(cv.get("x") or 0) / designer_w * PAGE_W
            y_top = PAGE_H - (float(cv.get("y") or 0) / designer_h * PAGE_H)
            w = max(12.0, float(cv.get("w") or 240) / designer_w * PAGE_W)
            h = max(12.0, float(cv.get("h") or 40) / designer_h * PAGE_H)
            pages[page_no - 1].append((el, x, y_top, w, h))
        return pages

    def _header_h(self, first):
        if not first:
            return 26.0
        h = 58.0
        if self.header.get("subtitle"):
            h += 14.0
        return h + 22.0

    # -- drawing ----------------------------------------------------------
    def _for_print(self, el):
        """Swap an input for a plain line of text when the rules say so."""
        how = self.plan.get(el["id"], "print")
        if how == "not_applicable":
            return dict(el, type="text", _fixed=NOT_APPLICABLE, _muted=True, required=False)
        if how == "already_filled":
            return dict(el, type="text", _fixed=ALREADY_FILLED, _muted=True, required=False)
        return el

    def render(self):
        pages = self.layout()
        total = len(pages)
        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=A4)
        c.setTitle(self.header.get("title") or "Form")
        boxes = []

        for index, placed in enumerate(pages, start=1):
            self._draw_header(c, first=index == 1)
            for el, x, y_top, w, h in placed:
                box = self._draw_element(c, el, x, y_top, w, h)
                if box:
                    box["page"] = index
                    boxes.append(box)
            self._draw_footer(c, index, total)
            c.showPage()
        c.save()
        return buf.getvalue(), boxes

    def _draw_header(self, c, first):
        title = self.header.get("title") or "Form"
        if not first:
            c.setFont(self.bold, 8.5)
            c.setFillColor(_color(self.theme["header_bg"]))
            c.drawString(MARGIN_X, PAGE_H - MARGIN_TOP + 4, title[:90])
            if self.reference:
                c.setFont(self.regular, 8)
                c.setFillColor(colors.HexColor("#64748B"))
                c.drawRightString(PAGE_W - MARGIN_X, PAGE_H - MARGIN_TOP + 4, self.reference)
            c.setStrokeColor(_tint(self.accent, 0.6))
            c.setLineWidth(0.8)
            c.line(MARGIN_X, PAGE_H - MARGIN_TOP - 4, PAGE_W - MARGIN_X, PAGE_H - MARGIN_TOP - 4)
            return

        band_h = 58.0 + (14.0 if self.header.get("subtitle") else 0.0)
        top = PAGE_H - MARGIN_TOP + 14
        c.setFillColor(_color(self.theme["header_bg"]))
        if self.theme.get("rounded", True):
            c.roundRect(MARGIN_X - 8, top - band_h, self.content_w + 16, band_h, 7, stroke=0, fill=1)
        else:
            c.rect(MARGIN_X - 8, top - band_h, self.content_w + 16, band_h, stroke=0, fill=1)
        c.setFillColor(_color(self.accent))
        c.rect(MARGIN_X - 8, top - band_h, self.content_w + 16, 3, stroke=0, fill=1)

        logo_w = 0
        if self.header.get("logo"):
            reader, (lw, lh) = _image_reader(self.header["logo"])
            if reader and lh:
                dh = band_h - 22
                dw = min(140.0, lw * dh / lh)
                dh = dw * lh / lw
                lx = PAGE_W - MARGIN_X - dw
                c.drawImage(reader, lx, top - band_h / 2 - dh / 2 + 1, dw, dh, mask="auto")
                logo_w = dw + 12

        c.setFillColor(_color(self.theme["header_text"], "#FFFFFF"))
        center = self.header.get("align") == "center"
        size = 17.0
        max_w = self.content_w - logo_w - 8
        while size > 11 and c.stringWidth(title, self.bold, size) > max_w:
            size -= 0.5
        c.setFont(self.bold, size)
        ty = top - 30
        if center:
            c.drawCentredString(PAGE_W / 2, ty, title)
        else:
            c.drawString(MARGIN_X + 6, ty, title)
        if self.header.get("subtitle"):
            c.setFont(self.regular, 9.5)
            sub = self.header["subtitle"][:140]
            if center:
                c.drawCentredString(PAGE_W / 2, ty - 16, sub)
            else:
                c.drawString(MARGIN_X + 6, ty - 16, sub)

        meta_y = top - band_h - 13
        c.setFont(self.regular, 7.8)
        c.setFillColor(colors.HexColor("#64748B"))
        left = []
        if self.reference and self.header.get("show_reference", True):
            left.append(f"Reference {self.reference}")
        if self.submitter:
            left.append(f"Submitted by {self.submitter}")
        if left:
            c.drawString(MARGIN_X, meta_y, "   |   ".join(left)[:150])
        right = self.status_label
        if self.submitted_at:
            stamp = timezone.localtime(self.submitted_at).strftime("%d %b %Y %H:%M")
            right = f"{right}   |   {stamp}" if right else stamp
        if right:
            c.drawRightString(PAGE_W - MARGIN_X, meta_y, right)

    def _draw_footer(self, c, index, total):
        c.setFont(self.regular, 7)
        c.setFillColor(colors.HexColor("#94A3B8"))
        title = (self.header.get("title") or "")[:80]
        c.drawString(MARGIN_X, 24, f"{title}{('  ·  ' + self.reference) if self.reference else ''}")
        c.drawRightString(PAGE_W - MARGIN_X, 24, f"Page {index} of {total}")

    def _label(self, c, el, x, y_top, w):
        label = el.get("label") or ""
        p = el.get("presentation") or {}
        size = float(p.get("label_font_size") or 8)
        c.setFont(self.bold, size)
        c.setFillColor(_color(p.get("label_color"), self.theme.get("label_color") or "#334155"))
        text = label + (" *" if el.get("required") and self.blank else "")
        c.drawString(x, y_top - size - 1, simpleSplit(text, self.bold, size, w)[0] if text else "")

    def _field_box(self, c, x, y, w, h, el=None):
        p = (el or {}).get("presentation") or {}
        c.setFillColor(_color(p.get("background"), "#F8FAFC"))
        c.setStrokeColor(_color(p.get("border_color"), "#CBD5E1"))
        c.setLineWidth(float(p.get("border_width") or 0.6))
        radius = float(p.get("radius") or 3)
        if self.theme.get("rounded", True) or radius:
            c.roundRect(x, y, w, h, radius, stroke=1, fill=1)
        else:
            c.rect(x, y, w, h, stroke=1, fill=1)

    def _draw_element(self, c, el, x, y_top, w, h):
        t = el["type"]
        c.saveState()
        try:
            if t == "heading":
                st = el["style"]
                size = {"lg": 15.0, "md": 12.5, "sm": 10.5}[st["size"]]
                pad = 8.0 if st["bg"] else 2.0
                if st["bg"]:
                    c.setFillColor(_color(st["bg"]))
                    c.roundRect(x, y_top - h, w, h, 4, stroke=0, fill=1)
                    c.setFillColor(_color(st["color"]))
                    c.rect(x, y_top - h, 3, h, stroke=0, fill=1)
                c.setFillColor(_color(st["color"]))
                c.setFont(self.bold, size)
                lines = self._wrap(el["text"], self.bold, size, w - (16 if st["bg"] else 0))
                for i, line in enumerate(lines):
                    ly = y_top - pad - size * (i + 1) * 1.25 + size * 0.25
                    self._aligned(c, line, x + (10 if st["bg"] else 0), ly, w - (20 if st["bg"] else 0), st["align"])
                return None

            if t == "paragraph":
                st = el["style"]
                size = 9.5 if st["size"] == "md" else 8.2
                pad = 8.0 if st.get("bg") else 0.0
                if st.get("bg"):
                    c.setFillColor(_color(st["bg"]))
                    c.roundRect(x, y_top - h, w, h, 4, stroke=0, fill=1)
                c.setFillColor(_color(st["color"]))
                c.setFont(self.regular, size)
                lines = el.get("_lines") or self._wrap(el["text"], self.regular, size, w - pad * 2)
                for i, line in enumerate(lines):
                    self._aligned(c, line, x + pad, y_top - pad - size * 1.35 * (i + 1) + size * 0.35, w - pad * 2, st["align"])
                return None

            if t == "divider":
                c.setStrokeColor(_color(el["style"]["color"], "#CBD5E1"))
                c.setLineWidth(0.8)
                c.line(x, y_top - 5, x + w, y_top - 5)
                return None

            if t == "spacer":
                return None

            if t == "image":
                if el.get("src"):
                    reader, (iw, ih) = _image_reader(el["src"])
                    if reader and ih:
                        dh = float(el["height"])
                        dw = min(w, iw * dh / ih)
                        dh = dw * ih / iw
                        ix = {"center": x + (w - dw) / 2, "right": x + w - dw}.get(el["align"], x)
                        c.drawImage(reader, ix, y_top - dh, dw, dh, mask="auto")
                return None

            if t == "signature":
                role = el.get("role") or ""
                heading = el["label"] + (f" — {role}" if role and role.lower() != el["label"].lower() else "")
                self._label(c, {"label": heading}, x, y_top, w)
                box_top = y_top - 13
                if (el.get("canvas") or {}).get("enabled"):
                    sig_h = max(20.0, h - 13.0 - (18.0 if el.get("show_date") else 4.0))
                else:
                    sig_h = max(20.0, min(180.0, float((el.get("presentation") or {}).get("height") or 50.0)))
                c.setFillColor(_tint(self.accent, 0.94))
                c.setStrokeColor(_tint(self.accent, 0.45))
                c.setDash(3, 2)
                c.setLineWidth(0.7)
                c.roundRect(x, box_top - sig_h, w, sig_h, 4, stroke=1, fill=1)
                c.setDash()
                c.setFont(self.regular, 6.5)
                c.setFillColor(_tint(self.accent, 0.35))
                c.drawString(x + 6, box_top - 10, "Sign here" if self.blank else "Signature")
                date_box = None
                if el.get("show_date"):
                    c.setFont(self.regular, 7.5)
                    c.setFillColor(colors.HexColor("#64748B"))
                    c.drawString(x, box_top - sig_h - 12, "Date")
                    c.setStrokeColor(colors.HexColor("#CBD5E1"))
                    c.line(x + 24, box_top - sig_h - 14, x + min(w, 170), box_top - sig_h - 14)
                    date_box = ((x + 24) / PAGE_W, (PAGE_H - (box_top - sig_h - 3)) / PAGE_H,
                                (min(w, 170) - 24) / PAGE_W, 11 / PAGE_H)
                return {
                    "element_id": el["id"], "step": el.get("step") or "", "label": el["label"],
                    "role": el.get("role") or "",
                    "sig": (x / PAGE_W, (PAGE_H - box_top) / PAGE_H, w / PAGE_W, sig_h / PAGE_H),
                    "date": date_box,
                }

            # ---- inputs ----
            self._label(c, el, x, y_top, w)
            body_top = y_top - 13
            value = self.values.get(el["key"])

            if t in ("radio", "checkboxes"):
                chosen = value if isinstance(value, list) else ([value] if value else [])
                per_row = max(1, int(w // 150))
                col_w = w / per_row
                for i, opt in enumerate(el.get("options", [])):
                    ox = x + (i % per_row) * col_w
                    oy = body_top - 12 - (i // per_row) * 14
                    c.setStrokeColor(colors.HexColor("#94A3B8"))
                    c.setLineWidth(0.7)
                    c.setFillColor(colors.white)
                    if t == "radio":
                        c.circle(ox + 4.5, oy + 3, 4, stroke=1, fill=1)
                    else:
                        c.rect(ox, oy - 1, 8.5, 8.5, stroke=1, fill=1)
                    if opt in chosen and not self.blank:
                        c.setFillColor(_color(self.accent))
                        if t == "radio":
                            c.circle(ox + 4.5, oy + 3, 2.2, stroke=0, fill=1)
                        else:
                            c.setStrokeColor(_color(self.accent))
                            c.setLineWidth(1.3)
                            c.line(ox + 1.8, oy + 3.3, ox + 3.8, oy + 1.1)
                            c.line(ox + 3.8, oy + 1.1, ox + 7.2, oy + 6.6)
                    c.setFillColor(colors.HexColor("#0F172A"))
                    c.setFont(self.regular, 8.8)
                    c.drawString(ox + 13, oy, simpleSplit(opt, self.regular, 8.8, col_w - 16)[0])
                return None

            if t == "table":
                self._draw_table(c, el, x, body_top, w)
                return None

            box_h = h - 13 - self._help_h(el, w)
            self._field_box(c, x, body_top - box_h, w, box_h, el)
            if not self.blank:
                text = el.get("_fixed") or display_value(el, value)
                p = el.get("presentation") or {}
                size = float(p.get("value_font_size") or 9.5)
                pad = float(p.get("padding") or 6)
                c.saveState()
                clip = c.beginPath()
                clip.rect(x, body_top - box_h, w, box_h)
                c.clipPath(clip, stroke=0, fill=0)
                c.setFillColor(_color(p.get("text_color"), "#94A3B8" if el.get("_muted") else "#0F172A"))
                c.setFont(self.regular, size)
                lines = el.get("_lines") or self._wrap(text, self.regular, size, max(10, w - pad * 2))
                line_h = size * 1.2
                max_lines = max(1, int(max(1, box_h - pad * 2) // max(line_h, 1)))
                for i, line in enumerate(lines[:max_lines]):
                    c.drawString(x + pad, body_top - pad - size - i * line_h, line)
                c.restoreState()
            elif el.get("help"):
                c.setFont(self.regular, 7)
                c.setFillColor(colors.HexColor("#94A3B8"))
                for i, line in enumerate(self._wrap(el["help"], self.regular, 7, w)):
                    c.drawString(x, body_top - box_h - 9 - i * 9, line)
            return None
        finally:
            c.restoreState()

    def _draw_table(self, c, el, x, top, w):
        cols = el["columns"]
        units = sum(col["width"] for col in cols) or 1
        widths = [w * col["width"] / units for col in cols]
        rows = self._table_rows(el)
        row_h = 18.0

        c.setFillColor(_tint(self.accent, 0.85))
        c.rect(x, top - row_h, w, row_h, stroke=0, fill=1)
        c.setFont(self.bold, 8)
        c.setFillColor(_color(self.theme.get("header_bg"), "#005A8B"))
        cx = x
        for col, cw in zip(cols, widths):
            label = simpleSplit(col["label"], self.bold, 8, cw - 8)[0] if col["label"] else ""
            if col["kind"] == "number":
                c.drawRightString(cx + cw - 5, top - 12, label)
            else:
                c.drawString(cx + 5, top - 12, label)
            cx += cw

        c.setFont(self.regular, 8.8)
        for r, row in enumerate(rows):
            ry = top - row_h * (r + 2)
            if r % 2:
                c.setFillColor(colors.HexColor("#F8FAFC"))
                c.rect(x, ry, w, row_h, stroke=0, fill=1)
            cx = x
            c.setFillColor(colors.HexColor("#0F172A"))
            for col, cw in zip(cols, widths):
                cell = str(row.get(col["key"]) or "")
                if cell and col["kind"] == "number":
                    c.drawRightString(cx + cw - 5, ry + 6, _num(cell.replace(",", "")))
                elif cell:
                    fake = {"type": "date"} if col["kind"] == "date" else {"type": "text"}
                    c.drawString(cx + 5, ry + 6, simpleSplit(display_value(fake, cell), self.regular, 8.8, cw - 9)[0])
                cx += cw

        n = len(rows) + 1
        if el.get("show_total"):
            ry = top - row_h * (n + 1)
            c.setFillColor(_tint(self.accent, 0.92))
            c.rect(x, ry, w, row_h, stroke=0, fill=1)
            c.setFont(self.bold, 8.5)
            c.setFillColor(colors.HexColor("#0F172A"))
            cx = x
            first_label_done = False
            for col, cw in zip(cols, widths):
                if col["kind"] == "number" and not self.blank:
                    all_rows = el.get("_rows_all") or rows
                    c.drawRightString(cx + cw - 5, ry + 6, _num(table_total(el, all_rows, col["key"])))
                elif not first_label_done:
                    c.drawString(cx + 5, ry + 6, "Total")
                    first_label_done = True
                cx += cw
            n += 1

        c.setStrokeColor(colors.HexColor("#CBD5E1"))
        c.setLineWidth(0.5)
        c.rect(x, top - row_h * n, w, row_h * n, stroke=1, fill=0)
        cx = x
        for cw in widths[:-1]:
            cx += cw
            c.line(cx, top, cx, top - row_h * n)
        for i in range(1, n):
            c.line(x, top - row_h * i, x + w, top - row_h * i)

    def _aligned(self, c, text, x, y, w, align):
        if align == "center":
            c.drawCentredString(x + w / 2, y, text)
        elif align == "right":
            c.drawRightString(x + w, y, text)
        else:
            c.drawString(x, y, text)


def render_form_pdf(schema, values=None, *, reference="", submitted_at=None, submitter="",
                    blank=False, status_label=""):
    """
    -> (pdf_bytes, signature_boxes)

    signature_boxes: [{"element_id", "step", "label", "role", "page",
                       "sig": (x, y, w, h), "date": (x, y, w, h) | None}]
    in page fractions with a top-left origin, ready for SignatureField.
    """
    painter = _Painter(
        schema, values, reference=reference, submitted_at=submitted_at,
        submitter=submitter, blank=blank, status_label=status_label,
    )
    return painter.render()
