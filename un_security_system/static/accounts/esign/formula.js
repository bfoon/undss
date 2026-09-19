/* accounts/esign/formula.js — UN PASS eSign Studio
 *
 * Browser mirror of accounts/form_formula_esign.py.
 *
 * New helpers:
 *   COUNT(...)              count non-empty items (text or number)
 *   COUNTNUM(...)           count numeric values only
 *   ROWS(...)               count table rows/items
 *   @row                    current table row number
 *   @rows                   number of saved rows
 *   ALPHA(@row)             A, B, C, ... AA
 *   ROMAN(@row)             I, II, III, IV
 *   SERIAL(@reference, ...) automatic submission/document serial
 */
(function (global) {
  "use strict";

  var MAX_EXPR = 600, MAX_PASSES = 8;

  function FormulaError(message) { this.name = "FormulaError"; this.message = message; }
  FormulaError.prototype = Object.create(Error.prototype);
  function fail(message) { throw new FormulaError(message); }

  function num(value, strict) {
    if (value === null || value === undefined || value === "") return strict ? null : 0;
    if (typeof value === "boolean") return value ? 1 : 0;
    if (typeof value === "number") return isFinite(value) ? value : (strict ? null : 0);
    if (Array.isArray(value)) return value.length;
    var s = String(value).trim().replace(/,/g, "").replace(/%/g, "");
    if (!s) return strict ? null : 0;
    var parsed = Number(s);
    return isNaN(parsed) ? (strict ? null : 0) : parsed;
  }

  function text(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (Array.isArray(value)) return value.map(text).join(", ");
    return String(value);
  }

  function bool(value) {
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value !== 0;
    if (Array.isArray(value)) return value.length > 0;
    return ["1", "true", "yes", "y", "on", "approved"].indexOf(String(value).trim().toLowerCase()) !== -1;
  }

  function asDate(value) {
    var t = String(value || "").trim().slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(t)) return null;
    var d = new Date(t + "T00:00:00Z");
    return isNaN(d.getTime()) ? null : d;
  }

  function isoDate(d) { return d.toISOString().slice(0, 10); }

  function flatten(args) {
    var out = [];
    args.forEach(function (a) {
      if (Array.isArray(a)) out.push.apply(out, flatten(a));
      else out.push(a);
    });
    return out;
  }

  function numbers(args) {
    var out = [];
    flatten(args).forEach(function (a) {
      var n = num(a, true);
      if (n !== null) out.push(n);
    });
    return out;
  }

  function nonblank(args) {
    return flatten(args).filter(function (x) { return text(x).trim(); });
  }

  function round(value, digits) {
    digits = Math.trunc(num(digits));
    var factor = Math.pow(10, digits), n = num(value);
    return Math.floor(Math.abs(n) * factor + 0.5) / factor * (n >= 0 ? 1 : -1);
  }

  function group(n, decimals) {
    return n.toLocaleString("en-US", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
  }

  function alphaNumber(value) {
    var n = Math.trunc(num(value)), out = "";
    if (n <= 0) return "";
    while (n > 0) {
      n -= 1;
      out = String.fromCharCode(65 + (n % 26)) + out;
      n = Math.floor(n / 26);
    }
    return out;
  }

  function romanNumber(value) {
    var n = Math.trunc(num(value));
    if (n <= 0) return "";
    if (n > 3999) return String(n);
    var pairs = [
      [1000, "M"], [900, "CM"], [500, "D"], [400, "CD"],
      [100, "C"], [90, "XC"], [50, "L"], [40, "XL"],
      [10, "X"], [9, "IX"], [5, "V"], [4, "IV"], [1, "I"]
    ];
    var out = "";
    pairs.forEach(function (pair) {
      while (n >= pair[0]) { out += pair[1]; n -= pair[0]; }
    });
    return out;
  }

  function padNumber(value, width) {
    var s = String(Math.trunc(num(value)));
    width = Math.max(0, Math.min(20, Math.trunc(num(width === undefined ? 4 : width))));
    while (s.length < width) s = "0" + s;
    return s;
  }

  function sequenceValue(value, style, pad) {
    var n = Math.trunc(num(value));
    style = String(style || "number").toLowerCase();
    if (["alpha", "letters", "letter", "upper_alpha", "upper-alpha"].indexOf(style) !== -1) return alphaNumber(n);
    if (["lower_alpha", "lower-alpha", "letters_lower", "letter_lower"].indexOf(style) !== -1) return alphaNumber(n).toLowerCase();
    if (["roman", "upper_roman", "upper-roman"].indexOf(style) !== -1) return romanNumber(n);
    if (["lower_roman", "lower-roman"].indexOf(style) !== -1) return romanNumber(n).toLowerCase();
    return Math.trunc(num(pad)) > 0 ? padNumber(n, pad) : String(n);
  }

  function referenceSequence(reference) {
    var matches = String(reference || "").match(/\d+/g);
    return matches && matches.length ? parseInt(matches[matches.length - 1], 10) : 1;
  }

  function serialValue(reference, pattern, style, start, pad) {
    var ref = text(reference).trim();
    var seq = Math.max(1, referenceSequence(ref) + Math.trunc(num(start === undefined ? 1 : start)) - 1);
    var serial = sequenceValue(seq, style || "number", pad === undefined ? 4 : pad);
    var now = new Date();
    var month = String(now.getMonth() + 1).padStart(2, "0");
    var day = String(now.getDate()).padStart(2, "0");
    var result = String(pattern || "SN-{year}-{serial}");
    var map = {
      "{serial}": serial,
      "{seq}": serial,
      "{year}": String(now.getFullYear()),
      "{month}": month,
      "{day}": day,
      "{reference}": ref
    };
    Object.keys(map).forEach(function (token) {
      result = result.split(token).join(map[token]);
    });
    return result;
  }

  var FUNCTIONS = {
    SUM: function () { return numbers([].slice.call(arguments)).reduce(function (a, b) { return a + b; }, 0); },
    AVERAGE: function () { var v = numbers([].slice.call(arguments)); return v.length ? v.reduce(function (a, b) { return a + b; }, 0) / v.length : 0; },
    AVG: function () { var v = numbers([].slice.call(arguments)); return v.length ? v.reduce(function (a, b) { return a + b; }, 0) / v.length : 0; },
    MIN: function () { var v = numbers([].slice.call(arguments)); return v.length ? Math.min.apply(null, v) : 0; },
    MAX: function () { var v = numbers([].slice.call(arguments)); return v.length ? Math.max.apply(null, v) : 0; },

    COUNT: function () { return nonblank([].slice.call(arguments)).length; },
    COUNTA: function () { return nonblank([].slice.call(arguments)).length; },
    COUNTNUM: function () { return numbers([].slice.call(arguments)).length; },
    ROWS: function () {
      var args = [].slice.call(arguments);
      if (args.length === 1 && Array.isArray(args[0])) return args[0].length;
      return flatten(args).length;
    },

    ROUND: round,
    ROUNDUP: function (v, d) { var f = Math.pow(10, Math.trunc(num(d))); return Math.ceil(num(v) * f) / f; },
    ROUNDDOWN: function (v, d) { var f = Math.pow(10, Math.trunc(num(d))); return Math.floor(num(v) * f) / f; },
    CEILING: function (v) { return Math.ceil(num(v)); },
    FLOOR: function (v) { return Math.floor(num(v)); },
    ABS: function (v) { return Math.abs(num(v)); },
    INT: function (v) { return Math.trunc(num(v)); },
    MOD: function (a, b) { return num(b) ? num(a) % num(b) : 0; },
    POWER: function (a, b) { return Math.pow(num(a), num(b)); },
    SQRT: function (v) { return Math.sqrt(Math.max(0, num(v))); },
    PERCENT: function (part, whole) { return num(whole) ? num(part) / num(whole) * 100 : 0; },
    CLAMP: function (v, lo, hi) { return Math.max(num(lo), Math.min(num(hi), num(v))); },

    NOT: function (v) { return !bool(v); },
    ISBLANK: function (v) { return !text(v).trim(); },
    ISNUMBER: function (v) { return num(v, true) !== null; },

    CONCAT: function () { return flatten([].slice.call(arguments)).map(text).join(""); },
    JOIN: function (sep) { return flatten([].slice.call(arguments, 1)).map(text).filter(function (s) { return s.trim(); }).join(String(sep)); },
    UPPER: function (v) { return text(v).toUpperCase(); },
    LOWER: function (v) { return text(v).toLowerCase(); },
    PROPER: function (v) { return text(v).replace(/\w\S*/g, function (w) { return w[0].toUpperCase() + w.slice(1).toLowerCase(); }); },
    TRIM: function (v) { return text(v).trim(); },
    LEN: function (v) { return text(v).length; },
    LEFT: function (v, n) { return text(v).slice(0, Math.max(0, Math.trunc(num(n === undefined ? 1 : n)))); },
    RIGHT: function (v, n) { var k = Math.max(0, Math.trunc(num(n === undefined ? 1 : n))); return k ? text(v).slice(-k) : ""; },
    MID: function (v, start, n) { var s = Math.max(0, Math.trunc(num(start)) - 1); return text(v).substr(s, Math.max(0, Math.trunc(num(n)))); },
    CONTAINS: function (hay, needle) { return text(hay).toLowerCase().indexOf(text(needle).toLowerCase()) !== -1; },
    SUBSTITUTE: function (v, oldText, newText) { return text(v).split(text(oldText)).join(text(newText)); },
    TEXT: function (value, pattern) {
      pattern = String(pattern || "");
      var n = num(value, true);
      if (n === null) return text(value);
      if (pattern.indexOf("0") !== -1 || pattern.indexOf("#") !== -1) {
        var decimals = pattern.indexOf(".") !== -1 ? pattern.split(".")[1].length : 0;
        return pattern.indexOf(",") !== -1 ? group(n, decimals) : n.toFixed(decimals);
      }
      return text(value);
    },
    VALUE: function (v) { return num(v); },
    LOOKUP: function (value, table, fallback) {
      var needle = text(value).trim().toLowerCase();
      var result = fallback === undefined ? "" : fallback;
      String(table || "").split(",").forEach(function (pair) {
        var bits = pair.split("=");
        if (bits.length > 1 && bits[0].trim().toLowerCase() === needle) {
          result = bits.slice(1).join("=").trim();
        }
      });
      return result;
    },
    CHOOSE: function (index) {
      var options = [].slice.call(arguments, 1), i = Math.trunc(num(index));
      return i >= 1 && i <= options.length ? options[i - 1] : "";
    },

    ALPHA: alphaNumber,
    ROMAN: romanNumber,
    PAD: padNumber,
    SERIAL: serialValue,

    TODAY: function () { return isoDate(new Date()); },
    DAYS: function (start, end) {
      var a = asDate(start), b = asDate(end);
      return a && b ? Math.round((b - a) / 86400000) : 0;
    },
    ADDDAYS: function (value, days) {
      var base = asDate(value);
      if (!base) return "";
      base.setUTCDate(base.getUTCDate() + Math.trunc(num(days)));
      return isoDate(base);
    },
    YEAR: function (v) { var d = asDate(v); return d ? d.getUTCFullYear() : 0; },
    MONTH: function (v) { var d = asDate(v); return d ? d.getUTCMonth() + 1 : 0; },
    DAY: function (v) { var d = asDate(v); return d ? d.getUTCDate() : 0; }
  };

  var LAZY = { IF: 1, IFS: 1, IFERROR: 1, AND: 1, OR: 1 };
  var OPS = ["<=", ">=", "<>", "!=", "==", "&&", "||", "+", "-", "*", "/", "%", "^", "&", "=", "<", ">", "(", ")", ","];

  function tokenize(expr) {
    expr = String(expr || "");
    if (expr.length > MAX_EXPR) fail("The formula is too long (limit " + MAX_EXPR + " characters).");
    var tokens = [], i = 0, n = expr.length;

    while (i < n) {
      var ch = expr[i];
      if (/\s/.test(ch)) { i++; continue; }

      if (ch === "[") {
        var close = expr.indexOf("]", i);
        if (close === -1) fail("A field reference is missing its closing ].");
        tokens.push({ kind: "ref", value: expr.slice(i + 1, close).trim() });
        i = close + 1;
        continue;
      }

      if (ch === "'" || ch === '"') {
        var end = expr.indexOf(ch, i + 1);
        if (end === -1) fail("A piece of text is missing its closing quote.");
        tokens.push({ kind: "str", value: expr.slice(i + 1, end) });
        i = end + 1;
        continue;
      }

      if (/[0-9]/.test(ch) || (ch === "." && /[0-9]/.test(expr[i + 1] || ""))) {
        var m = /^\d+(\.\d+)?/.exec(expr.slice(i));
        tokens.push({ kind: "num", value: parseFloat(m[0]) });
        i += m[0].length;
        continue;
      }

      if (ch === "@") {
        var mm = /^[A-Za-z_][A-Za-z0-9_]*/.exec(expr.slice(i + 1));
        if (!mm) fail("@ must be followed by a name, such as @today.");
        tokens.push({ kind: "ref", value: "@" + mm[0] });
        i += mm[0].length + 1;
        continue;
      }

      if (/[A-Za-z_]/.test(ch)) {
        var w = /^[A-Za-z_][A-Za-z0-9_]*/.exec(expr.slice(i))[0];
        var upper = w.toUpperCase();
        tokens.push({
          kind: ["AND", "OR", "NOT", "TRUE", "FALSE"].indexOf(upper) !== -1 ? "word" : "name",
          value: upper
        });
        i += w.length;
        continue;
      }

      var matched = null;
      for (var k = 0; k < OPS.length; k++) {
        if (expr.startsWith(OPS[k], i)) { matched = OPS[k]; break; }
      }
      if (!matched) fail("\u201c" + ch + "\u201d is not something a formula can use.");
      tokens.push({ kind: "op", value: matched });
      i += matched.length;
    }

    tokens.push({ kind: "end", value: null });
    return tokens;
  }

  function Parser(tokens) { this.t = tokens; this.i = 0; }
  Parser.prototype.peek = function () { return this.t[this.i]; };
  Parser.prototype.take = function () { return this.t[this.i++]; };
  Parser.prototype.expect = function (value) {
    var token = this.take();
    if (token.value !== value) fail("Expected \u201c" + value + "\u201d in the formula.");
  };
  Parser.prototype.parse = function () {
    var tree = this.or();
    if (this.peek().kind !== "end") fail("There is something extra at the end of the formula.");
    return tree;
  };
  Parser.prototype.or = function () {
    var left = this.and();
    while (this.peek().value === "OR" || this.peek().value === "||") {
      this.take(); left = ["or", left, this.and()];
    }
    return left;
  };
  Parser.prototype.and = function () {
    var left = this.notx();
    while (this.peek().value === "AND" || this.peek().value === "&&") {
      this.take(); left = ["and", left, this.notx()];
    }
    return left;
  };
  Parser.prototype.notx = function () {
    if (this.peek().value === "NOT") { this.take(); return ["not", this.notx()]; }
    return this.cmp();
  };
  Parser.prototype.cmp = function () {
    var left = this.concat();
    while (["=", "==", "<>", "!=", "<", "<=", ">", ">="].indexOf(this.peek().value) !== -1) {
      var op = this.take().value;
      left = ["cmp", op, left, this.concat()];
    }
    return left;
  };
  Parser.prototype.concat = function () {
    var left = this.add();
    while (this.peek().value === "&") { this.take(); left = ["concat", left, this.add()]; }
    return left;
  };
  Parser.prototype.add = function () {
    var left = this.mul();
    while (this.peek().value === "+" || this.peek().value === "-") {
      var op = this.take().value;
      left = ["bin", op, left, this.mul()];
    }
    return left;
  };
  Parser.prototype.mul = function () {
    var left = this.unary();
    while (["*", "/", "%"].indexOf(this.peek().value) !== -1) {
      var op = this.take().value;
      left = ["bin", op, left, this.unary()];
    }
    return left;
  };
  Parser.prototype.unary = function () {
    if (this.peek().value === "-") { this.take(); return ["neg", this.unary()]; }
    if (this.peek().value === "+") { this.take(); return this.unary(); }
    return this.power();
  };
  Parser.prototype.power = function () {
    var base = this.atom();
    if (this.peek().value === "^") { this.take(); return ["bin", "^", base, this.unary()]; }
    return base;
  };
  Parser.prototype.atom = function () {
    var token = this.take();
    if (token.kind === "num") return ["num", token.value];
    if (token.kind === "str") return ["str", token.value];
    if (token.kind === "ref") return ["ref", token.value];

    if (token.kind === "word") {
      if (token.value === "TRUE") return ["bool", true];
      if (token.value === "FALSE") return ["bool", false];
      if (token.value === "NOT") return ["not", this.notx()];
      fail("\u201c" + token.value + "\u201d cannot start a value.");
    }

    if (token.kind === "name") {
      var name = token.value;
      if (this.peek().value !== "(") fail("\u201c" + name + "\u201d must be followed by ( \u2026 ).");
      this.expect("(");
      var args = [];
      if (this.peek().value !== ")") {
        args.push(this.or());
        while (this.peek().value === ",") {
          this.take();
          args.push(this.or());
        }
      }
      this.expect(")");
      if (!FUNCTIONS[name] && !LAZY[name]) fail("There is no function called " + name + ".");
      return ["call", name, args];
    }

    if (token.value === "(") {
      var inner = this.or();
      this.expect(")");
      return inner;
    }
    fail("The formula is incomplete.");
  };

  var CACHE = {};
  function parse(expr) {
    var key = String(expr || "");
    if (!(key in CACHE)) {
      if (Object.keys(CACHE).length > 400) CACHE = {};
      CACHE[key] = new Parser(tokenize(key)).parse();
    }
    return CACHE[key];
  }

  function compare(op, left, right) {
    var a = num(left, true), b = num(right, true);
    if (a === null || b === null) {
      a = text(left).trim().toLowerCase();
      b = text(right).trim().toLowerCase();
    }
    switch (op) {
      case "=": case "==": return a === b;
      case "<>": case "!=": return a !== b;
      case "<": return a < b;
      case "<=": return a <= b;
      case ">": return a > b;
      default: return a >= b;
    }
  }

  function evalNode(node, resolve) {
    switch (node[0]) {
      case "num": case "str": case "bool": return node[1];
      case "ref": return resolve(node[1]);
      case "neg": return -num(evalNode(node[1], resolve));
      case "not": return !bool(evalNode(node[1], resolve));
      case "and": return bool(evalNode(node[1], resolve)) && bool(evalNode(node[2], resolve));
      case "or": return bool(evalNode(node[1], resolve)) || bool(evalNode(node[2], resolve));
      case "concat": return text(evalNode(node[1], resolve)) + text(evalNode(node[2], resolve));
      case "cmp": return compare(node[1], evalNode(node[2], resolve), evalNode(node[3], resolve));
      case "bin":
        var op = node[1];
        if (op === "/") {
          var divisor = num(evalNode(node[3], resolve));
          if (divisor === 0) fail("Division by zero");
          return num(evalNode(node[2], resolve)) / divisor;
        }
        var l = num(evalNode(node[2], resolve)), r = num(evalNode(node[3], resolve));
        if (op === "+") return l + r;
        if (op === "-") return l - r;
        if (op === "*") return l * r;
        if (op === "%") { if (r === 0) fail("Division by zero"); return l % r; }
        return Math.pow(l, r);
      case "call":
        return callFn(node[1], node[2], resolve);
    }
    fail("The formula could not be worked out.");
  }

  function callFn(name, args, resolve) {
    if (name === "IF") {
      if (args.length < 2) fail("IF needs a test and a result.");
      return bool(evalNode(args[0], resolve))
        ? evalNode(args[1], resolve)
        : (args.length > 2 ? evalNode(args[2], resolve) : "");
    }
    if (name === "IFS") {
      for (var i = 0; i + 1 < args.length; i += 2) {
        if (bool(evalNode(args[i], resolve))) return evalNode(args[i + 1], resolve);
      }
      return args.length % 2 ? evalNode(args[args.length - 1], resolve) : "";
    }
    if (name === "IFERROR") {
      try { return evalNode(args[0], resolve); }
      catch (e) { return args.length > 1 ? evalNode(args[1], resolve) : ""; }
    }
    if (name === "AND") return args.every(function (a) { return bool(evalNode(a, resolve)); });
    if (name === "OR") return args.some(function (a) { return bool(evalNode(a, resolve)); });
    return FUNCTIONS[name].apply(null, args.map(function (a) { return evalNode(a, resolve); }));
  }

  function evaluate(expr, resolve) {
    return evalNode(parse(expr), resolve);
  }

  function formulaOf(el) {
    var f = el && el.formula;
    return (f && f.enabled && f.expr) ? f : null;
  }

  function computedColumns(el) {
    return (el.columns || []).filter(function (c) {
      return String(c.formula || "").trim();
    });
  }

  function rowsOf(value) {
    return Array.isArray(value)
      ? value.filter(function (r) { return r && typeof r === "object"; })
      : [];
  }

  function format(value, spec) {
    var output = (spec && spec.output) || "number";
    if (output === "number") {
      var n = num(value, true);
      if (n === null) return "";
      var decimals = Number(spec.decimals || 0);
      var r = round(n, decimals);
      return decimals ? r.toFixed(decimals) : String(Math.trunc(r));
    }
    if (output === "date") {
      var d = asDate(value);
      return d ? isoDate(d) : text(value);
    }
    if (output === "yesno") return bool(value) ? "yes" : "no";
    return text(value);
  }

  function resolverFor(values, index, row, rowIndex, rowCount, reference) {
    return function (name) {
      name = String(name).trim();
      if (name.charAt(0) === "@") {
        var special = name.toLowerCase();
        if (special === "@today") return isoDate(new Date());
        if (special === "@reference") return reference || values["@reference"] || "";
        if (["@row", "@rownumber", "@row_number"].indexOf(special) !== -1) return rowIndex || 0;
        if (["@rows", "@rowcount", "@row_count"].indexOf(special) !== -1) return rowCount || 0;
        return values[name] || "";
      }

      if (row && name.indexOf(".") === -1 && Object.prototype.hasOwnProperty.call(row, name)) {
        return row[name];
      }

      if (name.indexOf(".") !== -1) {
        var bits = name.split(".");
        var key = bits[0], column = bits.slice(1).join(".");
        return rowsOf(values[key]).map(function (r) {
          return r[column] === undefined ? "" : r[column];
        });
      }

      return values[name] === undefined ? "" : values[name];
    };
  }

  function fingerprint(values, fieldFormulas, tables) {
    var parts = fieldFormulas.map(function (el) {
      return String(values[el.key] === undefined ? "" : values[el.key]);
    });
    tables.forEach(function (el) {
      parts.push(JSON.stringify(values[el.key] || []));
    });
    return parts.join("\u241f");
  }

  function compute(elements, values, reference) {
    values = Object.assign({}, values || {});
    reference = reference || values["@reference"] || "";

    var errors = {}, index = {};
    (elements || []).forEach(function (el) {
      if (el.key) index[el.key] = el;
    });

    var fieldFormulas = (elements || []).filter(function (el) {
      return el.key && formulaOf(el);
    });
    var tables = (elements || []).filter(function (el) {
      return el.key && el.type === "table" && computedColumns(el).length;
    });

    if (!fieldFormulas.length && !tables.length) {
      return { values: values, errors: errors };
    }

    for (var pass = 0; pass < MAX_PASSES; pass++) {
      var before = fingerprint(values, fieldFormulas, tables);
      errors = {};

      tables.forEach(function (el) {
        var rows = rowsOf(values[el.key]);
        var columns = computedColumns(el);
        var rowCount = rows.length;

        rows.forEach(function (row, indexNumber) {
          columns.forEach(function (column) {
            try {
              var result = evaluate(
                column.formula,
                resolverFor(values, index, row, indexNumber + 1, rowCount, reference)
              );

              var kind = column.kind || "text";
              var output = column.output || (
                kind === "number" ? "number" :
                kind === "date" ? "date" :
                "text"
              );

              row[column.key] = format(result, {
                output: output,
                decimals: column.decimals === undefined
                  ? (kind === "number" ? 2 : 0)
                  : column.decimals
              });
            } catch (e) {
              errors[el.key + "." + column.key] = e.message;
              row[column.key] = "";
            }
          });
        });

        values[el.key] = rows;
      });

      fieldFormulas.forEach(function (el) {
        var f = formulaOf(el);
        if (f.recalc === "if_empty" && String(values[el.key] || "").trim()) return;
        try {
          values[el.key] = format(
            evaluate(f.expr, resolverFor(values, index, null, 0, 0, reference)),
            f
          );
        } catch (e) {
          errors[el.key] = e.message;
          values[el.key] = "";
        }
      });

      if (fingerprint(values, fieldFormulas, tables) === before) break;
    }

    return { values: values, errors: errors };
  }

  function totalsFor(el, rows) {
    var out = {};
    var cleanRows = rowsOf(rows);

    (el.columns || []).forEach(function (column) {
      var mode = String(column.total || "").toLowerCase();
      if (!mode && !(el.show_total && column.kind === "number")) return;
      mode = mode || "sum";

      if (mode === "count") {
        out[column.key] = cleanRows.filter(function (r) {
          return text(r[column.key]).trim();
        }).length;
        return;
      }

      var vals = numbers(cleanRows.map(function (r) { return r[column.key]; }));
      if (!vals.length) out[column.key] = 0;
      else if (mode === "sum") out[column.key] = vals.reduce(function (a, b) { return a + b; }, 0);
      else if (mode === "avg") out[column.key] = vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
      else if (mode === "min") out[column.key] = Math.min.apply(null, vals);
      else if (mode === "max") out[column.key] = Math.max.apply(null, vals);
    });

    return out;
  }

  function readForm(root, elements, seed) {
    var values = Object.assign({}, seed || {});

    (elements || []).forEach(function (el) {
      if (!el.key) return;

      var item = root.querySelector('.fs-item[data-key="' + el.key + '"]');
      if (!item) return;

      var name = "f_" + el.key;

      if (el.type === "checkboxes") {
        values[el.key] = [].slice.call(
          item.querySelectorAll('input[name="' + name + '"]:checked')
        ).map(function (i) { return i.value; });
      } else if (el.type === "radio" || el.type === "yesno") {
        var checked = item.querySelector('input[name="' + name + '"]:checked');
        values[el.key] = checked ? checked.value : "";
      } else if (el.type === "table") {
        values[el.key] = [].slice.call(item.querySelectorAll("tbody tr"))
          .map(function (tr) {
            var row = {};
            tr.querySelectorAll("[data-col]").forEach(function (cell) {
              row[cell.dataset.col] = (
                cell.value !== undefined ? cell.value : cell.textContent
              ).trim();
            });
            return row;
          })
          .filter(function (r) {
            return Object.keys(r).some(function (k) { return r[k]; });
          });
      } else {
        var input = item.querySelector('[name="' + name + '"]');
        if (input) values[el.key] = input.value;
      }
    });

    return values;
  }

  function writeForm(root, elements, result) {
    var values = result.values, errors = result.errors;

    (elements || []).forEach(function (el) {
      if (!el.key) return;
      var item = root.querySelector('.fs-item[data-key="' + el.key + '"]');
      if (!item) return;

      if (formulaOf(el)) {
        var box = item.querySelector('[name="f_' + el.key + '"]');
        var shown = values[el.key] === undefined ? "" : values[el.key];
        if (box && box.value !== shown) box.value = shown;

        var readout = item.querySelector("[data-calc-readout]");
        if (readout) readout.textContent = shown === "" ? "—" : shown;

        var note = item.querySelector("[data-calc-error]");
        if (note) {
          note.textContent = errors[el.key] || "";
          note.classList.toggle("d-none", !errors[el.key]);
        }
      }

      if (el.type === "table") {
        var rows = rowsOf(values[el.key]);
        var bodyRows = [].slice.call(item.querySelectorAll("tbody tr"));

        computedColumns(el).forEach(function (column) {
          bodyRows.forEach(function (tr, rowIndex) {
            var cell = tr.querySelector('[data-col="' + column.key + '"]');
            if (!cell) return;
            var value = rows[rowIndex] ? (rows[rowIndex][column.key] || "") : "";
            if (cell.value !== undefined) {
              if (cell.value !== value) cell.value = value;
            } else {
              cell.textContent = value;
            }
          });
        });

        var totals = totalsFor(el, rows);
        item.querySelectorAll("[data-total]").forEach(function (cell) {
          var value = totals[cell.dataset.total];
          cell.textContent = value === undefined
            ? "0"
            : group(round(value, 2), Number.isInteger(value) ? 0 : 2);
        });
      }
    });
  }

  function attach(root, elements, seed) {
    if (!root || !elements) return null;

    var interesting = elements.some(function (el) {
      return formulaOf(el) || computedColumns(el).length;
    });
    if (!interesting) return null;

    elements.forEach(function (el) {
      if (!formulaOf(el)) return;
      var item = root.querySelector('.fs-item[data-key="' + el.key + '"]');
      if (!item) return;

      item.querySelectorAll('[name="f_' + el.key + '"]').forEach(function (box) {
        box.readOnly = true;
        box.classList.add("fs-calc-box");
        box.setAttribute("tabindex", "-1");
      });
    });

    elements.forEach(function (el) {
      computedColumns(el).forEach(function (column) {
        root.querySelectorAll(
          '.fs-item[data-key="' + el.key + '"] [data-col="' + column.key + '"]'
        ).forEach(function (cell) {
          if (cell.readOnly !== undefined) {
            cell.readOnly = true;
            cell.classList.add("fs-calc-box");
            cell.setAttribute("tabindex", "-1");
          }
        });
      });
    });

    var run = function () {
      var values = readForm(root, elements, seed);
      writeForm(root, elements, compute(elements, values, values["@reference"] || ""));
    };

    var queued = false;
    var schedule = function () {
      if (queued) return;
      queued = true;
      requestAnimationFrame(function () {
        queued = false;
        run();
      });
    };

    root.addEventListener("input", schedule);
    root.addEventListener("change", schedule);
    root.addEventListener("click", function (e) {
      if (e.target.closest(".fs-row-add, .fs-row-del")) setTimeout(run, 0);
    });

    run();

    return {
      recalculate: run,
      compute: function (values, reference) {
        return compute(elements, values, reference);
      }
    };
  }

  global.StudioFormula = {
    parse: parse,
    evaluate: evaluate,
    compute: compute,
    totalsFor: totalsFor,
    attach: attach,
    readForm: readForm,
    functions: Object.keys(FUNCTIONS).concat(Object.keys(LAZY)).sort(),
    sequenceValue: sequenceValue,
    serialValue: serialValue,
    check: function (expr) {
      try {
        parse(expr);
        return { ok: true, error: "" };
      } catch (e) {
        return { ok: false, error: e.message };
      }
    },
    FormulaError: FormulaError
  };
})(window);


/* -------------------------------------------------------------------------
 * Easy Formula Builder for the Form Designer
 *
 * This deliberately lives in formula.js because that file is already loaded
 * by the designer. It enhances the existing inspector without changing the
 * designer's schema/save logic.
 * ---------------------------------------------------------------------- */
(function () {
  "use strict";

  function escAttr(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function slug(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 40) || "field";
  }

  function fire(input) {
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.focus();
  }

  function insertAtCursor(input, value) {
    var start = input.selectionStart == null ? input.value.length : input.selectionStart;
    var end = input.selectionEnd == null ? start : input.selectionEnd;
    input.value = input.value.slice(0, start) + value + input.value.slice(end);
    input.selectionStart = input.selectionEnd = start + value.length;
    fire(input);
  }

  function tableColumnKeys(block) {
    var parent = block.parentElement;
    if (!parent) return [];

    return [].slice.call(parent.querySelectorAll(".fb-col-block")).map(function (colBlock) {
      var label = colBlock.querySelector('[data-ck="label"]');
      return {
        label: label ? label.value : "Column",
        key: slug(label ? label.value : "column")
      };
    });
  }

  function enhanceTableFormula(block) {
    if (block.dataset.easyFormulaReady === "1") return;

    var input = block.querySelector('[data-ck="formula"]');
    if (!input) return;

    block.dataset.easyFormulaReady = "1";

    var wrap = document.createElement("div");
    wrap.className = "border rounded bg-light p-2 mt-1 mb-2";
    wrap.setAttribute("data-easy-table-formula", "1");

    wrap.innerHTML =
      '<div class="small fw-semibold mb-1"><i class="bi bi-magic me-1"></i>Easy formula</div>' +
      '<div class="d-flex flex-wrap gap-1 mb-2">' +
        '<button type="button" class="btn btn-sm btn-outline-primary" data-seq="@row">1, 2, 3…</button>' +
        '<button type="button" class="btn btn-sm btn-outline-primary" data-seq="ALPHA(@row)">A, B, C…</button>' +
        '<button type="button" class="btn btn-sm btn-outline-primary" data-seq="LOWER(ALPHA(@row))">a, b, c…</button>' +
        '<button type="button" class="btn btn-sm btn-outline-primary" data-seq="ROMAN(@row)">I, II, III…</button>' +
        '<button type="button" class="btn btn-sm btn-outline-primary" data-seq="LOWER(ROMAN(@row))">i, ii, iii…</button>' +
      '</div>' +
      '<div class="input-group input-group-sm mb-1">' +
        '<select class="form-select" data-easy-col-insert><option value="">Insert a column…</option></select>' +
        '<button type="button" class="btn btn-outline-secondary" data-op=" * ">×</button>' +
        '<button type="button" class="btn btn-outline-secondary" data-op=" + ">+</button>' +
        '<button type="button" class="btn btn-outline-secondary" data-op=" - ">−</button>' +
        '<button type="button" class="btn btn-outline-secondary" data-op=" / ">÷</button>' +
      '</div>' +
      '<div class="small text-muted">Example: choose Qty, click ×, choose Price. ' +
      'For numbering, use one of the buttons above.</div>';

    input.closest(".fb-opt-row").insertAdjacentElement("afterend", wrap);

    function refreshColumnOptions() {
      var select = wrap.querySelector("[data-easy-col-insert]");
      var selected = select.value;
      select.innerHTML = '<option value="">Insert a column…</option>' +
        tableColumnKeys(block).map(function (col) {
          return '<option value="[' + escAttr(col.key) + ']">' +
            escAttr(col.label) + " [" + escAttr(col.key) + "]</option>";
        }).join("");
      select.value = selected;
    }

    refreshColumnOptions();

    wrap.querySelectorAll("[data-seq]").forEach(function (button) {
      button.addEventListener("click", function () {
        input.value = button.dataset.seq;
        fire(input);
      });
    });

    wrap.querySelector("[data-easy-col-insert]").addEventListener("change", function () {
      if (!this.value) return;
      insertAtCursor(input, this.value);
      this.value = "";
    });

    wrap.querySelectorAll("[data-op]").forEach(function (button) {
      button.addEventListener("click", function () {
        insertAtCursor(input, button.dataset.op);
      });
    });

    // Make footer wording explicit.
    var total = block.querySelector('[data-ck="total"]');
    if (total) {
      [].slice.call(total.options).forEach(function (option) {
        var names = {
          "": "No footer calculation",
          "sum": "Sum values",
          "avg": "Average",
          "min": "Lowest value",
          "max": "Highest value",
          "count": "Count items"
        };
        if (Object.prototype.hasOwnProperty.call(names, option.value)) {
          option.textContent = names[option.value];
        }
      });
      total.title = "What should appear under this column";
    }
  }

  function enhanceFieldFormula(inspector) {
    var checkbox = inspector.querySelector('[data-k="formula.enabled"]');

    if (checkbox && checkbox.dataset.easySerialReady !== "1") {
      checkbox.dataset.easySerialReady = "1";

      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-sm btn-outline-primary w-100 mb-2";
      button.innerHTML = '<i class="bi bi-upc-scan me-1"></i>Use automatic serial number';
      checkbox.closest("label").insertAdjacentElement("afterend", button);

      button.addEventListener("click", function () {
        if (!checkbox.checked) {
          checkbox.checked = true;
          checkbox.dispatchEvent(new Event("change", { bubbles: true }));
        }
        // The designer rerenders the inspector after enabling the formula.
        setTimeout(function () {
          var expr = document.querySelector('#inspector [data-k="formula.expr"]');
          if (expr) {
            expr.value = 'SERIAL(@reference, "SN-{year}-{serial}", "number", 1, 4)';
            fire(expr);

            var output = document.querySelector('#inspector [data-k="formula.output"]');
            if (output) {
              output.value = "text";
              output.dispatchEvent(new Event("change", { bubbles: true }));
            }
          }
        }, 0);
      });
    }

    var expr = inspector.querySelector('[data-k="formula.expr"]');
    if (!expr || expr.dataset.easyBuilderReady === "1") return;
    expr.dataset.easyBuilderReady = "1";

    var builder = document.createElement("div");
    builder.className = "border rounded bg-light p-2 mb-2";
    builder.setAttribute("data-easy-field-formula", "1");
    builder.innerHTML =
      '<div class="small fw-semibold mb-2"><i class="bi bi-upc-scan me-1"></i>Automatic serial builder</div>' +
      '<label class="form-label small mb-1">Structure / pattern</label>' +
      '<input class="form-control form-control-sm mb-2" data-serial-pattern value="SN-{year}-{serial}" ' +
        'placeholder="SN-{year}-{serial}">' +
      '<div class="row g-2 mb-2">' +
        '<div class="col-6"><label class="small">Series</label>' +
          '<select class="form-select form-select-sm" data-serial-style>' +
            '<option value="number">Numbers — 0001</option>' +
            '<option value="alpha">Letters — A, B, C</option>' +
            '<option value="lower_alpha">Letters — a, b, c</option>' +
            '<option value="roman">Roman — I, II, III</option>' +
            '<option value="lower_roman">Roman — i, ii, iii</option>' +
          '</select></div>' +
        '<div class="col-3"><label class="small">Start</label>' +
          '<input class="form-control form-control-sm" type="number" min="1" data-serial-start value="1"></div>' +
        '<div class="col-3"><label class="small">Digits</label>' +
          '<input class="form-control form-control-sm" type="number" min="0" max="12" data-serial-pad value="4"></div>' +
      '</div>' +
      '<button type="button" class="btn btn-sm btn-primary w-100" data-serial-apply>' +
        '<i class="bi bi-check2 me-1"></i>Apply serial formula</button>' +
      '<div class="small text-muted mt-2">' +
        'Pattern tokens: <code>{serial}</code>, <code>{year}</code>, <code>{month}</code>, ' +
        '<code>{day}</code>, <code>{reference}</code>. ' +
        'Use Advanced appearance below for font size, field height, background, border, colour and free placement.' +
      '</div>';

    expr.insertAdjacentElement("afterend", builder);

    builder.querySelector("[data-serial-apply]").addEventListener("click", function () {
      var pattern = builder.querySelector("[data-serial-pattern]").value || "{serial}";
      var style = builder.querySelector("[data-serial-style]").value || "number";
      var start = Math.max(1, parseInt(builder.querySelector("[data-serial-start]").value || "1", 10));
      var pad = Math.max(0, Math.min(12, parseInt(builder.querySelector("[data-serial-pad]").value || "0", 10)));

      // Formula strings only allow a matching quote delimiter; remove quotes
      // from the pattern rather than creating an invalid expression.
      pattern = pattern.replace(/"/g, "").replace(/'/g, "");

      expr.value =
        'SERIAL(@reference, "' + pattern + '", "' + style + '", ' + start + ', ' + pad + ')';
      fire(expr);

      var output = inspector.querySelector('[data-k="formula.output"]');
      if (output) {
        output.value = "text";
        output.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });

    // Enhance Insert-a-question with ready-made table operations.
    var insert = inspector.querySelector("#fxInsert");
    if (insert && insert.dataset.easyOpsReady !== "1") {
      insert.dataset.easyOpsReady = "1";
      var original = [].slice.call(insert.options).filter(function (option) {
        return /^\[[^.]+\.[^\]]+\]$/.test(option.value || "");
      });

      original.forEach(function (option) {
        var ref = option.value;

        var count = document.createElement("option");
        count.value = "COUNT(" + ref + ")";
        count.textContent = "Count items — " + option.textContent;
        insert.appendChild(count);

        var sum = document.createElement("option");
        sum.value = "SUM(" + ref + ")";
        sum.textContent = "Sum values — " + option.textContent;
        insert.appendChild(sum);
      });
    }
  }

  function enhance() {
    var inspector = document.getElementById("inspector");
    if (!inspector) return;

    inspector.querySelectorAll(".fb-col-block").forEach(enhanceTableFormula);
    enhanceFieldFormula(inspector);
  }

  function start() {
    var inspector = document.getElementById("inspector");
    if (!inspector) return;

    enhance();

    var observer = new MutationObserver(function () {
      enhance();
    });
    observer.observe(inspector, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
