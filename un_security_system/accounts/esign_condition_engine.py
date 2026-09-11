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
}

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
        "field": _s(rule.get("field"), 40),
        "op": op,
        "compare": compare,
        "field2": _s(rule.get("field2"), 40) if compare == "field" else "",
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
            value = _s(node.get(key), 40)
            if value and value not in seen:
                seen.add(value)
                result.append(value)

    walk(tree or {})
    return result


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

    if op in {"gt", "gte", "lt", "lte", "between"}:
        a = _as_number(left)
        b = _as_number(right)
        if a is None or b is None:
            return False
        if op == "between":
            c = _as_number(rule.get("value2"))
            if c is None:
                return False
            lo, hi = sorted((b, c))
            return lo <= a <= hi
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
