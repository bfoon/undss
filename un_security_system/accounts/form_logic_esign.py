"""Optional v2 form-state, presentation and absolute-layout helpers for UNPASS."""

from __future__ import annotations

from typing import Any, Dict, Mapping

from .esign_condition_engine import clean_tree, evaluate_tree

DISPLAY_STATES = ("active", "readonly", "completed", "disabled", "hidden")
HEX_DEFAULTS = {
    "background": "#FFFFFF",
    "border_color": "#CBD5E1",
    "text_color": "#111827",
    "label_color": "#334155",
}


def _num(value, lo, hi, default):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _int(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _hex(value, default):
    value = str(value or "").strip()
    if len(value) == 7 and value.startswith("#"):
        try:
            int(value[1:], 16)
            return value.upper()
        except ValueError:
            pass
    return default


def clean_presentation(raw: Mapping[str, Any] | None) -> Dict[str, Any]:
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "label_font_size": _int(raw.get("label_font_size"), 6, 24, 9),
        "value_font_size": _int(raw.get("value_font_size"), 6, 24, 10),
        "height": _int(raw.get("height"), 20, 300, 38),
        "padding": _int(raw.get("padding"), 0, 24, 6),
        "background": _hex(raw.get("background"), HEX_DEFAULTS["background"]),
        "border_color": _hex(raw.get("border_color"), HEX_DEFAULTS["border_color"]),
        "text_color": _hex(raw.get("text_color"), HEX_DEFAULTS["text_color"]),
        "label_color": _hex(raw.get("label_color"), HEX_DEFAULTS["label_color"]),
        "border_width": _num(raw.get("border_width"), 0, 5, 1),
        "radius": _int(raw.get("radius"), 0, 30, 4),
    }


def clean_canvas(raw: Mapping[str, Any] | None) -> Dict[str, Any]:
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "enabled": bool(raw.get("enabled")),
        "page": _int(raw.get("page"), 1, 50, 1),
        "x": _num(raw.get("x"), 0, 10000, 0),
        "y": _num(raw.get("y"), 0, 10000, 0),
        "w": _num(raw.get("w"), 20, 10000, 240),
        "h": _num(raw.get("h"), 16, 10000, 40),
        "snap": _int(raw.get("snap"), 2, 100, 20),
        "z": _int(raw.get("z"), 0, 999, 0),
    }


def clean_state_rule(raw: Mapping[str, Any] | None) -> Dict[str, Any]:
    raw = raw if isinstance(raw, Mapping) else {}
    state = raw.get("state") if raw.get("state") in DISPLAY_STATES else "active"
    completed_mode = raw.get("completed_mode")
    if completed_mode not in ("show_values", "message", "collapse"):
        completed_mode = "show_values"
    return {
        "state": state,
        "when": clean_tree(raw.get("when") or {"logic": "and", "rules": []}),
        "message": str(raw.get("message") or "")[:160],
        "completed_mode": completed_mode,
    }


def clean_v2_element_metadata(element: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "presentation": clean_presentation(element.get("presentation")),
        "canvas": clean_canvas(element.get("canvas")),
        "state_rules": [
            clean_state_rule(x) for x in (element.get("state_rules") or [])[:10]
            if isinstance(x, Mapping)
        ],
    }


def is_filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def element_state(
    element: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    scope: str = "submitter",
    section: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Decide the state shown to the current filler.

    Rule order matters: first matching state rule wins. A field's own rules are
    checked first, then those of the heading it sits under, so a whole section
    can be greyed out or hidden at once.
    If no rule matches, a value filled by a previous workflow step is shown as
    completed/read-only to later steps.
    """
    for owner, source in ((element, "own"), (section, "section")):
        if owner is None or (source == "section" and owner is element):
            continue
        for raw_rule in owner.get("state_rules") or []:
            rule = clean_state_rule(raw_rule)
            if evaluate_tree(values, rule["when"]):
                return {**rule, "from": source}

    fill_by = str(element.get("fill_by") or "submitter")
    key = element.get("key")
    value = values.get(key) if key else None
    if key and is_filled(value) and fill_by != scope:
        return {
            "from": "auto",
            "state": "completed",
            "when": {"logic": "and", "rules": []},
            "message": "Already filled",
            "completed_mode": "show_values",
        }
    if fill_by != scope and key:
        return {
            "from": "auto",
            "state": "readonly",
            "when": {"logic": "and", "rules": []},
            "message": "Completed later in the workflow",
            "completed_mode": "show_values",
        }
    return {
        "from": "auto",
        "state": "active",
        "when": {"logic": "and", "rules": []},
        "message": "",
        "completed_mode": "show_values",
    }


def annotate_states(schema, values, *, scope="submitter", extras=None):
    """
    Return a deep-copied schema with `runtime_state` on each element.
    Templates can then render inactive/completed/disabled sections in grey
    without changing the saved schema.

    Headings get a state too, and pass it down to the fields beneath them.
    Rules can also read computed values (table totals, days between dates, and
    — inside a workflow — the requester and the run), so `values` is expanded
    with derive_values() first.
    """
    import copy

    from .esign_condition_engine import derive_values

    out = copy.deepcopy(schema or {})
    extras = extras or {}
    resolved = derive_values(values or {}, out, who=extras.get("who"), run=extras.get("run"),
                             steps=extras.get("steps"), today=extras.get("today"))
    section = None
    for el in out.get("elements") or []:
        if el.get("type") == "heading":
            section = el if (el.get("state_rules") or []) else None
        if el.get("key") or el.get("type") == "heading":
            el["runtime_state"] = element_state(el, resolved, scope=scope, section=section)
    return out


def logic_payload(schema, values, *, scope="submitter", extras=None, hide_keys=()):
    """
    What the browser needs to keep states up to date as someone types: the
    elements, the answers it can't read off the page, and the
    workflow extras. Answers the viewer isn't allowed to see are left out
    unless a rule actually reads them.
    """
    from .esign_condition_engine import referenced_fields

    schema = schema or {}
    needed = set()
    for el in schema.get("elements") or []:
        for rule in el.get("state_rules") or []:
            needed.update(referenced_fields(clean_state_rule(rule)["when"]))
    elements = []
    for el in schema.get("elements") or []:
        slim = {k: el[k] for k in ("id", "key", "type", "label", "options", "required", "fill_by", "state_rules") if k in el}
        if el.get("type") == "table":
            slim["columns"] = [{"key": c.get("key"), "label": c.get("label"), "kind": c.get("kind")} for c in el.get("columns") or []]
        elements.append(slim)
    hidden = set(hide_keys or ())
    vals = {k: v for k, v in (values or {}).items() if k not in hidden or k in needed}
    return {"elements": elements, "values": vals, "scope": scope, "extras": extras or {}}


# ─────────────────────────────────────────────────────────────────────────────
# Free placement
# ─────────────────────────────────────────────────────────────────────────────

CANVAS_W, CANVAS_H = 794, 1123          # the designer canvas, in the same units the PDF uses


def canvas_problems(schema):
    """
    Readable warnings about freely placed fields: ones that overlap each other,
    and ones that run off the page. Unlike the flowing layout, free placement
    can't push things out of the way, so two fields can quietly print on top of
    one another.
    """
    placed = []
    for el in (schema or {}).get("elements") or []:
        canvas = el.get("canvas") or {}
        if canvas.get("enabled"):
            placed.append((el, canvas))
    problems = []

    def name(el):
        return el.get("label") or el.get("text") or el.get("type")

    for el, c in placed:
        if c["x"] + c["w"] > CANVAS_W + 1 or c["y"] + c["h"] > CANVAS_H + 1:
            problems.append(f"“{name(el)}” runs off the page — move it back inside.")
    for i, (a, ca) in enumerate(placed):
        for b, cb in placed[i + 1:]:
            if ca.get("page") != cb.get("page"):
                continue
            overlap_w = min(ca["x"] + ca["w"], cb["x"] + cb["w"]) - max(ca["x"], cb["x"])
            overlap_h = min(ca["y"] + ca["h"], cb["y"] + cb["h"]) - max(ca["y"], cb["y"])
            if overlap_w > 1 and overlap_h > 1:
                problems.append(f"“{name(a)}” and “{name(b)}” overlap — one will print on top of the other.")
    return problems
