# accounts/form_formula_esign.py
"""
UN PASS — eSign Studio: calculations on forms.

A field, or a column inside a table, can carry a formula. The formula is
written the way people write them in a spreadsheet, but it refers to questions
by name instead of by cell:

    [quantity] * [unit_price]
    SUM([items.amount]) * 0.15
    IF([staff_type] = "International", [rate] * 1.25, [rate])
    ROUND(SUM([items.amount]) + [freight], 2)
    DAYS([start_date], [end_date]) * [daily_rate]

Where a formula can live
------------------------
field       element["formula"]  -> {"enabled", "expr", "output", "decimals", "recalc"}
table cell  column["formula"]   -> a row-scope expression; inside it, a bare
                                   [column_key] means "this row's cell"
table total column["total"]     -> "" | sum | avg | min | max | count

What it can read
----------------
[key]                a question on this form (any type)
[table.column]       every cell of that column, as a list — feed it to SUM/AVG/…
@today               today's date

Nothing else. There is no attribute access, no imports, no Python evaluation:
the expression is tokenised, parsed into a small tree and walked. The browser
runs the same grammar (static/accounts/esign/formula.js) so what someone sees
while typing is what the server stores.

The server always recomputes. A computed answer posted by the browser is
discarded — the formula is the authority, not the input box.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta

MAX_EXPR = 600
MAX_NODES = 400
MAX_PASSES = 8
OUTPUT_KINDS = ("number", "text", "date", "yesno")
RECALC_MODES = ("always", "if_empty")
TOTAL_MODES = ("", "sum", "avg", "min", "max", "count")

_REF_RE = re.compile(r"\[([A-Za-z0-9_.@# ]{1,90})\]")
_NUM_RE = re.compile(r"\d+(\.\d+)?")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class FormulaError(ValueError):
    """A formula that cannot be parsed, or cannot be worked out."""


# ─────────────────────────────────────────────────────────────────────────────
# Values in and out
# ─────────────────────────────────────────────────────────────────────────────

BLANK = ""


def to_number(value, *, strict=False):
    if value is None or value is BLANK:
        return None if strict else 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return float(len(value))
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text:
        return None if strict else 0.0
    try:
        return float(text)
    except ValueError:
        if strict:
            return None
        return 0.0


def to_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(to_text(v) for v in value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value)


def to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "approved"}


def to_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _flatten(args):
    out = []
    for a in args:
        if isinstance(a, (list, tuple)):
            out.extend(_flatten(a))
        else:
            out.append(a)
    return out


def _numbers(args):
    values = []
    for item in _flatten(args):
        num = to_number(item, strict=True)
        if num is not None:
            values.append(num)
    return values


# ─────────────────────────────────────────────────────────────────────────────
# Functions
# ─────────────────────────────────────────────────────────────────────────────

def _round(value, digits=0):
    digits = int(to_number(digits))
    factor = 10 ** digits
    number = to_number(value)
    # Half-up, the way an accountant expects — not banker's rounding.
    return math.floor(abs(number) * factor + 0.5) / factor * (1 if number >= 0 else -1)


def _divide(a, b):
    divisor = to_number(b)
    if divisor == 0:
        raise FormulaError("Division by zero")
    return to_number(a) / divisor


def _text_fn(value, pattern=""):
    pattern = str(pattern or "")
    number = to_number(value, strict=True)
    if number is None:
        return to_text(value)
    if pattern.count("0") or pattern.count("#"):
        decimals = len(pattern.split(".")[1]) if "." in pattern else 0
        grouped = "," in pattern
        return f"{number:,.{decimals}f}" if grouped else f"{number:.{decimals}f}"
    return to_text(value)


def _days(start, end):
    a, b = to_date(start), to_date(end)
    if not a or not b:
        return 0.0
    return float((b - a).days)


def _add_days(value, days):
    base = to_date(value)
    if not base:
        return ""
    return (base + timedelta(days=int(to_number(days)))).isoformat()


def _lookup(value, table, default=""):
    """LOOKUP([grade], "P3=1200, P4=1500, P5=1900", 0)"""
    needle = to_text(value).strip().casefold()
    for pair in str(table or "").split(","):
        if "=" not in pair:
            continue
        key, result = pair.split("=", 1)
        if key.strip().casefold() == needle:
            return result.strip()
    return default


def _choose(index, *options):
    i = int(to_number(index))
    return options[i - 1] if 1 <= i <= len(options) else ""


FUNCTIONS = {
    # maths
    "SUM": lambda *a: sum(_numbers(a)),
    "AVERAGE": lambda *a: (sum(_numbers(a)) / len(_numbers(a))) if _numbers(a) else 0.0,
    "AVG": lambda *a: (sum(_numbers(a)) / len(_numbers(a))) if _numbers(a) else 0.0,
    "MIN": lambda *a: min(_numbers(a)) if _numbers(a) else 0.0,
    "MAX": lambda *a: max(_numbers(a)) if _numbers(a) else 0.0,
    "COUNT": lambda *a: float(len(_numbers(a))),
    "COUNTA": lambda *a: float(len([x for x in _flatten(a) if to_text(x).strip()])),
    "ROUND": _round,
    "ROUNDUP": lambda v, d=0: math.ceil(to_number(v) * 10 ** int(to_number(d))) / 10 ** int(to_number(d)),
    "ROUNDDOWN": lambda v, d=0: math.floor(to_number(v) * 10 ** int(to_number(d))) / 10 ** int(to_number(d)),
    "CEILING": lambda v: float(math.ceil(to_number(v))),
    "FLOOR": lambda v: float(math.floor(to_number(v))),
    "ABS": lambda v: abs(to_number(v)),
    "INT": lambda v: float(int(to_number(v))),
    "MOD": lambda a, b: math.fmod(to_number(a), to_number(b) or 1),
    "POWER": lambda a, b: to_number(a) ** to_number(b),
    "SQRT": lambda v: math.sqrt(max(0.0, to_number(v))),
    "PERCENT": lambda part, whole: (to_number(part) / to_number(whole) * 100) if to_number(whole) else 0.0,
    "CLAMP": lambda v, lo, hi: max(to_number(lo), min(to_number(hi), to_number(v))),
    # logic (IF / IFS / IFERROR / AND / OR are handled lazily by the evaluator)
    "NOT": lambda v: not to_bool(v),
    "ISBLANK": lambda v: not to_text(v).strip(),
    "ISNUMBER": lambda v: to_number(v, strict=True) is not None,
    # text
    "CONCAT": lambda *a: "".join(to_text(x) for x in _flatten(a)),
    "JOIN": lambda sep, *a: str(sep).join(to_text(x) for x in _flatten(a) if to_text(x).strip()),
    "UPPER": lambda v: to_text(v).upper(),
    "LOWER": lambda v: to_text(v).lower(),
    "PROPER": lambda v: to_text(v).title(),
    "TRIM": lambda v: to_text(v).strip(),
    "LEN": lambda v: float(len(to_text(v))),
    "LEFT": lambda v, n=1: to_text(v)[: max(0, int(to_number(n)))],
    "RIGHT": lambda v, n=1: to_text(v)[-max(0, int(to_number(n))):] if int(to_number(n)) > 0 else "",
    "MID": lambda v, start=1, n=1: to_text(v)[max(0, int(to_number(start)) - 1): max(0, int(to_number(start)) - 1) + max(0, int(to_number(n)))],
    "CONTAINS": lambda hay, needle: to_text(needle).casefold() in to_text(hay).casefold(),
    "SUBSTITUTE": lambda v, old, new: to_text(v).replace(to_text(old), to_text(new)),
    "TEXT": _text_fn,
    "VALUE": lambda v: to_number(v),
    "LOOKUP": _lookup,
    "CHOOSE": _choose,
    # dates
    "TODAY": lambda: date.today().isoformat(),
    "DAYS": _days,
    "ADDDAYS": _add_days,
    "YEAR": lambda v: float(to_date(v).year) if to_date(v) else 0.0,
    "MONTH": lambda v: float(to_date(v).month) if to_date(v) else 0.0,
    "DAY": lambda v: float(to_date(v).day) if to_date(v) else 0.0,
}

LAZY_FUNCTIONS = {"IF", "IFS", "IFERROR", "AND", "OR"}
FUNCTION_NAMES = sorted(set(FUNCTIONS) | LAZY_FUNCTIONS)


# ─────────────────────────────────────────────────────────────────────────────
# Tokeniser and parser
# ─────────────────────────────────────────────────────────────────────────────

class _Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind, value, pos):
        self.kind, self.value, self.pos = kind, value, pos

    def __repr__(self):  # pragma: no cover - debugging only
        return f"<{self.kind} {self.value!r}>"


_OPERATORS = ["<=", ">=", "<>", "!=", "==", "&&", "||", "+", "-", "*", "/", "%",
              "^", "&", "=", "<", ">", "(", ")", ","]


def tokenize(expr):
    expr = str(expr or "")
    if len(expr) > MAX_EXPR:
        raise FormulaError(f"The formula is too long (limit {MAX_EXPR} characters).")
    tokens, i, n = [], 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "[":
            match = _REF_RE.match(expr, i)
            if not match:
                raise FormulaError("A field reference is missing its closing ].")
            tokens.append(_Token("ref", match.group(1).strip(), i))
            i = match.end()
            continue
        if ch in "'\"":
            end = expr.find(ch, i + 1)
            if end == -1:
                raise FormulaError("A piece of text is missing its closing quote.")
            tokens.append(_Token("str", expr[i + 1:end], i))
            i = end + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and expr[i + 1].isdigit()):
            match = _NUM_RE.match(expr, i)
            tokens.append(_Token("num", float(match.group(0)), i))
            i = match.end()
            continue
        if ch == "@":
            match = _NAME_RE.match(expr, i + 1)
            if not match:
                raise FormulaError("@ must be followed by a name, such as @today.")
            tokens.append(_Token("ref", "@" + match.group(0), i))
            i = match.end()
            continue
        if ch.isalpha() or ch == "_":
            match = _NAME_RE.match(expr, i)
            word = match.group(0)
            upper = word.upper()
            if upper in ("AND", "OR", "NOT", "TRUE", "FALSE"):
                tokens.append(_Token("word", upper, i))
            else:
                tokens.append(_Token("name", upper, i))
            i = match.end()
            continue
        for op in _OPERATORS:
            if expr.startswith(op, i):
                tokens.append(_Token("op", op, i))
                i += len(op)
                break
        else:
            raise FormulaError(f"“{ch}” is not something a formula can use.")
    tokens.append(_Token("end", None, n))
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.tokens, self.i, self.nodes = tokens, 0, 0

    def peek(self):
        return self.tokens[self.i]

    def take(self):
        token = self.tokens[self.i]
        self.i += 1
        return token

    def expect(self, value):
        token = self.take()
        if token.value != value:
            raise FormulaError(f"Expected “{value}” in the formula.")
        return token

    def node(self, *parts):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise FormulaError("The formula is too complicated.")
        return parts

    # precedence climbing, loosest first
    def parse(self):
        tree = self.parse_or()
        if self.peek().kind != "end":
            raise FormulaError("There is something extra at the end of the formula.")
        return tree

    def parse_or(self):
        left = self.parse_and()
        while self.peek().value in ("OR", "||"):
            self.take()
            left = self.node("or", left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.peek().value in ("AND", "&&"):
            self.take()
            left = self.node("and", left, self.parse_not())
        return left

    def parse_not(self):
        if self.peek().value == "NOT":
            self.take()
            return self.node("not", self.parse_not())
        return self.parse_compare()

    def parse_compare(self):
        left = self.parse_concat()
        while self.peek().value in ("=", "==", "<>", "!=", "<", "<=", ">", ">="):
            op = self.take().value
            left = self.node("cmp", op, left, self.parse_concat())
        return left

    def parse_concat(self):
        left = self.parse_add()
        while self.peek().value == "&":
            self.take()
            left = self.node("concat", left, self.parse_add())
        return left

    def parse_add(self):
        left = self.parse_mul()
        while self.peek().value in ("+", "-"):
            op = self.take().value
            left = self.node("bin", op, left, self.parse_mul())
        return left

    def parse_mul(self):
        left = self.parse_unary()
        while self.peek().value in ("*", "/", "%"):
            op = self.take().value
            left = self.node("bin", op, left, self.parse_unary())
        return left

    def parse_unary(self):
        if self.peek().value == "-":
            self.take()
            return self.node("neg", self.parse_unary())
        if self.peek().value == "+":
            self.take()
            return self.parse_unary()
        return self.parse_power()

    def parse_power(self):
        base = self.parse_atom()
        if self.peek().value == "^":
            self.take()
            return self.node("bin", "^", base, self.parse_unary())
        return base

    def parse_atom(self):
        token = self.take()
        if token.kind == "num":
            return self.node("num", token.value)
        if token.kind == "str":
            return self.node("str", token.value)
        if token.kind == "ref":
            return self.node("ref", token.value)
        if token.kind == "word":
            if token.value == "TRUE":
                return self.node("bool", True)
            if token.value == "FALSE":
                return self.node("bool", False)
            if token.value == "NOT":
                return self.node("not", self.parse_not())
            raise FormulaError(f"“{token.value}” cannot start a value.")
        if token.kind == "name":
            name = token.value
            if self.peek().value != "(":
                raise FormulaError(f"“{name}” must be followed by ( … ) — did you mean [{name.lower()}]?")
            self.expect("(")
            args = []
            if self.peek().value != ")":
                args.append(self.parse_or())
                while self.peek().value == ",":
                    self.take()
                    args.append(self.parse_or())
            self.expect(")")
            if name not in FUNCTIONS and name not in LAZY_FUNCTIONS:
                raise FormulaError(f"There is no function called {name}.")
            return self.node("call", name, args)
        if token.value == "(":
            inner = self.parse_or()
            self.expect(")")
            return inner
        raise FormulaError("The formula is incomplete.")


_PARSE_CACHE = {}


def parse(expr):
    key = str(expr or "")
    if key not in _PARSE_CACHE:
        if len(_PARSE_CACHE) > 500:
            _PARSE_CACHE.clear()
        _PARSE_CACHE[key] = _Parser(tokenize(key)).parse()
    return _PARSE_CACHE[key]


def references(expr):
    """Every [reference] a formula reads, as written."""
    try:
        tree = parse(expr)
    except FormulaError:
        return set()
    found = set()

    def walk(node):
        if not isinstance(node, tuple):
            return
        if node[0] == "ref":
            found.add(node[1])
            return
        for part in node[1:]:
            if isinstance(part, list):
                for child in part:
                    walk(child)
            else:
                walk(part)

    walk(tree)
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation
# ─────────────────────────────────────────────────────────────────────────────

def _compare(op, left, right):
    left_num = to_number(left, strict=True)
    right_num = to_number(right, strict=True)
    if left_num is not None and right_num is not None:
        a, b = left_num, right_num
    else:
        a, b = to_text(left).strip().casefold(), to_text(right).strip().casefold()
    if op in ("=", "=="):
        return a == b
    if op in ("<>", "!="):
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b


def evaluate(expr, resolver):
    """`resolver(name)` returns the value behind a [reference]."""
    return _eval(parse(expr), resolver)


def _eval(node, resolver):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "str":
        return node[1]
    if kind == "bool":
        return node[1]
    if kind == "ref":
        return resolver(node[1])
    if kind == "neg":
        return -to_number(_eval(node[1], resolver))
    if kind == "not":
        return not to_bool(_eval(node[1], resolver))
    if kind == "and":
        return to_bool(_eval(node[1], resolver)) and to_bool(_eval(node[2], resolver))
    if kind == "or":
        return to_bool(_eval(node[1], resolver)) or to_bool(_eval(node[2], resolver))
    if kind == "concat":
        return to_text(_eval(node[1], resolver)) + to_text(_eval(node[2], resolver))
    if kind == "cmp":
        return _compare(node[1], _eval(node[2], resolver), _eval(node[3], resolver))
    if kind == "bin":
        op = node[1]
        if op == "/":
            return _divide(_eval(node[2], resolver), _eval(node[3], resolver))
        left = to_number(_eval(node[2], resolver))
        right = to_number(_eval(node[3], resolver))
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "%":
            if right == 0:
                raise FormulaError("Division by zero")
            return math.fmod(left, right)
        if op == "^":
            try:
                return left ** right
            except (OverflowError, ValueError):
                raise FormulaError("That power is too large to work out.")
    if kind == "call":
        return _call(node[1], node[2], resolver)
    raise FormulaError("The formula could not be worked out.")


def _call(name, args, resolver):
    if name == "IF":
        if len(args) < 2:
            raise FormulaError("IF needs a test and a result.")
        if to_bool(_eval(args[0], resolver)):
            return _eval(args[1], resolver)
        return _eval(args[2], resolver) if len(args) > 2 else ""
    if name == "IFS":
        for i in range(0, len(args) - 1, 2):
            if to_bool(_eval(args[i], resolver)):
                return _eval(args[i + 1], resolver)
        return _eval(args[-1], resolver) if len(args) % 2 else ""
    if name == "IFERROR":
        try:
            return _eval(args[0], resolver)
        except FormulaError:
            return _eval(args[1], resolver) if len(args) > 1 else ""
    if name == "AND":
        return all(to_bool(_eval(a, resolver)) for a in args)
    if name == "OR":
        return any(to_bool(_eval(a, resolver)) for a in args)

    values = [_eval(a, resolver) for a in args]
    try:
        return FUNCTIONS[name](*values)
    except FormulaError:
        raise
    except ZeroDivisionError:
        raise FormulaError("Division by zero")
    except TypeError:
        raise FormulaError(f"{name} was given the wrong number of values.")
    except (ValueError, OverflowError) as exc:
        raise FormulaError(f"{name} could not be worked out ({exc}).")


# ─────────────────────────────────────────────────────────────────────────────
# Schema cleaning
# ─────────────────────────────────────────────────────────────────────────────

def clean_formula(raw):
    """Sanitise element["formula"]. Returns None when there is nothing to keep."""
    if not isinstance(raw, dict):
        return None
    expr = str(raw.get("expr") or "").strip()[:MAX_EXPR]
    if not expr:
        return None
    output = raw.get("output") if raw.get("output") in OUTPUT_KINDS else "number"
    recalc = raw.get("recalc") if raw.get("recalc") in RECALC_MODES else "always"
    try:
        decimals = max(0, min(4, int(raw.get("decimals"))))
    except (TypeError, ValueError):
        decimals = 2 if output == "number" else 0
    out = {
        "enabled": bool(raw.get("enabled", True)),
        "expr": expr,
        "output": output,
        "decimals": decimals,
        "recalc": recalc,
        "error": "",
    }
    try:
        parse(expr)
    except FormulaError as exc:
        out["error"] = str(exc)[:200]
    return out


def clean_column_formula(raw):
    expr = str(raw or "").strip()[:MAX_EXPR]
    if not expr:
        return ""
    try:
        parse(expr)
    except FormulaError:
        pass          # kept as typed so the designer can show the mistake
    return expr


def clean_total_mode(raw):
    value = str(raw or "").strip().lower()
    return value if value in TOTAL_MODES else ""


def formula_of(element):
    formula = element.get("formula")
    if isinstance(formula, dict) and formula.get("enabled") and formula.get("expr"):
        return formula
    return None


def computed_columns(element):
    return [c for c in element.get("columns") or [] if str(c.get("formula") or "").strip()]


def has_formulas(schema):
    for el in (schema or {}).get("elements") or []:
        if formula_of(el) or computed_columns(el):
            return True
        for col in el.get("columns") or []:
            if clean_total_mode(col.get("total")):
                return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Working the whole form out
# ─────────────────────────────────────────────────────────────────────────────

def _rows(value):
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _format(value, formula):
    output = formula.get("output") or "number"
    if output == "number":
        number = to_number(value, strict=True)
        if number is None:
            return ""
        decimals = int(formula.get("decimals") or 0)
        rounded = _round(number, decimals)
        return f"{rounded:.{decimals}f}" if decimals else str(int(rounded))
    if output == "date":
        parsed = to_date(value)
        return parsed.isoformat() if parsed else to_text(value)
    if output == "yesno":
        return "yes" if to_bool(value) else "no"
    return to_text(value)


def _resolver(values, schema_index, row=None, today=None):
    """Turn a [reference] into a value, in row scope or form scope."""

    def resolve(name):
        name = name.strip()
        if name.startswith("@"):
            key = name[1:].lower()
            if key == "today":
                return (today or date.today()).isoformat()
            if key in ("rowcount", "rows"):
                return float(len(row or {}))
            return values.get(name, "")
        if row is not None and "." not in name and name in row:
            return row.get(name, "")
        if "." in name:
            table_key, column = name.split(".", 1)
            element = schema_index.get(table_key)
            rows = _rows(values.get(table_key))
            if element is None and not rows:
                return []
            return [r.get(column.strip(), "") for r in rows]
        return values.get(name, "")

    return resolve


def compute_values(schema, values, *, today=None):
    """
    Work out every formula on the form and write the answers into `values`.

    Runs to a fixed point (at most MAX_PASSES rounds), so a field may feed a
    table, a table total may feed another field, and so on, whichever order
    they were drawn in. Returns (values, errors) — errors maps a field key, or
    "table.column", to a readable message.
    """
    values = dict(values or {})
    schema = schema or {}
    errors = {}
    elements = [el for el in (schema.get("elements") or []) if el.get("key")]
    index = {el["key"]: el for el in elements}

    field_formulas = [(el, formula_of(el)) for el in elements]
    field_formulas = [(el, f) for el, f in field_formulas if f]
    table_elements = [el for el in elements if el.get("type") == "table" and computed_columns(el)]

    if not field_formulas and not table_elements:
        return values, errors

    for _pass in range(MAX_PASSES):
        before = _fingerprint(values, field_formulas, table_elements)
        errors = {}

        # 1. every computed cell, table by table, row by row
        for el in table_elements:
            rows = _rows(values.get(el["key"]))
            columns = computed_columns(el)
            for row in rows:
                for column in columns:
                    resolve = _resolver(values, index, row=row, today=today)
                    try:
                        result = evaluate(column["formula"], resolve)
                        row[column["key"]] = _format(
                            result,
                            {"output": column.get("output") or ("number" if column.get("kind") != "date" else "date"),
                             "decimals": column.get("decimals", 2 if column.get("kind") == "number" else 0)},
                        )
                    except FormulaError as exc:
                        errors[f"{el['key']}.{column['key']}"] = str(exc)
                        row[column["key"]] = ""
            values[el["key"]] = rows

        # 2. then the fields
        for el, formula in field_formulas:
            key = el["key"]
            if formula.get("recalc") == "if_empty" and str(values.get(key) or "").strip():
                continue
            resolve = _resolver(values, index, today=today)
            try:
                values[key] = _format(evaluate(formula["expr"], resolve), formula)
            except FormulaError as exc:
                errors[key] = str(exc)
                values[key] = ""

        if _fingerprint(values, field_formulas, table_elements) == before:
            break
    else:
        for el, _f in field_formulas:
            errors.setdefault(
                el["key"],
                "These calculations refer to each other in a circle, so they never settle.",
            )

    return values, errors


def _fingerprint(values, field_formulas, table_elements):
    parts = [str(values.get(el["key"], "")) for el, _f in field_formulas]
    for el in table_elements:
        parts.append(repr(values.get(el["key"], "")))
    return "\u241f".join(parts)


def totals_for(element, rows):
    """{column key: total} for the columns with a total mode set."""
    out = {}
    for column in element.get("columns") or []:
        mode = clean_total_mode(column.get("total"))
        if not mode and not (element.get("show_total") and column.get("kind") == "number"):
            continue
        mode = mode or "sum"
        numbers = _numbers([r.get(column["key"]) for r in _rows(rows)])
        if mode == "count":
            out[column["key"]] = float(len([r for r in _rows(rows) if to_text(r.get(column["key"])).strip()]))
        elif not numbers:
            out[column["key"]] = 0.0
        elif mode == "sum":
            out[column["key"]] = sum(numbers)
        elif mode == "avg":
            out[column["key"]] = sum(numbers) / len(numbers)
        elif mode == "min":
            out[column["key"]] = min(numbers)
        elif mode == "max":
            out[column["key"]] = max(numbers)
    return out


def computed_keys(schema):
    """Keys the server owns — the browser may not post them."""
    return {el["key"] for el in (schema or {}).get("elements") or []
            if el.get("key") and formula_of(el)}


# ─────────────────────────────────────────────────────────────────────────────
# Help for the designer
# ─────────────────────────────────────────────────────────────────────────────

def token_catalog(schema, *, exclude_id=None):
    """
    Everything a formula on this form can refer to, for the designer's
    "insert a field" list: [{"token", "label", "kind"}].
    """
    items = [{"token": "@today", "label": "Today's date", "kind": "date"}]
    for el in (schema or {}).get("elements") or []:
        key, kind = el.get("key"), el.get("type")
        if not key or el.get("id") == exclude_id:
            continue
        label = el.get("label") or key
        if kind == "table":
            for column in el.get("columns") or []:
                items.append({
                    "token": f"[{key}.{column['key']}]",
                    "label": f"{label} · every {column.get('label') or column['key']}",
                    "kind": "list",
                })
            continue
        items.append({"token": f"[{key}]", "label": label, "kind": kind})
    return items


def describe(expr):
    """A one-line health check used by the designer and the save view."""
    try:
        parse(expr)
    except FormulaError as exc:
        return {"ok": False, "error": str(exc), "reads": []}
    return {"ok": True, "error": "", "reads": sorted(references(expr))}


def check_schema(schema):
    """
    Readable problems with the formulas on a form: bad syntax, references to
    questions that no longer exist, and circular calculations.
    """
    problems = []
    keys = {el.get("key") for el in (schema or {}).get("elements") or [] if el.get("key")}
    tables = {el["key"]: {c["key"] for c in el.get("columns") or []}
              for el in (schema or {}).get("elements") or []
              if el.get("key") and el.get("type") == "table"}

    def check(expr, where, *, row_columns=None):
        report = describe(expr)
        if not report["ok"]:
            problems.append(f"{where}: {report['error']}")
            return
        for ref in report["reads"]:
            if ref.startswith("@"):
                continue
            if "." in ref:
                table, column = ref.split(".", 1)
                if table not in tables:
                    problems.append(f"{where}: there is no table called “{table}”.")
                elif column not in tables[table]:
                    problems.append(f"{where}: “{table}” has no column called “{column}”.")
                continue
            if row_columns and ref in row_columns:
                continue
            if ref not in keys:
                problems.append(f"{where}: there is no question called “{ref}”.")

    for el in (schema or {}).get("elements") or []:
        label = el.get("label") or el.get("key") or el.get("type")
        formula = formula_of(el)
        if formula:
            check(formula["expr"], f"“{label}”")
        for column in el.get("columns") or []:
            if str(column.get("formula") or "").strip():
                check(column["formula"], f"“{label}” · column “{column.get('label') or column['key']}”",
                      row_columns={c["key"] for c in el.get("columns") or []})

    # circularity: compute_values reports it once it has run
    _v, errors = compute_values(schema, {})
    for key, message in errors.items():
        if "circle" in message:
            problems.append(f"“{key}”: {message}")
    return problems
