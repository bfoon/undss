/* accounts/esign/formula-builder.js — UN PASS eSign Studio
 *
 * A formula editor for people who do not write formulas. The expression is
 * still the plain text that accounts/form_formula_esign.py understands; this
 * is a way of assembling it without typing brackets.
 *
 *   StudioFormulaBuilder.open({
 *     expr:      "[qty] * [price]",
 *     scope:     "field" | "row",
 *     title:     "Line total",
 *     fields:    [{key, label, type, columns:[{key,label,kind}]}, …],
 *     row:       {key: "items", columns: [{key,label,kind}, …]},   // scope "row"
 *     context:   [{token: "@today", label: "Today's date"}, …],
 *     onSave:    function (expr) { … }
 *   });
 *
 * Three ways in, because people differ: drag a card from the palette, click it,
 * or switch to "Type it" and write the thing by hand. All three edit the same
 * expression, and the preview runs it on sample answers as you go.
 */
(function (global) {
  "use strict";

  var FUNCTION_GROUPS = [
    ["Adding up", [
      ["SUM(…)", "SUM(", "Adds everything up", "SUM([items.amount])"],
      ["AVERAGE(…)", "AVERAGE(", "The average", "AVERAGE([items.rate])"],
      ["COUNT(…)", "COUNT(", "How many numbers", "COUNT([items.qty])"],
      ["MIN(…)", "MIN(", "The smallest", "MIN([items.price])"],
      ["MAX(…)", "MAX(", "The largest", "MAX([items.price])"],
      ["ROUND(…)", "ROUND(", "Round to n decimals", "ROUND([total], 2)"],
      ["ABS(…)", "ABS(", "Ignore the minus sign", "ABS([difference])"],
      ["PERCENT(…)", "PERCENT(", "Part as a % of a whole", "PERCENT([spent], [budget])"]
    ]],
    ["Choosing", [
      ["IF(…)", "IF(", "One answer or another", 'IF([grade] = "P3", 1200, 900)'],
      ["IFS(…)", "IFS(", "Several tests in order", 'IFS([a] > 10, "High", TRUE, "Low")'],
      ["IFERROR(…)", "IFERROR(", "A fallback when it fails", 'IFERROR([a] / [b], 0)'],
      ["AND(…)", "AND(", "Both must be true", "AND([a] > 0, [b] > 0)"],
      ["OR(…)", "OR(", "Either will do", "OR([a] > 0, [b] > 0)"],
      ["LOOKUP(…)", "LOOKUP(", "Pick a value from a list", 'LOOKUP([city], "Banjul=120, Dakar=180", 100)'],
      ["ISBLANK(…)", "ISBLANK(", "Nothing was entered", "ISBLANK([note])"]
    ]],
    ["Words", [
      ["CONCAT(…)", "CONCAT(", "Join pieces of text", 'CONCAT([first], " ", [last])'],
      ["UPPER(…)", "UPPER(", "IN CAPITALS", "UPPER([code])"],
      ["TEXT(…)", "TEXT(", "A number as text", 'TEXT([total], "#,##0.00")'],
      ["LEN(…)", "LEN(", "How many characters", "LEN([reference])"],
      ["TRIM(…)", "TRIM(", "Remove stray spaces", "TRIM([name])"]
    ]],
    ["Dates", [
      ["DAYS(…)", "DAYS(", "Days between two dates", "DAYS([start], [end])"],
      ["ADDDAYS(…)", "ADDDAYS(", "A date n days later", "ADDDAYS([start], 30)"],
      ["YEAR(…)", "YEAR(", "The year", "YEAR([signed_on])"],
      ["TODAY()", "TODAY()", "Today's date", "TODAY()"]
    ]]
  ];

  var OPERATORS = [
    ["+", "+", "plus"], ["−", "-", "minus"], ["×", "*", "times"], ["÷", "/", "divided by"],
    ["(", "(", "open bracket"], [")", ")", "close bracket"], [",", ", ", "next value"],
    ["=", " = ", "is equal to"], ["≠", " <> ", "is not"], ["&gt;", " > ", "is more than"],
    ["&lt;", " < ", "is less than"], ["≥", " >= ", "is at least"], ["≤", " <= ", "is at most"],
    ["&amp;", " & ", "join as text"]
  ];

  var PRESETS = [
    ["Quantity × price", "[qty] * [price]", "row"],
    ["Total of a column", "SUM([table.column])", "field"],
    ["Subtotal + 15% tax", "ROUND([subtotal] * 1.15, 2)", "field"],
    ["Percentage used", "PERCENT([spent], [budget])", "field"],
    ["Days between two dates", "DAYS([start_date], [end_date])", "field"],
    ["Different rate per choice", 'IF([staff_type] = "International", [rate] * 1.25, [rate])', "field"],
    ["Daily rate × nights", "[nights] * [daily_rate]", "field"],
    ["Line total less discount", "[qty] * [price] - [discount]", "row"]
  ];

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ── splitting an expression into display chips ───────────────────────────
  // Deliberately forgiving: half-typed formulas must still render.
  var LEX = /(\[[^\]]*\]?)|(@[A-Za-z_][A-Za-z0-9_]*)|("[^"]*"?|'[^']*'?)|([A-Za-z_][A-Za-z0-9_]*\s*\()|(\d+(?:\.\d+)?)|(<=|>=|<>|!=|==|&&|\|\||[-+*/%^&=<>(),])|(\s+)|(.)/g;

  function lex(expr) {
    var out = [], match;
    LEX.lastIndex = 0;
    while ((match = LEX.exec(String(expr || ""))) !== null) {
      if (match[7]) continue;                       // whitespace is cosmetic
      var text = match[0];
      var kind = match[1] ? "ref" : match[2] ? "context" : match[3] ? "text"
               : match[4] ? "fn" : match[5] ? "number" : match[6] ? "op" : "bad";
      out.push({ text: text, kind: kind });
    }
    return out;
  }

  function join(tokens) {
    var parts = [];
    tokens.forEach(function (token, index) {
      var previous = tokens[index - 1];
      var glue = " ";
      if (index === 0) glue = "";
      else if (token.text === ")" || token.text === "," || token.text.trim() === ",") glue = "";
      else if (previous && /\($/.test(previous.text)) glue = "";
      else if (previous && previous.text.trim() === ",") glue = " ";
      parts.push(glue + token.text.trim());
    });
    return parts.join("").replace(/\s+/g, " ").trim();
  }

  // ── sample answers, so the preview shows a real number ───────────────────
  function sampleFor(field) {
    if (field.type === "number") return "10";
    if (field.type === "date") return new Date().toISOString().slice(0, 10);
    if (field.type === "yesno") return "yes";
    if (field.type === "select" || field.type === "radio") {
      return (field.options && field.options[0]) || "Option 1";
    }
    if (field.type === "checkboxes") return (field.options || ["Option 1"]).slice(0, 1);
    if (field.type === "table") {
      var row = {};
      (field.columns || []).forEach(function (c) { row[c.key] = c.kind === "number" ? "5" : "Sample"; });
      var second = {};
      (field.columns || []).forEach(function (c) { second[c.key] = c.kind === "number" ? "3" : "Sample 2"; });
      return [row, second];
    }
    return "Sample";
  }

  function buildSamples(options, overrides) {
    var values = {};
    (options.fields || []).forEach(function (field) {
      values[field.key] = sampleFor(field);
    });
    Object.keys(overrides || {}).forEach(function (key) { values[key] = overrides[key]; });
    return values;
  }

  function resolverFor(options, values, rowValues) {
    return function (name) {
      name = String(name).trim();
      if (name.charAt(0) === "@") {
        if (name.toLowerCase() === "@today") return new Date().toISOString().slice(0, 10);
        return values[name] === undefined ? "Sample" : values[name];
      }
      if (rowValues && name.indexOf(".") === -1 &&
          Object.prototype.hasOwnProperty.call(rowValues, name)) {
        return rowValues[name];
      }
      if (name.indexOf(".") !== -1) {
        var bits = name.split("."), rows = values[bits[0]];
        if (!Array.isArray(rows)) return [];
        return rows.map(function (r) { return r[bits[1]] === undefined ? "" : r[bits[1]]; });
      }
      return values[name] === undefined ? "" : values[name];
    };
  }

  // ── plain English ────────────────────────────────────────────────────────
  var OP_WORDS = {
    "+": "plus", "-": "minus", "*": "times", "/": "divided by", "%": "remainder of",
    "^": "to the power of", "&": "followed by", "=": "is", "==": "is", "<>": "is not",
    "!=": "is not", "<": "is less than", ">": "is more than", "<=": "is at most", ">=": "is at least"
  };

  var FN_WORDS = {
    SUM: "the total of", AVERAGE: "the average of", MIN: "the smallest of", MAX: "the largest of",
    COUNT: "how many of", COUNTA: "how many filled in of", ROUND: "rounded:", ROUNDUP: "rounded up:",
    ROUNDDOWN: "rounded down:", ABS: "the size of", PERCENT: "the percentage:", IF: "if:",
    IFS: "the first that matches:", IFERROR: "or, if that fails:", AND: "both of:", OR: "either of:",
    NOT: "not:", ISBLANK: "nothing entered in", LOOKUP: "looked up:", CONCAT: "joined together:",
    UPPER: "in capitals:", LOWER: "in lower case:", TRIM: "trimmed:", LEN: "the length of",
    TEXT: "formatted:", VALUE: "as a number:", DAYS: "the days between", ADDDAYS: "days added to",
    YEAR: "the year of", MONTH: "the month of", DAY: "the day of", TODAY: "today", CHOOSE: "chosen from:",
    CLAMP: "kept within:", SQRT: "the square root of", POWER: "to the power:", MOD: "the remainder of"
  };

  function explain(expr, labels) {
    var tokens = lex(expr);
    if (!tokens.length) return "";
    var words = tokens.map(function (token) {
      if (token.kind === "ref") {
        var key = token.text.replace(/[\[\]]/g, "");
        return "\u201c" + (labels[key] || key) + "\u201d";
      }
      if (token.kind === "context") return token.text === "@today" ? "today" : token.text;
      if (token.kind === "op") return OP_WORDS[token.text.trim()] || token.text;
      if (token.kind === "fn") {
        var name = token.text.replace(/\s*\($/, "").toUpperCase();
        return (FN_WORDS[name] || ("the " + name.toLowerCase() + " of")) + " (";
      }
      return token.text;
    });
    var sentence = words.join(" ").replace(/\(\s+/g, "(").replace(/\s+\)/g, ")").replace(/\s+,/g, ",");
    return sentence.charAt(0).toUpperCase() + sentence.slice(1);
  }

  // ── the modal ────────────────────────────────────────────────────────────
  var modal = null, state = null;

  function ensureModal() {
    if (modal) return modal;
    modal = document.createElement("div");
    modal.className = "modal fade fxb-modal";
    modal.tabIndex = -1;
    modal.innerHTML =
      '<div class="modal-dialog modal-xl modal-dialog-scrollable"><div class="modal-content">' +
        '<div class="modal-header">' +
          '<h5 class="modal-title text-un-primary"><i class="bi bi-calculator me-2"></i><span id="fxbTitle">Build a formula</span></h5>' +
          '<button type="button" class="btn-close" data-bs-dismiss="modal"></button>' +
        "</div>" +
        '<div class="modal-body pt-2">' +
          '<div class="fxb-grid">' +
            '<aside class="fxb-palette">' +
              '<input class="form-control form-control-sm mb-2" id="fxbSearch" placeholder="Search questions and functions…">' +
              '<div id="fxbPalette"></div>' +
            "</aside>" +
            '<section class="fxb-main">' +
              '<div class="d-flex justify-content-between align-items-center mb-1">' +
                '<label class="form-label small fw-semibold mb-0">Your formula</label>' +
                '<div class="btn-group btn-group-sm">' +
                  '<button type="button" class="btn btn-outline-secondary active" id="fxbModeBuild">Build it</button>' +
                  '<button type="button" class="btn btn-outline-secondary" id="fxbModeType">Type it</button>' +
                "</div>" +
              "</div>" +
              '<div class="fxb-canvas" id="fxbCanvas" tabindex="0"></div>' +
              '<textarea class="form-control fxb-raw d-none" id="fxbRaw" rows="3" spellcheck="false"></textarea>' +
              '<div class="d-flex justify-content-between align-items-start gap-2 mt-2">' +
                '<div class="small" id="fxbStatus"></div>' +
                '<button type="button" class="btn btn-sm btn-outline-secondary" id="fxbClear">Clear</button>' +
              "</div>" +
              '<div class="fxb-readout mt-2" id="fxbReadout"></div>' +
              '<div class="fxb-ops mt-2" id="fxbOps"></div>' +
              '<details class="mt-3"><summary class="small text-muted">Sample answers used for the preview</summary>' +
                '<div id="fxbSamples" class="row g-2 mt-1"></div></details>' +
            "</section>" +
          "</div>" +
        "</div>" +
        '<div class="modal-footer">' +
          '<span class="me-auto small text-muted">Drag a card in, or click it. Click a piece of the formula to remove it.</span>' +
          '<button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>' +
          '<button type="button" class="btn btn-un-primary" id="fxbSave"><i class="bi bi-check2 me-1"></i>Use this formula</button>' +
        "</div>" +
      "</div></div>";
    document.body.appendChild(modal);
    wire();
    return modal;
  }

  function el(id) { return modal.querySelector("#" + id); }

  // ── palette ──────────────────────────────────────────────────────────────
  function paletteGroups() {
    var groups = [];

    if (state.options.scope === "row" && state.options.row) {
      groups.push({
        name: "This row",
        hint: "The cells on the row being worked out",
        items: (state.options.row.columns || [])
          .filter(function (c) { return c.key !== state.options.columnKey; })
          .map(function (c) {
            return { label: c.label || c.key, token: "[" + c.key + "]", note: c.kind || "text", kind: "ref" };
          })
      });
    }

    var plain = [], tables = [];
    (state.options.fields || []).forEach(function (field) {
      if (field.key === state.options.selfKey) return;
      if (field.type === "table") {
        (field.columns || []).forEach(function (c) {
          tables.push({
            label: (field.label || field.key) + " · " + (c.label || c.key),
            token: "SUM([" + field.key + "." + c.key + "])",
            note: "total of the column", kind: "ref",
            alt: "[" + field.key + "." + c.key + "]"
          });
        });
        return;
      }
      plain.push({
        label: field.label || field.key,
        token: "[" + field.key + "]",
        note: field.type || "text", kind: "ref"
      });
    });

    if (plain.length) groups.push({ name: "Questions", hint: "Answers on this form", items: plain });
    if (tables.length) groups.push({ name: "Table columns", hint: "Totals across every row", items: tables });

    if ((state.options.context || []).length) {
      groups.push({
        name: "About this submission", hint: "",
        items: state.options.context.map(function (c) {
          return { label: c.label, token: c.token, note: "", kind: "context" };
        })
      });
    }

    FUNCTION_GROUPS.forEach(function (group) {
      groups.push({
        name: group[0], hint: "", items: group[1].map(function (fn) {
          return { label: fn[0], token: fn[1], note: fn[2], kind: "fn", example: fn[3] };
        })
      });
    });

    var scope = state.options.scope === "row" ? "row" : "field";
    groups.push({
      name: "Ready-made", hint: "A starting point you can edit",
      items: PRESETS.filter(function (p) { return p[2] === scope || scope === "field"; })
        .map(function (p) { return { label: p[0], token: p[1], note: "replaces the formula", kind: "preset" }; })
    });

    return groups;
  }

  function renderPalette() {
    var needle = (el("fxbSearch").value || "").trim().toLowerCase();
    var html = paletteGroups().map(function (group) {
      var items = group.items.filter(function (item) {
        return !needle || (item.label + " " + item.token + " " + (item.note || "")).toLowerCase().indexOf(needle) !== -1;
      });
      if (!items.length) return "";
      return '<div class="fxb-group">' +
        '<div class="fxb-group-name">' + esc(group.name) +
          (group.hint ? ' <span class="fxb-group-hint">' + esc(group.hint) + "</span>" : "") + "</div>" +
        items.map(function (item) {
          return '<div class="fxb-card" draggable="true" data-token="' + esc(item.token) + '"' +
                 (item.alt ? ' data-alt="' + esc(item.alt) + '"' : "") +
                 (item.kind === "preset" ? ' data-preset="1"' : "") +
                 ' data-kind="' + esc(item.kind) + '" title="' + esc(item.example || item.token) + '">' +
            '<span class="fxb-card-label">' + esc(item.label) + "</span>" +
            (item.note ? '<span class="fxb-card-note">' + esc(item.note) + "</span>" : "") +
          "</div>";
        }).join("") +
      "</div>";
    }).join("");
    el("fxbPalette").innerHTML = html || '<div class="text-muted small p-2">Nothing matches that.</div>';
  }

  // ── canvas ───────────────────────────────────────────────────────────────
  function renderCanvas() {
    var canvas = el("fxbCanvas");
    if (!state.tokens.length) {
      canvas.innerHTML = '<div class="fxb-empty">Drag a question or a function here to start. ' +
        "Or pick something ready-made from the bottom of the list.</div>";
    } else {
      canvas.innerHTML = state.tokens.map(function (token, index) {
        var label = token.text;
        if (token.kind === "ref") {
          var key = token.text.replace(/[\[\]]/g, "");
          label = state.labels[key] || key;
        }
        return '<span class="fxb-chip fxb-chip-' + token.kind + '" draggable="true" data-index="' + index +
               '" title="Click to remove">' + esc(label) +
               '<i class="bi bi-x fxb-chip-x"></i></span>' +
               '<span class="fxb-gap" data-gap="' + (index + 1) + '"></span>';
      }).join("");
      canvas.insertAdjacentHTML("afterbegin", '<span class="fxb-gap" data-gap="0"></span>');
    }
    el("fxbRaw").value = state.expr;
    validate();
  }

  function setExpr(expr, opts) {
    state.expr = String(expr || "").trim();
    state.tokens = lex(state.expr);
    if (!opts || !opts.fromRaw) renderCanvas();
    else { el("fxbRaw").value = state.expr; validate(); }
  }

  function insert(token, at) {
    var tokens = state.tokens.slice();
    var pieces = lex(token);
    at = (at === undefined || at === null) ? tokens.length : at;
    // A function card inserts its closing bracket too, so nothing is unbalanced.
    if (/\($/.test(token.trim()) && !/\)$/.test(token.trim())) pieces.push({ text: ")", kind: "op" });
    tokens.splice.apply(tokens, [at, 0].concat(pieces));
    setExpr(join(tokens));
  }

  function removeAt(index) {
    var tokens = state.tokens.slice();
    tokens.splice(index, 1);
    setExpr(join(tokens));
  }

  function moveToken(from, to) {
    var tokens = state.tokens.slice();
    var moved = tokens.splice(from, 1)[0];
    tokens.splice(from < to ? to - 1 : to, 0, moved);
    setExpr(join(tokens));
  }

  // ── validation and preview ───────────────────────────────────────────────
  function validate() {
    var status = el("fxbStatus"), readout = el("fxbReadout");
    var expr = state.expr;

    if (!expr) {
      status.innerHTML = '<span class="text-muted">Nothing yet.</span>';
      readout.innerHTML = "";
      el("fxbSave").disabled = false;
      return;
    }

    var report = global.StudioFormula ? global.StudioFormula.check(expr) : { ok: true, error: "" };
    var unknown = unknownReferences(expr);
    var plain = plainProblem(expr);

    if (plain) {
      status.innerHTML = '<span class="text-danger"><i class="bi bi-exclamation-circle me-1"></i>' +
        esc(plain) + "</span>";
      readout.innerHTML = "";
      el("fxbSave").disabled = true;
      return;
    }

    if (!report.ok) {
      status.innerHTML = '<span class="text-danger"><i class="bi bi-exclamation-circle me-1"></i>' +
        esc(report.error) + "</span>";
      readout.innerHTML = "";
      el("fxbSave").disabled = true;
      return;
    }
    if (unknown.length) {
      status.innerHTML = '<span class="text-warning"><i class="bi bi-question-circle me-1"></i>No question called ' +
        unknown.map(function (u) { return "<code>" + esc(u) + "</code>"; }).join(", ") + "</span>";
      el("fxbSave").disabled = true;
      readout.innerHTML = "";
      return;
    }

    el("fxbSave").disabled = false;
    status.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Ready</span>';

    var sentence = explain(expr, state.labels);
    var result = "";
    try {
      var values = buildSamples(state.options, state.overrides);
      var rowValues = null;
      if (state.options.scope === "row" && state.options.row) {
        rowValues = {};
        (state.options.row.columns || []).forEach(function (c) {
          rowValues[c.key] = state.overrides[c.key] !== undefined
            ? state.overrides[c.key] : (c.kind === "number" ? "5" : "Sample");
        });
      }
      result = global.StudioFormula.evaluate(expr, resolverFor(state.options, values, rowValues));
      if (typeof result === "number") result = Math.round(result * 10000) / 10000;
      if (typeof result === "boolean") result = result ? "Yes" : "No";
    } catch (error) {
      result = "— (" + error.message + ")";
    }

    readout.innerHTML =
      '<div class="fxb-readout-line"><span class="fxb-readout-tag">In words</span>' + esc(sentence) + "</div>" +
      '<div class="fxb-readout-line"><span class="fxb-readout-tag">On sample answers</span>' +
        '<strong>' + esc(String(result)) + "</strong></div>";
    renderSamples();
  }

  // The mistakes people actually make when dragging, said in words rather than
  // in parser language.
  function plainProblem(expr) {
    var tokens = lex(expr);
    var depth = 0, i;
    for (i = 0; i < tokens.length; i++) {
      var text = tokens[i].text.trim();
      if (tokens[i].kind === "fn" || text === "(") depth++;
      else if (text === ")") depth--;
      if (depth < 0) return "There is a closing bracket with nothing to close.";
    }
    if (depth > 0) {
      return depth === 1 ? "A bracket is still open — add the closing one."
                         : depth + " brackets are still open.";
    }
    var VALUE = { ref: 1, context: 1, number: 1, text: 1 };
    for (i = 1; i < tokens.length; i++) {
      if (VALUE[tokens[i].kind] && VALUE[tokens[i - 1].kind]) {
        return "Two values are next to each other — put something between them, such as " +
               "\u00d7 or +.";
      }
      if (tokens[i].kind === "fn" && VALUE[tokens[i - 1].kind]) {
        return "A value is followed straight by a function — put an operator between them.";
      }
    }
    var last = tokens[tokens.length - 1];
    if (last && last.kind === "op" && [")", ","].indexOf(last.text.trim()) === -1) {
      return "The formula ends on \u201c" + last.text.trim() + "\u201d — something is missing after it.";
    }
    return "";
  }

  function unknownReferences(expr) {
    var known = {}, out = [];
    (state.options.fields || []).forEach(function (field) {
      known[field.key] = true;
      if (field.type === "table") {
        (field.columns || []).forEach(function (c) { known[field.key + "." + c.key] = true; });
      }
    });
    if (state.options.scope === "row" && state.options.row) {
      (state.options.row.columns || []).forEach(function (c) { known[c.key] = true; });
    }
    lex(expr).forEach(function (token) {
      if (token.kind !== "ref") return;
      var key = token.text.replace(/[\[\]]/g, "").trim();
      if (key && !known[key] && out.indexOf(key) === -1) out.push(key);
    });
    return out;
  }

  function renderSamples() {
    var used = {};
    lex(state.expr).forEach(function (token) {
      if (token.kind === "ref") used[token.text.replace(/[\[\]]/g, "").split(".")[0]] = true;
    });
    var host = el("fxbSamples");
    var rows = [];
    (state.options.fields || []).forEach(function (field) {
      if (!used[field.key] || field.type === "table") return;
      var value = state.overrides[field.key] !== undefined ? state.overrides[field.key] : sampleFor(field);
      rows.push('<div class="col-6 col-lg-4"><label class="form-label small mb-1">' + esc(field.label || field.key) +
        '</label><input class="form-control form-control-sm" data-sample="' + esc(field.key) +
        '" value="' + esc(value) + '"></div>');
    });
    if (state.options.scope === "row" && state.options.row) {
      (state.options.row.columns || []).forEach(function (c) {
        if (!used[c.key]) return;
        var value = state.overrides[c.key] !== undefined ? state.overrides[c.key] : (c.kind === "number" ? "5" : "Sample");
        rows.push('<div class="col-6 col-lg-4"><label class="form-label small mb-1">' + esc(c.label || c.key) +
          ' <span class="text-muted">(this row)</span></label><input class="form-control form-control-sm" data-sample="' +
          esc(c.key) + '" value="' + esc(value) + '"></div>');
      });
    }
    host.innerHTML = rows.join("") ||
      '<div class="col-12 small text-muted">This formula does not read any answers yet.</div>';
  }

  // ── events ───────────────────────────────────────────────────────────────
  function wire() {
    el("fxbSearch").addEventListener("input", renderPalette);

    el("fxbPalette").addEventListener("click", function (event) {
      var card = event.target.closest(".fxb-card");
      if (!card) return;
      if (card.dataset.preset) {
        if (state.expr && !confirm("Replace what you have with this ready-made formula?")) return;
        setExpr(card.dataset.token);
        return;
      }
      insert(card.dataset.token, state.caret);
      state.caret = null;
    });

    el("fxbPalette").addEventListener("dragstart", function (event) {
      var card = event.target.closest(".fxb-card");
      if (!card) return;
      // State first: dataTransfer is not always handed over, and if setting it
      // throws the drop would have nothing to work with.
      state.dragging = { token: card.dataset.token, from: null, preset: !!card.dataset.preset };
      if (event.dataTransfer) {
        try {
          event.dataTransfer.setData("text/plain", card.dataset.token);
          event.dataTransfer.effectAllowed = "copy";
        } catch (error) { /* the drop falls back to state.dragging */ }
      }
    });

    var canvas = el("fxbCanvas");

    canvas.addEventListener("click", function (event) {
      var chip = event.target.closest(".fxb-chip");
      if (chip) { removeAt(+chip.dataset.index); return; }
      var gap = event.target.closest(".fxb-gap");
      if (gap) {
        state.caret = +gap.dataset.gap;
        canvas.querySelectorAll(".fxb-gap").forEach(function (g) { g.classList.remove("active"); });
        gap.classList.add("active");
      }
    });

    canvas.addEventListener("dragstart", function (event) {
      var chip = event.target.closest(".fxb-chip");
      if (!chip) return;
      state.dragging = { token: null, from: +chip.dataset.index };
      if (event.dataTransfer) {
        try {
          event.dataTransfer.setData("text/plain", state.tokens[+chip.dataset.index].text);
          event.dataTransfer.effectAllowed = "move";
        } catch (error) { /* the drop falls back to state.dragging */ }
      }
    });

    canvas.addEventListener("dragover", function (event) {
      event.preventDefault();
      var gap = nearestGap(event);
      canvas.querySelectorAll(".fxb-gap").forEach(function (g) { g.classList.toggle("over", g === gap); });
    });

    canvas.addEventListener("dragleave", function () {
      canvas.querySelectorAll(".fxb-gap").forEach(function (g) { g.classList.remove("over"); });
    });

    canvas.addEventListener("drop", function (event) {
      event.preventDefault();
      var gap = nearestGap(event);
      var at = gap ? +gap.dataset.gap : state.tokens.length;
      canvas.querySelectorAll(".fxb-gap").forEach(function (g) { g.classList.remove("over"); });
      if (state.dragging && state.dragging.from !== null) {
        moveToken(state.dragging.from, at);
      } else {
        var token = (state.dragging && state.dragging.token) || "";
        if (!token && event.dataTransfer) {
          try { token = event.dataTransfer.getData("text/plain"); } catch (error) { token = ""; }
        }
        if (token && state.dragging && state.dragging.preset) setExpr(token);
        else if (token) insert(token, at);
      }
      state.dragging = null;
    });

    canvas.addEventListener("dragend", function () {
      state.dragging = null;
      canvas.querySelectorAll(".fxb-gap").forEach(function (g) { g.classList.remove("over"); });
    });
    el("fxbPalette").addEventListener("dragend", function () { state.dragging = null; });

    el("fxbClear").addEventListener("click", function () { setExpr(""); state.caret = null; });

    el("fxbOps").addEventListener("click", function (event) {
      var button = event.target.closest("[data-op]");
      if (!button) return;
      insert(button.dataset.op, state.caret);
      state.caret = null;
    });

    el("fxbRaw").addEventListener("input", function () { setExpr(this.value, { fromRaw: true }); });

    el("fxbModeBuild").addEventListener("click", function () { setMode("build"); });
    el("fxbModeType").addEventListener("click", function () { setMode("type"); });

    el("fxbSamples").addEventListener("input", function (event) {
      var input = event.target.closest("[data-sample]");
      if (!input) return;
      state.overrides[input.dataset.sample] = input.value;
      validate();
    });

    modal.querySelectorAll("[data-bs-dismiss='modal']").forEach(function (button) {
      button.addEventListener("click", hideModal);
    });
    modal.addEventListener("mousedown", function (event) {
      if (event.target === modal) hideModal();          // click outside the card
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && modal && modal.classList.contains("show")) hideModal();
    });

    el("fxbSave").addEventListener("click", function () {
      var expr = state.expr;
      if (state.options.onSave) state.options.onSave(expr);
      hideModal();
    });
  }

  function nearestGap(event) {
    var gaps = [].slice.call(el("fxbCanvas").querySelectorAll(".fxb-gap"));
    if (!gaps.length) return null;
    var best = null, bestDistance = Infinity;
    gaps.forEach(function (gap) {
      var box = gap.getBoundingClientRect();
      var distance = Math.abs(event.clientX - (box.left + box.width / 2)) +
                     Math.abs(event.clientY - (box.top + box.height / 2)) * 2;
      if (distance < bestDistance) { bestDistance = distance; best = gap; }
    });
    return best;
  }

  function setMode(mode) {
    var build = mode === "build";
    el("fxbCanvas").classList.toggle("d-none", !build);
    el("fxbOps").classList.toggle("d-none", !build);
    el("fxbRaw").classList.toggle("d-none", build);
    el("fxbModeBuild").classList.toggle("active", build);
    el("fxbModeType").classList.toggle("active", !build);
    if (!build) el("fxbRaw").focus();
  }

  // The dialog opens and closes itself. It used to call bootstrap.Modal, which
  // meant that on any page where Bootstrap's JS had not loaded yet the click
  // threw and nothing at all happened — no dialog, no error the user could see.
  var backdrop = null;

  function showModal() {
    if (global.bootstrap && global.bootstrap.Modal) {
      global.bootstrap.Modal.getOrCreateInstance(modal).show();
      return;
    }
    backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop fade show";
    document.body.appendChild(backdrop);
    document.body.classList.add("modal-open");
    modal.classList.add("show");
    modal.style.display = "block";
    modal.removeAttribute("aria-hidden");
    modal.setAttribute("aria-modal", "true");
    var focusable = modal.querySelector("#fxbSearch");
    if (focusable) setTimeout(function () { focusable.focus(); }, 30);
  }

  function hideModal() {
    if (global.bootstrap && global.bootstrap.Modal) {
      var instance = global.bootstrap.Modal.getInstance(modal);
      if (instance) { instance.hide(); return; }
    }
    modal.classList.remove("show");
    modal.style.display = "none";
    modal.setAttribute("aria-hidden", "true");
    modal.removeAttribute("aria-modal");
    document.body.classList.remove("modal-open");
    if (backdrop && backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
    backdrop = null;
  }

  // ── entry point ──────────────────────────────────────────────────────────
  function open(options) {
    if (!global.StudioFormula) {
      /* eslint-disable no-alert */
      alert("The formula engine did not load. Run collectstatic and refresh the page.");
      return;
    }
    ensureModal();
    state = {
      options: options || {},
      expr: "",
      tokens: [],
      labels: {},
      overrides: {},
      caret: null,
      dragging: null
    };

    (state.options.fields || []).forEach(function (field) {
      state.labels[field.key] = field.label || field.key;
      if (field.type === "table") {
        (field.columns || []).forEach(function (c) {
          state.labels[field.key + "." + c.key] = (field.label || field.key) + " · " + (c.label || c.key);
        });
      }
    });
    if (state.options.scope === "row" && state.options.row) {
      (state.options.row.columns || []).forEach(function (c) {
        state.labels[c.key] = (c.label || c.key) + " (this row)";
      });
    }

    el("fxbTitle").textContent = options.title
      ? "Formula for \u201c" + options.title + "\u201d"
      : "Build a formula";
    el("fxbSearch").value = "";
    el("fxbOps").innerHTML = OPERATORS.map(function (op) {
      return '<button type="button" class="fxb-op" data-op="' + esc(op[1]) + '" title="' + esc(op[2]) + '">' +
             op[0] + "</button>";
    }).join("");

    setMode("build");
    renderPalette();
    setExpr(options.expr || "");
    showModal();
  }

  global.StudioFormulaBuilder = { open: open, lex: lex, explain: explain };
})(window);
