"""Advanced, backwards-compatible conditions for UNPASS eSign workflows."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping

CONDITION_OPS = {
    "eq": "is",
    "neq": "is not",
    "contains": "contains",
    "not_contains": "does not contain",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "gt": "is greater than",
    "gte": "is at least",
    "lt": "is less than",
    "lte": "is at most",
    "between": "is between",
    "empty": "is empty",
    "not_empty": "is filled in",
    "before": "is before",
    "after": "is after",
    "on_or_before": "is on or before",
    "on_or_after": "is on or after",
    "truthy": "is true / yes",
    "falsy": "is false / no",
    # --- added: lists of options, sets of choices and dates relative to today
    "in": "is one of",
    "not_in": "is not one of",
    "not_between": "is not between",
    "includes_any": "includes any of",
    "includes_all": "includes all of",
    "in_past": "is in the past",
    "in_future": "is in the future",
    "within_days": "is within the next … days",
    "beyond_days": "is more than … days away",
}

#: Operators needing no right-hand value at all.
UNARY_OPS = {"empty", "not_empty", "truthy", "falsy", "in_past", "in_future"}
#: Operators whose value is a comma-separated list.
LIST_OPS = {"in", "not_in", "includes_any", "includes_all"}
#: Operators needing both `value` and `value2`.
RANGE_OPS = {"between", "not_between"}
#: Operators whose value is a number of days.
DAY_OPS = {"within_days", "beyond_days"}
#: Operators that can compare against another field instead of a typed value.
FIELD_COMPARABLE = {"eq", "neq", "gt", "gte", "lt", "lte", "before", "after", "on_or_before", "on_or_after"}

MAX_DEPTH = 4
MAX_RULES = 40
MAX_VALUE = 300


def _s(value: Any, limit: int = MAX_VALUE) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _as_number(value: Any):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _as_date(value: Any):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _s(value, 40)
    if not text:
        return None
    # HTML date inputs use ISO yyyy-mm-dd. Keep this strict and predictable.
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return _s(value).lower() in {"1", "true", "yes", "y", "on", "approved"}


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value)
    return _s(value)


def clean_rule(rule: Mapping[str, Any] | None) -> Dict[str, Any]:
    rule = rule if isinstance(rule, Mapping) else {}
    op = rule.get("op") if rule.get("op") in CONDITION_OPS else "eq"
    compare = "field" if rule.get("compare") == "field" else "value"
    return {
        "field": _s(rule.get("field"), 80),
        "op": op,
        "compare": compare,
        "field2": _s(rule.get("field2"), 80) if compare == "field" else "",
        "value": _s(rule.get("value")),
        "value2": _s(rule.get("value2")),
    }


def clean_tree(tree: Mapping[str, Any] | None, *, depth: int = 0, budget=None) -> Dict[str, Any]:
    """Sanitise a nested condition tree posted by the browser."""
    if budget is None:
        budget = [MAX_RULES]
    if depth >= MAX_DEPTH or budget[0] <= 0:
        return {"logic": "and", "rules": []}

    tree = tree if isinstance(tree, Mapping) else {}
    logic = "or" if tree.get("logic") == "or" else "and"
    out: List[Dict[str, Any]] = []
    for item in (tree.get("rules") or []):
        if budget[0] <= 0:
            break
        if isinstance(item, Mapping) and ("rules" in item or item.get("type") == "group"):
            child = clean_tree(item, depth=depth + 1, budget=budget)
            if child["rules"]:
                child["type"] = "group"
                child["negate"] = bool(item.get("negate"))
                out.append(child)
        else:
            budget[0] -= 1
            out.append({"type": "rule", **clean_rule(item)})
    return {"type": "group", "logic": logic, "negate": bool(tree.get("negate")), "rules": out}


def legacy_tree(field: str, op: str = "eq", value: Any = "") -> Dict[str, Any]:
    return clean_tree({
        "logic": "and",
        "rules": [{"field": field, "op": op, "value": value}],
    })


def referenced_fields(tree: Mapping[str, Any] | None) -> List[str]:
    result = []
    seen = set()

    def walk(node):
        if not isinstance(node, Mapping):
            return
        if "rules" in node:
            for child in node.get("rules") or []:
                walk(child)
            return
        for key in ("field", "field2"):
            value = _s(node.get(key), 80)
            if value and value not in seen:
                seen.add(value)
                result.append(value)

    walk(tree or {})
    return result


def _as_list(value: Any) -> List[str]:
    """The left-hand side as a set of chosen options, however it is stored."""
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    elif value in (None, ""):
        items = []
    else:
        items = [value]
    return [str(v).strip().casefold() for v in items if str(v).strip()]


def _split_list(text: Any) -> List[str]:
    """A typed right-hand side like "Basse, Kerewan" as a list."""
    return [part.strip().casefold() for part in str(text or "").split(",") if part.strip()]


def _today(values: Mapping[str, Any]):
    """Today, or the date a caller pinned in the values (used by the tests)."""
    pinned = _as_date(values.get("@today")) if isinstance(values, Mapping) else None
    return pinned or date.today()


def evaluate_rule(values: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    rule = clean_rule(rule)
    left = values.get(rule["field"])
    right = values.get(rule["field2"]) if rule["compare"] == "field" else rule["value"]
    op = rule["op"]

    left_text = _text(left).strip()
    right_text = _text(right).strip()
    empty = not left if isinstance(left, (list, tuple, set, dict)) else left_text == ""

    if op == "empty":
        return empty
    if op == "not_empty":
        return not empty
    if op == "truthy":
        return _as_bool(left)
    if op == "falsy":
        return not _as_bool(left)

    if op in LIST_OPS:
        wanted = _split_list(right)
        if not wanted:
            return False
        if op in {"in", "not_in"}:
            hit = left_text.strip().casefold() in wanted
            return hit if op == "in" else not hit
        chosen = set(_as_list(left))
        return bool(chosen & set(wanted)) if op == "includes_any" else set(wanted) <= chosen

    if op in {"in_past", "in_future", "within_days", "beyond_days"}:
        a = _as_date(left)
        if a is None:
            return False
        today = _today(values)
        if op == "in_past":
            return a < today
        if op == "in_future":
            return a > today
        span = _as_number(right)
        if span is None:
            return False
        from datetime import timedelta

        edge = today + timedelta(days=int(span))
        return today <= a <= edge if op == "within_days" else a > edge

    if op in {"gt", "gte", "lt", "lte", "between", "not_between"}:
        a = _as_number(left)
        b = _as_number(right)
        if op in RANGE_OPS:
            c = _as_number(rule.get("value2"))
            if a is None or b is None or c is None:
                return False
            lo, hi = sorted((b, c))
            inside = lo <= a <= hi
            return inside if op == "between" else not inside
        if a is None or b is None:
            return False
        return {
            "gt": a > b,
            "gte": a >= b,
            "lt": a < b,
            "lte": a <= b,
        }[op]

    if op in {"before", "after", "on_or_before", "on_or_after"}:
        a = _as_date(left)
        b = _as_date(right)
        if a is None or b is None:
            return False
        return {
            "before": a < b,
            "after": a > b,
            "on_or_before": a <= b,
            "on_or_after": a >= b,
        }[op]

    a = left_text.casefold()
    b = right_text.casefold()

    if op == "contains":
        return bool(b) and b in a
    if op == "not_contains":
        return not b or b not in a
    if op == "starts_with":
        return bool(b) and a.startswith(b)
    if op == "ends_with":
        return bool(b) and a.endswith(b)
    if op == "neq":
        # Preserve useful numeric equality semantics from the old engine.
        an, bn = _as_number(left), _as_number(right)
        return (an != bn) if (an is not None and bn is not None) else a != b

    an, bn = _as_number(left), _as_number(right)
    return (an == bn) if (an is not None and bn is not None) else a == b


def evaluate_tree(values: Mapping[str, Any], tree: Mapping[str, Any] | None) -> bool:
    tree = clean_tree(tree)
    results = []
    for item in tree.get("rules") or []:
        if item.get("type") == "group" or "rules" in item:
            result = evaluate_tree(values, item)
        else:
            result = evaluate_rule(values, item)
        results.append(bool(result))

    # An empty AND group is false here because a flow with no actual rule
    # should never silently take the Yes branch.
    if not results:
        result = False
    elif tree.get("logic") == "or":
        result = any(results)
    else:
        result = all(results)

    return not result if tree.get("negate") else result


def describe_tree(tree: Mapping[str, Any] | None) -> str:
    tree = clean_tree(tree)

    def describe(node):
        if node.get("type") == "group" or "rules" in node:
            parts = [describe(x) for x in node.get("rules") or []]
            parts = [p for p in parts if p]
            if not parts:
                return ""
            text = (" OR " if node.get("logic") == "or" else " AND ").join(parts)
            if len(parts) > 1:
                text = f"({text})"
            return f"NOT {text}" if node.get("negate") else text

        op = CONDITION_OPS.get(node.get("op"), node.get("op", ""))
        rhs = node.get("field2") if node.get("compare") == "field" else node.get("value")
        if node.get("op") in {"empty", "not_empty", "truthy", "falsy"}:
            return f"{node.get('field')} {op}"
        if node.get("op") == "between":
            return f"{node.get('field')} {op} {rhs} and {node.get('value2')}"
        return f"{node.get('field')} {op} {rhs}"

    return describe(tree)


# ─────────────────────────────────────────────────────────────────────────────
# Computed values
#
# A rule's `field` is looked up in a plain mapping, so anything that can be
# worked out ahead of time can be checked the same way as a normal answer.
# Computed keys start with "@" and are added by derive_values():
#
#   @sum:costs:amount     total of a number column in a table
#   @min: / @max:         smallest / largest value in that column
#   @rows:costs           number of filled rows
#   @count:services       how many options are ticked
#   @days:start:end       days from one date answer to another
#   @who:job_title        the requester (name, email, email_domain,
#                         job_title, agency, office)
#   @run:returns          times returned for changes; also days_open, pages
#   @step:<node>:comment  the comment left at a workflow step
# ─────────────────────────────────────────────────────────────────────────────

COMPUTED_PREFIX = "@"
WHO_ATTRS = {"name": "Name", "email": "Email", "email_domain": "Email domain",
             "job_title": "Job title", "agency": "Agency", "office": "Country office"}
RUN_ATTRS = {"returns": "Times returned for changes", "days_open": "Days since the run started",
             "pages": "Pages in the document"}


def _rows(value):
    return [r for r in value if isinstance(r, Mapping)] if isinstance(value, list) else []


def derive_values(values, schema=None, *, who=None, run=None, steps=None, today=None):
    """
    values plus every computed entry the rules above can read. Existing keys are
    never overwritten, so a form answer always wins over a computed one.
    """
    out = dict(values or {})
    if today is not None:
        out.setdefault("@today", today.isoformat() if hasattr(today, "isoformat") else str(today))
    for key, attrs, prefix in ((who, WHO_ATTRS, "@who:"), (run, RUN_ATTRS, "@run:")):
        for name in attrs:
            if isinstance(key, Mapping) and key.get(name) not in (None, ""):
                out[f"{prefix}{name}"] = key[name]
    for node_id, comment in (steps or {}).items():
        out[f"@step:{node_id}:comment"] = comment

    for el in (schema or {}).get("elements") or []:
        key, kind = el.get("key"), el.get("type")
        if not key:
            continue
        value = out.get(key)
        if kind == "table":
            rows = _rows(value)
            out[f"@rows:{key}"] = len(rows)
            for col in el.get("columns") or []:
                if col.get("kind") != "number":
                    continue
                nums = [n for n in (_as_number(r.get(col["key"])) for r in rows) if n is not None]
                out[f"@sum:{key}:{col['key']}"] = sum(nums)
                if nums:
                    out[f"@min:{key}:{col['key']}"] = min(nums)
                    out[f"@max:{key}:{col['key']}"] = max(nums)
        elif kind == "checkboxes":
            out[f"@count:{key}"] = len(_as_list(value))

    dates = [el for el in (schema or {}).get("elements") or [] if el.get("type") == "date" and el.get("key")]
    for a in dates:
        for b in dates:
            if a is b:
                continue
            start, end = _as_date(out.get(a["key"])), _as_date(out.get(b["key"]))
            if start and end:
                out[f"@days:{a['key']}:{b['key']}"] = (end - start).days
    return out


def catalog(schema=None, *, workflow=False, node_labels=None):
    """
    Everything a rule can check, for the rule builder:
    [{"field", "label", "type", "group", "options"}]. `type` is one of
    text / number / date / choices / yesno and decides which operators fit.
    """
    kinds = {"number": "number", "date": "date", "checkboxes": "choices", "yesno": "yesno"}
    items, dates = [], []
    for el in (schema or {}).get("elements") or []:
        key, kind, label = el.get("key"), el.get("type"), el.get("label") or el.get("key")
        if not key or kind in {"heading", "paragraph", "divider", "spacer", "image", "signature"}:
            continue
        if kind == "table":
            items.append({"field": f"@rows:{key}", "label": f"{label} · number of rows", "type": "number", "group": "Tables"})
            for col in el.get("columns") or []:
                if col.get("kind") == "number":
                    for prefix, word in (("@sum", "total"), ("@min", "smallest"), ("@max", "largest")):
                        items.append({"field": f"{prefix}:{key}:{col['key']}",
                                      "label": f"{label} · {word} {col.get('label') or col['key']}",
                                      "type": "number", "group": "Tables"})
            continue
        entry = {"field": key, "label": label, "type": kinds.get(kind, "text"), "group": "Form answers"}
        if el.get("options"):
            entry["options"] = list(el["options"])
        if kind == "yesno":
            entry["options"] = ["yes", "no"]
        items.append(entry)
        if kind == "checkboxes":
            items.append({"field": f"@count:{key}", "label": f"{label} · how many ticked", "type": "number", "group": "Form answers"})
        if kind == "date":
            dates.append((key, label))
    for a_key, a_label in dates:
        for b_key, b_label in dates:
            if a_key != b_key:
                items.append({"field": f"@days:{a_key}:{b_key}", "label": f"Days from {a_label} to {b_label}",
                              "type": "number", "group": "Dates"})
    if workflow:
        for name, label in WHO_ATTRS.items():
            items.append({"field": f"@who:{name}", "label": f"Requester's {label.lower()}", "type": "text", "group": "Requester"})
        for name, label in RUN_ATTRS.items():
            items.append({"field": f"@run:{name}", "label": label, "type": "number", "group": "This run"})
        for node_id, label in (node_labels or {}).items():
            items.append({"field": f"@step:{node_id}:comment", "label": f"Comment at “{label}”", "type": "text", "group": "Step comments"})
    return items


def label_for(field, catalog_items=None, schema=None):
    for item in catalog_items or catalog(schema):
        if item["field"] == field:
            return item["label"]
    return field


def describe_tree_labelled(tree, schema=None, node_labels=None, catalog_items=None):
    """describe_tree, but with question names instead of keys."""
    items = catalog_items or catalog(schema, workflow=bool(node_labels), node_labels=node_labels)
    names = {i["field"]: i["label"] for i in items}

    def describe(node):
        if node.get("type") == "group" or "rules" in node:
            parts = [p for p in (describe(x) for x in node.get("rules") or []) if p]
            if not parts:
                return ""
            text = (" or " if node.get("logic") == "or" else " and ").join(parts)
            if len(parts) > 1:
                text = f"({text})"
            return f"not {text}" if node.get("negate") else text
        op_label = CONDITION_OPS.get(node.get("op"), node.get("op", ""))
        left = names.get(node.get("field"), node.get("field") or "?")
        if node.get("op") in UNARY_OPS:
            return f"{left} {op_label}"
        right = names.get(node.get("field2"), node.get("field2")) if node.get("compare") == "field" else node.get("value")
        if node.get("op") in RANGE_OPS:
            return f"{left} {op_label} {right} and {node.get('value2')}"
        if node.get("op") in DAY_OPS:
            return f"{left} {op_label.replace(' … days', '')} {right} days"
        return f"{left} {op_label} {right}"

    return describe(clean_tree(tree))


def problems(tree, schema=None, *, allow_computed=True, node_ids=None):
    """Readable reasons a tree can't work — missing questions, blank values."""
    items = catalog(schema, workflow=True, node_labels={n: n for n in (node_ids or ())}) if schema is not None else []
    known = {i["field"] for i in items}
    types = {i["field"]: i["type"] for i in items}
    found = []

    def walk(node):
        if node.get("type") == "group" or "rules" in node:
            if not (node.get("rules") or []):
                found.append("A group has no rules in it.")
            for child in node.get("rules") or []:
                walk(child)
            return
        field, op = node.get("field"), node.get("op")
        name = label_for(field, items) if field else ""
        if not field:
            found.append("A rule has no question chosen.")
            return
        if field.startswith(COMPUTED_PREFIX) and not allow_computed:
            found.append(f"“{name}” can't be used here.")
            return
        if schema is not None and field not in known:
            found.append(f"A rule reads a question that no longer exists: {field}.")
            return
        kind = types.get(field)
        if kind == "yesno" and op in {"gt", "gte", "lt", "lte", "before", "after"}:
            found.append(f"“{CONDITION_OPS.get(op, op)}” doesn't suit {name}.")
        if op in UNARY_OPS:
            return
        if node.get("compare") == "field":
            if not node.get("field2"):
                found.append(f"Choose the question to compare {name} with.")
            elif schema is not None and node["field2"] not in known:
                found.append(f"A rule compares with a question that no longer exists: {node['field2']}.")
            return
        if _s(node.get("value")) == "":
            found.append(f"Enter a value for “{name} {CONDITION_OPS.get(op, op)}”.")
        if op in RANGE_OPS and _s(node.get("value2")) == "":
            found.append(f"Enter both ends of the range for “{name}”.")

    tree = clean_tree(tree)
    walk(tree)
    if not (tree.get("rules") or []):
        found.insert(0, "Add at least one rule.")
    return list(dict.fromkeys(found))
