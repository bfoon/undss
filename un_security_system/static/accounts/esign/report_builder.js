/* UN PASS eSign Report Builder
 * No third-party chart dependency. Charts are rendered as responsive SVG so
 * the report remains usable behind strict CSP/proxies and on the UN network.
 */
(function (global) {
  "use strict";

  const TYPES = [
    ["card", "KPI card", "bi-123"],
    ["bar", "Bar chart", "bi-bar-chart"],
    ["line", "Line chart", "bi-graph-up"],
    ["area", "Area chart", "bi-graph-up-arrow"],
    ["pie", "Pie chart", "bi-pie-chart"],
    ["donut", "Donut chart", "bi-circle"],
    ["table", "Table", "bi-table"],
    ["matrix", "Matrix / pivot", "bi-grid-3x3-gap"],
    ["gauge", "Gauge", "bi-speedometer2"],
    ["funnel", "Funnel", "bi-filter"],
    ["scatter", "Scatter", "bi-dot"],
    ["slicer", "Slicer", "bi-funnel"],
    ["text", "Text box", "bi-fonts"]
  ];

  const AGGS = [
    ["count", "Count"],
    ["count_distinct", "Count distinct"],
    ["sum", "Sum"],
    ["avg", "Average"],
    ["min", "Minimum"],
    ["max", "Maximum"],
    ["percentage", "% of total"]
  ];

  const FILTER_OPS = [
    ["eq", "is"],
    ["neq", "is not"],
    ["contains", "contains"],
    ["not_contains", "does not contain"],
    ["gt", ">"],
    ["gte", ">="],
    ["lt", "<"],
    ["lte", "<="],
    ["between", "between"],
    ["is_blank", "is blank"],
    ["not_blank", "is not blank"]
  ];

  const PALETTE = ["#009EDB", "#005A8B", "#7C3AED", "#0E9F6E", "#EA580C", "#DC2626", "#0891B2", "#64748B", "#D97706", "#4F46E5"];

  function clone(value) { return JSON.parse(JSON.stringify(value || {})); }
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    }[c]));
  }
  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, Number(n) || 0)); }
  function uuid() { return "w_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 8); }
  function fmt(value, format) {
    format = format || {};
    const prefix = format.prefix || "", suffix = format.suffix || "";
    const decimals = Number.isFinite(Number(format.decimals)) ? Number(format.decimals) : 0;
    if (value === null || value === undefined || value === "") return "—";
    const n = Number(value);
    if (!Number.isNaN(n) && Number.isFinite(n)) {
      return prefix + n.toLocaleString(undefined, {minimumFractionDigits: decimals, maximumFractionDigits: decimals}) + suffix;
    }
    return prefix + String(value) + suffix;
  }
  function fieldIcon(kind) {
    if (kind === "number") return "bi-hash";
    if (kind === "date" || kind === "datetime") return "bi-calendar3";
    if (kind === "boolean") return "bi-toggle-on";
    if (kind === "category") return "bi-tags";
    return "bi-fonts";
  }

  function mount(options) {
    const root = options.root;
    if (!root) return;

    const mode = root.dataset.mode || "view";
    const design = mode === "design";
    const catalog = options.catalog || {datasets: []};
    const cfg = clone(options.config || {});
    cfg.widgets = Array.isArray(cfg.widgets) ? cfg.widgets : [];
    cfg.filters = Array.isArray(cfg.filters) ? cfg.filters : [];
    cfg.theme = cfg.theme || {accent:"#009EDB", background:"#F5F7FA", card_background:"#FFFFFF"};
    cfg.page = cfg.page || {title:"", subtitle:"", columns:12, row_height:82};

    const state = {
      cfg,
      selected: null,
      results: {},
      meta: {},
      crossFilters: [],
      dirty: false,
      undo: [],
      redo: [],
      querying: false,
      currentDataset: (catalog.datasets[0] || {}).id || "",
      reportName: options.name || "Report",
      description: options.description || "",
      shareScope: options.shareScope || "private"
    };

    const canvas = document.getElementById("rbCanvas");
    const datasetSelect = document.getElementById("rbDataset");
    const fieldsHost = document.getElementById("rbFields");
    const inspector = document.getElementById("rbInspector");
    const filtersHost = document.getElementById("rbFilters");
    const cross = document.getElementById("rbCross");
    const crossText = document.getElementById("rbCrossText");
    let controller = null;
    let queryTimer = null;

    function dataset(id) { return catalog.datasets.find(d => d.id === id) || catalog.datasets[0] || null; }
    function fields(id) { const d = dataset(id); return d ? d.fields || [] : []; }
    function field(id, fid) { return fields(id).find(f => f.id === fid) || null; }
    function fieldLabel(id, fid) {
      if (fid === "__rows__") return "Records";
      const f = field(id, fid);
      return f ? f.label : (fid || "—");
    }
    function defaultDimension(dsId) {
      return fields(dsId).find(f => ["category","text","date","datetime","boolean"].includes(f.kind)) || fields(dsId)[0] || null;
    }
    function defaultMeasure(dsId) {
      return fields(dsId).find(f => f.kind === "number") || {id:"__rows__", label:"Records", kind:"number"};
    }

    function snapshot() {
      return {
        cfg: clone(state.cfg),
        reportName: state.reportName,
        description: state.description,
        shareScope: state.shareScope
      };
    }
    function restore(s) {
      state.cfg = clone(s.cfg);
      state.reportName = s.reportName;
      state.description = s.description;
      state.shareScope = s.shareScope;
      state.selected = null;
      state.dirty = true;
      syncName();
      renderAll();
      querySoon();
    }
    function beforeChange() {
      state.undo.push(snapshot());
      if (state.undo.length > 80) state.undo.shift();
      state.redo = [];
    }
    function changed(noQuery) {
      state.dirty = true;
      const saveState = document.getElementById("rbSaveState");
      if (saveState) saveState.textContent = "Unsaved changes";
      syncHistory();
      if (!noQuery) querySoon();
    }
    function syncHistory() {
      const u = document.getElementById("rbUndo"), r = document.getElementById("rbRedo");
      if (u) u.disabled = !state.undo.length;
      if (r) r.disabled = !state.redo.length;
    }
    function syncName() {
      const input = document.getElementById("rbReportName");
      if (input && input.value !== state.reportName) input.value = state.reportName;
    }

    function reportHeight() {
      let max = 8;
      state.cfg.widgets.forEach(w => {
        const l = w.layout || {};
        max = Math.max(max, Number(l.y || 0) + Number(l.h || 3));
      });
      return max * 82 + 20;
    }
    function place(el, layout) {
      const x = clamp(layout.x || 0, 0, 11);
      const y = clamp(layout.y || 0, 0, 500);
      const w = clamp(layout.w || 4, 1, 12 - x);
      const h = clamp(layout.h || 3, 1, 10);
      el.style.left = `calc(${x / 12 * 100}% + 5px)`;
      el.style.width = `calc(${w / 12 * 100}% - 10px)`;
      el.style.top = `${y * 82 + 5}px`;
      el.style.height = `${h * 82 - 10}px`;
    }

    function renderCanvas() {
      if (!canvas) return;
      canvas.style.backgroundColor = state.cfg.theme.background || "#F5F7FA";
      canvas.style.minHeight = reportHeight() + "px";
      canvas.innerHTML = "";

      state.cfg.widgets.forEach(widget => {
        const el = document.createElement("section");
        el.className = "rb-widget" + (state.selected === widget.id ? " selected" : "");
        el.dataset.id = widget.id;
        el.style.background = state.cfg.theme.card_background || "#FFFFFF";
        place(el, widget.layout || {});
        el.innerHTML = `
          <div class="rb-widget-head">
            <span class="title">${esc(widget.title || visualName(widget.type))}</span>
            ${design ? '<button type="button" class="btn btn-sm btn-link py-0 px-1 rb-widget-more" title="Configure"><i class="bi bi-sliders"></i></button>' : ''}
          </div>
          <div class="rb-widget-body"><div class="rb-widget-loading"><span class="spinner-border spinner-border-sm me-2"></span>Loading…</div></div>
          ${design ? '<span class="rb-resize" title="Resize"></span>' : ''}
        `;
        canvas.appendChild(el);

        el.addEventListener("click", e => {
          if (!design) return;
          if (e.target.closest(".rb-resize")) return;
          state.selected = widget.id;
          renderCanvas();
          renderInspector();
          paintResults();
        });

        if (design) {
          wireMove(el, widget);
          wireResize(el, widget);
        }
      });
      paintResults();
    }

    function wireMove(el, widget) {
      const head = el.querySelector(".rb-widget-head");
      head.addEventListener("pointerdown", e => {
        if (e.target.closest("button")) return;
        e.preventDefault();
        state.selected = widget.id;
        beforeChange();
        const rect = canvas.getBoundingClientRect();
        const startX = e.clientX, startY = e.clientY;
        const start = clone(widget.layout || {x:0,y:0,w:4,h:3});
        head.setPointerCapture(e.pointerId);

        function move(ev) {
          const cellW = rect.width / 12;
          const dx = Math.round((ev.clientX - startX) / cellW);
          const dy = Math.round((ev.clientY - startY) / 82);
          widget.layout.x = clamp(start.x + dx, 0, 12 - widget.layout.w);
          widget.layout.y = clamp(start.y + dy, 0, 500);
          place(el, widget.layout);
          canvas.style.minHeight = reportHeight() + "px";
        }
        function up() {
          head.removeEventListener("pointermove", move);
          head.removeEventListener("pointerup", up);
          changed(true);
          renderInspector();
        }
        head.addEventListener("pointermove", move);
        head.addEventListener("pointerup", up);
      });
    }

    function wireResize(el, widget) {
      const handle = el.querySelector(".rb-resize");
      handle.addEventListener("pointerdown", e => {
        e.preventDefault();
        e.stopPropagation();
        beforeChange();
        const rect = canvas.getBoundingClientRect();
        const startX = e.clientX, startY = e.clientY;
        const start = clone(widget.layout || {x:0,y:0,w:4,h:3});
        handle.setPointerCapture(e.pointerId);

        function move(ev) {
          const cellW = rect.width / 12;
          const dw = Math.round((ev.clientX - startX) / cellW);
          const dh = Math.round((ev.clientY - startY) / 82);
          widget.layout.w = clamp(start.w + dw, 1, 12 - widget.layout.x);
          widget.layout.h = clamp(start.h + dh, 1, 10);
          place(el, widget.layout);
          canvas.style.minHeight = reportHeight() + "px";
        }
        function up() {
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", up);
          changed(true);
          querySoon();
        }
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", up);
      });
    }

    function visualName(type) {
      const item = TYPES.find(t => t[0] === type);
      return item ? item[1] : "Visual";
    }

    function renderDataPane() {
      if (!design || !datasetSelect || !fieldsHost) return;
      datasetSelect.innerHTML = catalog.datasets.map(d =>
        `<option value="${esc(d.id)}"${d.id === state.currentDataset ? " selected" : ""}>${esc(d.name)}</option>`
      ).join("");
      datasetSelect.onchange = () => {
        state.currentDataset = datasetSelect.value;
        renderDataPane();
      };

      const ds = dataset(state.currentDataset);
      if (!ds) { fieldsHost.innerHTML = ""; return; }
      fieldsHost.innerHTML = `<div class="rb-dataset-title">${esc(ds.name)}</div>` +
        (ds.fields || []).map(f => `
          <div class="rb-field" draggable="true" data-dataset="${esc(ds.id)}" data-field="${esc(f.id)}" title="Drag into Category, Values, Series or Columns">
            <i class="bi ${fieldIcon(f.kind)}"></i>
            <span class="text-truncate">${esc(f.label)}</span>
            <span class="kind">${esc(f.kind)}</span>
          </div>
        `).join("");

      fieldsHost.querySelectorAll(".rb-field").forEach(node => {
        node.addEventListener("dragstart", e => {
          e.dataTransfer.setData("application/json", JSON.stringify({
            dataset: node.dataset.dataset,
            field: node.dataset.field
          }));
          e.dataTransfer.effectAllowed = "copy";
        });
      });
    }

    function optionsForFields(dsId, selected, includeRows, kind) {
      let opts = [];
      if (includeRows) opts.push(`<option value="__rows__"${selected === "__rows__" ? " selected" : ""}>Records</option>`);
      fields(dsId).forEach(f => {
        if (kind && f.kind !== kind) return;
        opts.push(`<option value="${esc(f.id)}"${selected === f.id ? " selected" : ""}>${esc(f.label)}</option>`);
      });
      return opts.join("");
    }

    function roleBox(label, role, widget, fieldId) {
      return `
        <div class="rb-role" data-role="${esc(role)}">
          <div class="rb-role-label">${esc(label)}</div>
          <div class="rb-role-value">${esc(fieldLabel(widget.dataset, fieldId) || "Drop a field here")}</div>
        </div>
      `;
    }

    function renderInspector() {
      if (!design || !inspector) return;
      const widget = state.cfg.widgets.find(w => w.id === state.selected);
      if (!widget) {
        inspector.innerHTML = `
          <div class="rb-inspector-section">
            <div class="small fw-semibold mb-1">Report settings</div>
            <label class="small">Description</label>
            <textarea class="form-control form-control-sm mb-2" id="rbDescription" rows="3">${esc(state.description)}</textarea>
            <label class="small">Shared with</label>
            <select class="form-select form-select-sm mb-2" id="rbShare">
              <option value="private"${state.shareScope === "private" ? " selected" : ""}>Only me</option>
              <option value="office"${state.shareScope === "office" ? " selected" : ""}>My country office</option>
              <option value="agency"${state.shareScope === "agency" ? " selected" : ""}>My whole agency</option>
            </select>
            <label class="small">Page title</label>
            <input class="form-control form-control-sm mb-2" id="rbPageTitle" value="${esc(state.cfg.page.title || "")}">
            <label class="small">Subtitle</label>
            <input class="form-control form-control-sm" id="rbPageSubtitle" value="${esc(state.cfg.page.subtitle || "")}">
          </div>
          <div class="small text-muted">Select a visual on the canvas to configure its data and formatting.</div>
        `;
        bindReportSettings();
        return;
      }

      const dsId = widget.dataset || state.currentDataset || (catalog.datasets[0] || {}).id || "";
      widget.dataset = dsId;
      const dim = (widget.dimension || {}).field || "";
      const measure = widget.measure || {field:"__rows__", agg:"count"};
      const series = (widget.series || {}).field || "";
      const format = widget.format || {};
      const type = widget.type || "card";

      let body = `
        <div class="rb-inspector-section">
          <label class="small fw-semibold">Title</label>
          <input class="form-control form-control-sm mb-2" data-bind="title" value="${esc(widget.title || "")}">
          <label class="small fw-semibold">Visual type</label>
          <select class="form-select form-select-sm mb-2" data-bind="type">
            ${TYPES.map(t => `<option value="${t[0]}"${t[0] === type ? " selected" : ""}>${t[1]}</option>`).join("")}
          </select>
          <label class="small fw-semibold">Dataset</label>
          <select class="form-select form-select-sm" data-bind="dataset">
            ${catalog.datasets.map(d => `<option value="${esc(d.id)}"${d.id === dsId ? " selected" : ""}>${esc(d.name)}</option>`).join("")}
          </select>
        </div>
      `;

      if (type === "text") {
        body += `
          <div class="rb-inspector-section">
            <label class="small fw-semibold">Text</label>
            <textarea class="form-control form-control-sm" rows="7" data-bind="text">${esc(widget.text || "")}</textarea>
          </div>`;
      } else if (type === "table") {
        body += `
          <div class="rb-inspector-section">
            ${roleBox("Columns", "columns", widget, (widget.columns || [])[0])}
            <label class="small fw-semibold">Columns</label>
            <select class="form-select form-select-sm" multiple size="8" data-bind="columns">
              ${fields(dsId).map(f => `<option value="${esc(f.id)}"${(widget.columns || []).includes(f.id) ? " selected" : ""}>${esc(f.label)}</option>`).join("")}
            </select>
            <label class="small mt-2">Maximum rows</label>
            <input type="number" min="1" max="500" class="form-control form-control-sm" data-bind="limit" value="${esc(widget.limit || 100)}">
          </div>`;
      } else if (type === "matrix") {
        body += `
          <div class="rb-inspector-section">
            ${roleBox("Rows", "matrix_row", widget, (widget.matrix_row || {}).field)}
            <select class="form-select form-select-sm mb-2" data-bind="matrix_row">${optionsForFields(dsId, (widget.matrix_row || {}).field, false)}</select>
            ${roleBox("Columns", "matrix_col", widget, (widget.matrix_col || {}).field)}
            <select class="form-select form-select-sm mb-2" data-bind="matrix_col">${optionsForFields(dsId, (widget.matrix_col || {}).field, false)}</select>
            ${roleBox("Values", "measure", widget, measure.field)}
            <select class="form-select form-select-sm mb-2" data-bind="measure_field">${optionsForFields(dsId, measure.field, true)}</select>
            <select class="form-select form-select-sm" data-bind="measure_agg">${AGGS.map(a => `<option value="${a[0]}"${a[0] === measure.agg ? " selected" : ""}>${a[1]}</option>`).join("")}</select>
          </div>`;
      } else if (type === "scatter") {
        body += `
          <div class="rb-inspector-section">
            ${roleBox("X axis", "x", widget, (widget.x || {}).field)}
            <select class="form-select form-select-sm mb-2" data-bind="x_field">${optionsForFields(dsId, (widget.x || {}).field, false, "number")}</select>
            ${roleBox("Y axis", "y", widget, (widget.y || {}).field)}
            <select class="form-select form-select-sm" data-bind="y_field">${optionsForFields(dsId, (widget.y || {}).field, false, "number")}</select>
          </div>`;
      } else if (type === "slicer") {
        body += `
          <div class="rb-inspector-section">
            ${roleBox("Field", "dimension", widget, dim)}
            <select class="form-select form-select-sm" data-bind="dimension_field">${optionsForFields(dsId, dim, false)}</select>
          </div>`;
      } else if (type === "card" || type === "gauge") {
        body += `
          <div class="rb-inspector-section">
            ${roleBox("Values", "measure", widget, measure.field)}
            <label class="small">Field</label>
            <select class="form-select form-select-sm mb-2" data-bind="measure_field">${optionsForFields(dsId, measure.field, true)}</select>
            <label class="small">Aggregation</label>
            <select class="form-select form-select-sm" data-bind="measure_agg">${AGGS.filter(a => a[0] !== "percentage").map(a => `<option value="${a[0]}"${a[0] === measure.agg ? " selected" : ""}>${a[1]}</option>`).join("")}</select>
            ${type === "gauge" ? `
              <div class="row g-2 mt-1">
                <div class="col-6"><label class="small">Minimum</label><input class="form-control form-control-sm" type="number" data-bind="minimum" value="${esc(widget.minimum == null ? 0 : widget.minimum)}"></div>
                <div class="col-6"><label class="small">Maximum</label><input class="form-control form-control-sm" type="number" data-bind="maximum" value="${esc(widget.maximum == null ? 100 : widget.maximum)}"></div>
              </div>
              <label class="small mt-1">Target</label><input class="form-control form-control-sm" type="number" data-bind="target" value="${esc(widget.target == null ? "" : widget.target)}">
            ` : ""}
          </div>`;
      } else {
        body += `
          <div class="rb-inspector-section">
            ${roleBox("Category / X axis", "dimension", widget, dim)}
            <select class="form-select form-select-sm mb-2" data-bind="dimension_field">${optionsForFields(dsId, dim, false)}</select>
            <div class="row g-2 mb-2">
              <div class="col-6">
                <label class="small">Date grouping</label>
                <select class="form-select form-select-sm" data-bind="dimension_bin">
                  ${["","day","week","month","quarter","year"].map(x => `<option value="${x}"${x === ((widget.dimension || {}).bin || "") ? " selected" : ""}>${x ? x[0].toUpperCase()+x.slice(1) : "None"}</option>`).join("")}
                </select>
              </div>
              <div class="col-6">
                <label class="small">Top N</label>
                <input class="form-control form-control-sm" type="number" min="1" max="200" data-bind="top_n" value="${esc(widget.top_n || 30)}">
              </div>
            </div>
            ${roleBox("Values", "measure", widget, measure.field)}
            <select class="form-select form-select-sm mb-2" data-bind="measure_field">${optionsForFields(dsId, measure.field, true)}</select>
            <select class="form-select form-select-sm mb-2" data-bind="measure_agg">${AGGS.map(a => `<option value="${a[0]}"${a[0] === measure.agg ? " selected" : ""}>${a[1]}</option>`).join("")}</select>
            ${["bar","line","area"].includes(type) ? `
              ${roleBox("Legend / Series", "series", widget, series)}
              <select class="form-select form-select-sm" data-bind="series_field">
                <option value="">No series</option>${optionsForFields(dsId, series, false)}
              </select>
            ` : ""}
          </div>`;
      }

      if (type !== "text" && type !== "slicer") {
        body += `
          <div class="rb-inspector-section">
            <div class="small fw-semibold mb-1">Number format</div>
            <div class="row g-2">
              <div class="col-4"><label class="small">Prefix</label><input class="form-control form-control-sm" data-bind="format_prefix" value="${esc(format.prefix || "")}"></div>
              <div class="col-4"><label class="small">Suffix</label><input class="form-control form-control-sm" data-bind="format_suffix" value="${esc(format.suffix || "")}"></div>
              <div class="col-4"><label class="small">Decimals</label><input type="number" min="0" max="6" class="form-control form-control-sm" data-bind="format_decimals" value="${esc(format.decimals == null ? 0 : format.decimals)}"></div>
            </div>
          </div>`;
      }

      body += `
        <button class="btn btn-sm btn-outline-danger w-100" id="rbDeleteVisual"><i class="bi bi-trash me-1"></i>Remove visual</button>
      `;
      inspector.innerHTML = body;
      bindInspector(widget);
    }

    function bindReportSettings() {
      const desc = document.getElementById("rbDescription");
      const share = document.getElementById("rbShare");
      const title = document.getElementById("rbPageTitle");
      const subtitle = document.getElementById("rbPageSubtitle");
      [desc, share, title, subtitle].forEach(el => {
        if (!el) return;
        el.addEventListener("change", () => {
          beforeChange();
          state.description = desc.value;
          state.shareScope = share.value;
          state.cfg.page.title = title.value;
          state.cfg.page.subtitle = subtitle.value;
          changed(true);
        });
      });
    }

    function bindInspector(widget) {
      inspector.querySelectorAll("[data-bind]").forEach(input => {
        input.addEventListener("change", () => {
          beforeChange();
          const key = input.dataset.bind;
          let value = input.value;

          if (key === "title") widget.title = value;
          else if (key === "type") {
            widget.type = value;
            prepareWidget(widget);
          }
          else if (key === "dataset") {
            widget.dataset = value;
            resetRoles(widget);
            state.currentDataset = value;
          }
          else if (key === "text") widget.text = value;
          else if (key === "columns") widget.columns = [...input.selectedOptions].map(o => o.value);
          else if (key === "limit") widget.limit = clamp(value, 1, 500);
          else if (key === "dimension_field") {
            widget.dimension = widget.dimension || {};
            widget.dimension.field = value;
          }
          else if (key === "dimension_bin") {
            widget.dimension = widget.dimension || {};
            widget.dimension.bin = value;
          }
          else if (key === "series_field") {
            widget.series = widget.series || {};
            widget.series.field = value;
          }
          else if (key === "measure_field") {
            widget.measure = widget.measure || {};
            widget.measure.field = value;
          }
          else if (key === "measure_agg") {
            widget.measure = widget.measure || {};
            widget.measure.agg = value;
          }
          else if (key === "matrix_row") widget.matrix_row = {field:value};
          else if (key === "matrix_col") widget.matrix_col = {field:value};
          else if (key === "x_field") widget.x = {field:value};
          else if (key === "y_field") widget.y = {field:value};
          else if (["minimum","maximum","target","top_n"].includes(key)) widget[key] = value === "" ? null : Number(value);
          else if (key.startsWith("format_")) {
            widget.format = widget.format || {};
            const k = key.replace("format_", "");
            widget.format[k] = k === "decimals" ? clamp(value, 0, 6) : value;
          }

          changed();
          renderAll();
        });
        if (input.tagName === "TEXTAREA" || input.type === "text") {
          input.addEventListener("input", () => {
            if (input.dataset.bind === "title") {
              widget.title = input.value;
              state.dirty = true;
              renderCanvas();
            } else if (input.dataset.bind === "text") {
              widget.text = input.value;
              state.dirty = true;
              renderCanvas();
              paintResults();
            }
          });
        }
      });

      inspector.querySelectorAll(".rb-role").forEach(zone => {
        zone.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("over"); });
        zone.addEventListener("dragleave", () => zone.classList.remove("over"));
        zone.addEventListener("drop", e => {
          e.preventDefault();
          zone.classList.remove("over");
          let payload;
          try { payload = JSON.parse(e.dataTransfer.getData("application/json")); } catch (_) { return; }
          if (!payload || !payload.field) return;
          beforeChange();
          if (payload.dataset && payload.dataset !== widget.dataset) {
            widget.dataset = payload.dataset;
            resetRoles(widget);
          }
          assignRole(widget, zone.dataset.role, payload.field);
          changed();
          state.currentDataset = widget.dataset;
          renderAll();
        });
      });

      const del = document.getElementById("rbDeleteVisual");
      if (del) del.onclick = () => {
        beforeChange();
        state.cfg.widgets = state.cfg.widgets.filter(w => w.id !== widget.id);
        state.selected = null;
        changed();
        renderAll();
      };
    }

    function assignRole(widget, role, fid) {
      if (role === "dimension") widget.dimension = {field:fid};
      else if (role === "measure") {
        widget.measure = widget.measure || {agg:"sum"};
        widget.measure.field = fid;
        if (fid === "__rows__") widget.measure.agg = "count";
      }
      else if (role === "series") widget.series = {field:fid};
      else if (role === "matrix_row") widget.matrix_row = {field:fid};
      else if (role === "matrix_col") widget.matrix_col = {field:fid};
      else if (role === "x") widget.x = {field:fid};
      else if (role === "y") widget.y = {field:fid};
      else if (role === "columns") {
        widget.columns = widget.columns || [];
        if (!widget.columns.includes(fid)) widget.columns.push(fid);
      }
    }

    function resetRoles(widget) {
      const dsId = widget.dataset;
      const dim = defaultDimension(dsId);
      const measure = defaultMeasure(dsId);
      widget.dimension = dim ? {field:dim.id} : {};
      widget.measure = {field:measure.id, agg:measure.id === "__rows__" ? "count" : "sum"};
      widget.series = {};
      widget.columns = fields(dsId).slice(0, 6).map(f => f.id);
      const nums = fields(dsId).filter(f => f.kind === "number");
      widget.x = nums[0] ? {field:nums[0].id} : {};
      widget.y = nums[1] ? {field:nums[1].id} : nums[0] ? {field:nums[0].id} : {};
      widget.matrix_row = dim ? {field:dim.id} : {};
      widget.matrix_col = fields(dsId).find(f => f.id !== (dim || {}).id && f.kind !== "number")
        ? {field:fields(dsId).find(f => f.id !== (dim || {}).id && f.kind !== "number").id}
        : {};
    }

    function prepareWidget(widget) {
      widget.layout = widget.layout || {x:0,y:0,w:4,h:3};
      widget.format = widget.format || {decimals:0};
      if (!widget.dataset) widget.dataset = state.currentDataset;
      if (!widget.measure || !widget.dimension) resetRoles(widget);
      if (widget.type === "table" && !(widget.columns || []).length) widget.columns = fields(widget.dataset).slice(0,6).map(f=>f.id);
      if (widget.type === "text") widget.text = widget.text || "Add text here.";
      if (widget.type === "gauge") {
        if (widget.minimum == null) widget.minimum = 0;
        if (widget.maximum == null) widget.maximum = 100;
      }
    }

    function addWidget(type) {
      beforeChange();
      const maxY = state.cfg.widgets.reduce((m,w) => Math.max(m, Number((w.layout||{}).y||0)+Number((w.layout||{}).h||3)), 0);
      const dsId = state.currentDataset || (catalog.datasets[0] || {}).id || "";
      const widget = {
        id: uuid(), type, title: visualName(type), dataset: dsId,
        layout: {x:0, y:maxY, w:type === "card" ? 3 : type === "table" ? 8 : 5, h:type === "card" ? 2 : 4},
        format:{decimals:0}
      };
      resetRoles(widget);
      prepareWidget(widget);
      state.cfg.widgets.push(widget);
      state.selected = widget.id;
      changed();
      renderAll();
      setTimeout(() => {
        const el = canvas.querySelector(`[data-id="${CSS.escape(widget.id)}"]`);
        if (el) el.scrollIntoView({behavior:"smooth", block:"center"});
      }, 20);
    }

    function renderAddMenu() {
      const host = document.getElementById("rbAddVisual");
      if (!host) return;
      host.classList.add("rb-visual-menu");
      host.innerHTML = TYPES.map(t => `
        <li><button class="dropdown-item" type="button" data-visual="${t[0]}"><i class="bi ${t[2]}"></i><span>${t[1]}</span></button></li>
      `).join("");
      host.querySelectorAll("[data-visual]").forEach(b => b.onclick = () => addWidget(b.dataset.visual));
    }

    function renderFilters() {
      if (design) {
        if (!filtersHost) return;
        filtersHost.innerHTML = state.cfg.filters.length ? "" : '<div class="small text-muted">No report filters.</div>';
        state.cfg.filters.forEach((flt, index) => {
          const dsId = flt.dataset || state.currentDataset || (catalog.datasets[0]||{}).id;
          const row = document.createElement("div");
          row.className = "rb-filter-row";
          row.innerHTML = `
            <div class="d-flex gap-1 mb-1">
              <select class="form-select" data-f="dataset">${catalog.datasets.map(d=>`<option value="${esc(d.id)}"${d.id===dsId?" selected":""}>${esc(d.name)}</option>`).join("")}</select>
              <button class="btn btn-sm btn-link text-danger px-1" data-remove><i class="bi bi-x-lg"></i></button>
            </div>
            <select class="form-select mb-1" data-f="field">${optionsForFields(dsId, flt.field, false)}</select>
            <div class="d-flex gap-1">
              <select class="form-select" data-f="op">${FILTER_OPS.map(op=>`<option value="${op[0]}"${op[0]===(flt.op||"eq")?" selected":""}>${op[1]}</option>`).join("")}</select>
              <input class="form-control" data-f="value" value="${esc(flt.value == null ? "" : flt.value)}" placeholder="Value">
            </div>
          `;
          filtersHost.appendChild(row);

          row.querySelector("[data-remove]").onclick = () => {
            beforeChange(); state.cfg.filters.splice(index,1); changed(); renderFilters();
          };
          row.querySelectorAll("[data-f]").forEach(control => {
            control.onchange = () => {
              beforeChange();
              const k = control.dataset.f;
              if (k === "dataset") {
                flt.dataset = control.value;
                flt.field = (fields(control.value)[0] || {}).id || "";
                renderFilters();
              } else {
                flt[k] = control.value;
              }
              changed();
            };
          });
        });
      } else {
        const viewHost = document.getElementById("rbViewFilters");
        if (!viewHost) return;
        const arr = state.cfg.filters || [];
        viewHost.innerHTML = arr.length ? arr.map(f => {
          const ds = dataset(f.dataset);
          return `<span class="rb-filter-pill">${esc(ds ? ds.name : "")}: ${esc(fieldLabel(f.dataset, f.field))} ${esc(f.op || "eq")} ${esc(f.value == null ? "" : f.value)}</span>`;
        }).join("") : '<span class="small text-muted">No fixed filters</span>';
      }
    }

    function addFilter() {
      const dsId = state.currentDataset || (catalog.datasets[0] || {}).id;
      const f = (fields(dsId)[0] || {}).id || "";
      beforeChange();
      state.cfg.filters.push({dataset:dsId, field:f, op:"eq", value:""});
      changed();
      renderFilters();
    }

    async function queryNow() {
      clearTimeout(queryTimer);
      if (!root.dataset.queryUrl || !state.cfg.widgets.length) {
        state.results = {};
        paintResults();
        return;
      }
      if (controller) controller.abort();
      controller = new AbortController();
      state.querying = true;
      canvas && canvas.querySelectorAll(".rb-widget-body").forEach(body => {
        body.innerHTML = '<div class="rb-widget-loading"><span class="spinner-border spinner-border-sm me-2"></span>Loading…</div>';
      });

      try {
        const response = await fetch(root.dataset.queryUrl, {
          method:"POST",
          credentials:"same-origin",
          cache:"no-store",
          headers:{
            "Content-Type":"application/json",
            "Accept":"application/json",
            "X-CSRFToken": options.csrf || ""
          },
          body:JSON.stringify({widgets:state.cfg.widgets, filters:state.crossFilters}),
          signal:controller.signal
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "The report query failed.");
        state.results = data.results || {};
        state.meta = data.datasets || {};
        paintResults();
      } catch (err) {
        if (err.name === "AbortError") return;
        canvas && canvas.querySelectorAll(".rb-widget-body").forEach(body => {
          body.innerHTML = `<div class="rb-widget-error">${esc(err.message || "The report could not be loaded.")}</div>`;
        });
      } finally {
        state.querying = false;
      }
    }
    function querySoon() {
      clearTimeout(queryTimer);
      queryTimer = setTimeout(queryNow, 180);
    }

    function paintResults() {
      if (!canvas) return;
      state.cfg.widgets.forEach(widget => {
        const el = canvas.querySelector(`[data-id="${CSS.escape(widget.id)}"]`);
        if (!el) return;
        const body = el.querySelector(".rb-widget-body");
        const result = state.results[widget.id];
        if (!result) {
          if (widget.type === "text") renderText(body, widget, {kind:"text",text:widget.text||""});
          return;
        }
        body.innerHTML = "";
        try { renderVisual(body, widget, result); }
        catch (err) { body.innerHTML = `<div class="rb-widget-error">${esc(err.message)}</div>`; }

        const meta = state.meta[widget.dataset];
        if (meta && meta.truncated) {
          const w = document.createElement("span");
          w.className = "rb-warning";
          w.title = "The source exceeded the configured report row limit.";
          w.textContent = "Row limit";
          body.appendChild(w);
        }
      });
    }

    function renderVisual(body, widget, result) {
      if (widget.type === "card") return renderCard(body, widget, result);
      if (widget.type === "gauge") return renderGauge(body, widget, result);
      if (widget.type === "table") return renderTable(body, widget, result);
      if (widget.type === "matrix") return renderMatrix(body, widget, result);
      if (widget.type === "scatter") return renderScatter(body, widget, result);
      if (widget.type === "slicer") return renderSlicer(body, widget, result);
      if (widget.type === "text") return renderText(body, widget, result);
      if (widget.type === "pie" || widget.type === "donut") return renderPie(body, widget, result);
      if (widget.type === "funnel") return renderFunnel(body, widget, result);
      if (widget.type === "line" || widget.type === "area") return renderLine(body, widget, result);
      return renderBar(body, widget, result);
    }

    function renderCard(body, widget, result) {
      body.innerHTML = `<div class="rb-kpi"><div class="rb-kpi-value">${esc(fmt(result.value, widget.format))}</div><div class="rb-kpi-sub">${result.filtered_rows == null ? "" : esc(result.filtered_rows + " record" + (result.filtered_rows === 1 ? "" : "s"))}</div></div>`;
    }

    function svgRoot() {
      const ns = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(ns, "svg");
      svg.setAttribute("viewBox", "0 0 640 300");
      svg.setAttribute("preserveAspectRatio", "none");
      svg.classList.add("rb-svg");
      return svg;
    }
    function sEl(name, attrs) {
      const el = document.createElementNS("http://www.w3.org/2000/svg", name);
      Object.entries(attrs || {}).forEach(([k,v]) => el.setAttribute(k, v));
      return el;
    }
    function tooltip(textValue, ev) {
      let tip = document.querySelector(".rb-tooltip");
      if (!tip) { tip = document.createElement("div"); tip.className="rb-tooltip"; document.body.appendChild(tip); }
      tip.textContent = textValue;
      tip.style.left = (ev.clientX + 12) + "px";
      tip.style.top = (ev.clientY + 12) + "px";
      tip.style.display = "block";
    }
    function hideTip() { const t=document.querySelector(".rb-tooltip"); if(t)t.style.display="none"; }

    function onCategory(widget, result, datum) {
      if (!result.dimension_field || (widget.dimension || {}).bin) return;
      state.crossFilters = [{
        dataset:widget.dataset,
        field:result.dimension_field,
        op:"eq",
        value:datum.filter_value
      }];
      showCross(`${fieldLabel(widget.dataset, result.dimension_field)} = ${datum.filter_value}`);
      queryNow();
    }

    function showCross(label) {
      if (!cross) return;
      cross.classList.remove("d-none");
      if (crossText) crossText.textContent = label;
    }
    function clearCross() {
      state.crossFilters = [];
      if (cross) cross.classList.add("d-none");
      queryNow();
    }

    function seriesMatrix(result) {
      const labels = [...new Set((result.data || []).map(d => String(d.label)))];
      const series = result.series_names && result.series_names.length ? result.series_names : [""];
      const map = new Map();
      (result.data || []).forEach(d => map.set(String(d.label) + "\u241f" + String(d.series || ""), d));
      return {labels, series, map};
    }

    function renderBar(body, widget, result) {
      const data = result.data || [];
      if (!data.length) { body.innerHTML='<div class="rb-empty">No data for this visual.</div>'; return; }
      const svg = svgRoot(), W=640,H=300, left=48,right=12,top=12,bottom=55;
      const plotW=W-left-right, plotH=H-top-bottom;
      const mat=seriesMatrix(result);
      const max=Math.max(1,...data.map(d=>Number(d.value)||0));
      for(let g=0;g<=4;g++){
        const y=top+plotH*g/4;
        svg.appendChild(sEl("line",{x1:left,y1:y,x2:W-right,y2:y,class:"rb-chart-grid"}));
        const tx=sEl("text",{x:left-6,y:y+3,"text-anchor":"end",class:"rb-chart-label"});tx.textContent=fmt(max*(4-g)/4,widget.format);svg.appendChild(tx);
      }
      const groupW=plotW/Math.max(1,mat.labels.length);
      const barW=Math.max(3,(groupW*.72)/mat.series.length);
      mat.labels.forEach((label,i)=>{
        mat.series.forEach((sn,j)=>{
          const d=mat.map.get(label+"\u241f"+sn); if(!d)return;
          const v=Number(d.value)||0;
          const bh=plotH*v/max;
          const rect=sEl("rect",{x:left+i*groupW+groupW*.14+j*barW,y:top+plotH-bh,width:Math.max(2,barW-2),height:bh,rx:2,fill:PALETTE[j%PALETTE.length],class:"rb-chart-bar"});
          rect.addEventListener("mousemove",ev=>tooltip(`${label}${sn?" · "+sn:""}: ${fmt(v,widget.format)}`,ev));
          rect.addEventListener("mouseleave",hideTip);
          rect.addEventListener("click",()=>onCategory(widget,result,d));
          svg.appendChild(rect);
        });
        const tx=sEl("text",{x:left+i*groupW+groupW/2,y:H-30,"text-anchor":"middle",class:"rb-chart-label"});
        tx.textContent=label.length>16?label.slice(0,15)+"…":label; svg.appendChild(tx);
      });
      body.appendChild(svg);
      if(mat.series[0]) body.appendChild(legend(mat.series));
    }

    function renderLine(body, widget, result) {
      const data=result.data||[];
      if(!data.length){body.innerHTML='<div class="rb-empty">No data for this visual.</div>';return;}
      const svg=svgRoot(),W=640,H=300,left=48,right=12,top=12,bottom=52,plotW=W-left-right,plotH=H-top-bottom;
      const mat=seriesMatrix(result), max=Math.max(1,...data.map(d=>Number(d.value)||0));
      for(let g=0;g<=4;g++){
        const y=top+plotH*g/4;svg.appendChild(sEl("line",{x1:left,y1:y,x2:W-right,y2:y,class:"rb-chart-grid"}));
        const tx=sEl("text",{x:left-6,y:y+3,"text-anchor":"end",class:"rb-chart-label"});tx.textContent=fmt(max*(4-g)/4,widget.format);svg.appendChild(tx);
      }
      mat.series.forEach((sn,j)=>{
        const points=[];
        mat.labels.forEach((label,i)=>{
          const d=mat.map.get(label+"\u241f"+sn); if(!d)return;
          const x=left+(mat.labels.length===1?plotW/2:i*plotW/(mat.labels.length-1));
          const y=top+plotH-(Number(d.value)||0)/max*plotH;
          points.push([x,y,d,label]);
        });
        if(!points.length)return;
        if(widget.type==="area"){
          let path=`M ${points[0][0]} ${top+plotH} `+points.map(p=>`L ${p[0]} ${p[1]}`).join(" ")+` L ${points[points.length-1][0]} ${top+plotH} Z`;
          svg.appendChild(sEl("path",{d:path,fill:PALETTE[j%PALETTE.length],opacity:.14}));
        }
        const poly=sEl("polyline",{points:points.map(p=>p[0]+","+p[1]).join(" "),fill:"none",stroke:PALETTE[j%PALETTE.length],"stroke-width":"2.6"});
        svg.appendChild(poly);
        points.forEach(p=>{
          const c=sEl("circle",{cx:p[0],cy:p[1],r:3.5,fill:PALETTE[j%PALETTE.length]});
          c.addEventListener("mousemove",ev=>tooltip(`${p[3]}${sn?" · "+sn:""}: ${fmt(p[2].value,widget.format)}`,ev));c.addEventListener("mouseleave",hideTip);svg.appendChild(c);
        });
      });
      const every=Math.max(1,Math.ceil(mat.labels.length/8));
      mat.labels.forEach((label,i)=>{if(i%every)return;const x=left+(mat.labels.length===1?plotW/2:i*plotW/(mat.labels.length-1));const t=sEl("text",{x,y:H-27,"text-anchor":"middle",class:"rb-chart-label"});t.textContent=label.length>14?label.slice(0,13)+"…":label;svg.appendChild(t);});
      body.appendChild(svg);if(mat.series[0])body.appendChild(legend(mat.series));
    }

    function renderPie(body, widget, result) {
      const data=(result.data||[]).slice(0,12);
      if(!data.length){body.innerHTML='<div class="rb-empty">No data for this visual.</div>';return;}
      const wrap=document.createElement("div");wrap.style.height="calc(100% - 30px)";
      const svg=svgRoot(),cx=260,cy=145,r=105,total=data.reduce((a,d)=>a+(Number(d.value)||0),0)||1;
      let angle=-Math.PI/2;
      data.forEach((d,i)=>{
        const slice=(Number(d.value)||0)/total*Math.PI*2, end=angle+slice;
        const x1=cx+r*Math.cos(angle),y1=cy+r*Math.sin(angle),x2=cx+r*Math.cos(end),y2=cy+r*Math.sin(end);
        const large=slice>Math.PI?1:0;
        let path=`M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`;
        const p=sEl("path",{d:path,fill:PALETTE[i%PALETTE.length],class:"rb-chart-pie-slice"});
        p.addEventListener("mousemove",ev=>tooltip(`${d.label}: ${fmt(d.value,widget.format)}`,ev));p.addEventListener("mouseleave",hideTip);p.addEventListener("click",()=>onCategory(widget,result,d));svg.appendChild(p);angle=end;
      });
      if(widget.type==="donut")svg.appendChild(sEl("circle",{cx,cy,r:55,fill:state.cfg.theme.card_background||"#fff"}));
      wrap.appendChild(svg);body.appendChild(wrap);body.appendChild(legend(data.map(d=>d.label).slice(0,10)));
    }

    function renderGauge(body, widget, result) {
      const min=Number(result.minimum==null?0:result.minimum), max=Number(result.maximum==null?100:result.maximum), value=Number(result.value)||0;
      const pct=clamp((value-min)/Math.max(.00001,max-min),0,1);
      const svg=svgRoot();svg.setAttribute("viewBox","0 0 300 180");
      const start=Math.PI,end=0,cx=150,cy=145,r=105;
      function arc(a1,a2,color,width){
        const x1=cx+r*Math.cos(a1),y1=cy+r*Math.sin(a1),x2=cx+r*Math.cos(a2),y2=cy+r*Math.sin(a2);
        return sEl("path",{d:`M ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2}`,fill:"none",stroke:color,"stroke-width":width,"stroke-linecap":"round"});
      }
      svg.appendChild(arc(Math.PI,0,"#E2E8F0",22));
      svg.appendChild(arc(Math.PI,Math.PI-Math.PI*pct,state.cfg.theme.accent||"#009EDB",22));
      const wrap=document.createElement("div");wrap.className="rb-gauge-wrap";wrap.appendChild(svg);
      const val=document.createElement("div");val.className="rb-gauge-value";val.innerHTML=`${esc(fmt(value,widget.format))}<div class="small text-muted fw-normal">${esc(min)} – ${esc(max)}</div>`;wrap.appendChild(val);body.appendChild(wrap);
    }

    function renderFunnel(body, widget, result) {
      const data=(result.data||[]).slice(0,12),max=Math.max(1,...data.map(d=>Number(d.value)||0));
      if(!data.length){body.innerHTML='<div class="rb-empty">No data for this visual.</div>';return;}
      data.forEach(d=>{
        const row=document.createElement("div");row.className="rb-funnel-row";
        row.innerHTML=`<div style="width:28%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(d.label)}">${esc(d.label)}</div><div class="rb-funnel-bar" style="width:${Math.max(5,(Number(d.value)||0)/max*65)}%">${esc(fmt(d.value,widget.format))}</div>`;
        row.querySelector(".rb-funnel-bar").onclick=()=>onCategory(widget,result,d);body.appendChild(row);
      });
    }

    function renderScatter(body, widget, result) {
      const pts=result.points||[];if(!pts.length){body.innerHTML='<div class="rb-empty">No numeric pairs for this visual.</div>';return;}
      const svg=svgRoot(),W=640,H=300,left=45,right=15,top=12,bottom=38,pw=W-left-right,ph=H-top-bottom;
      const xs=pts.map(p=>p.x),ys=pts.map(p=>p.y),xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys);
      svg.appendChild(sEl("line",{x1:left,y1:top+ph,x2:W-right,y2:top+ph,class:"rb-chart-axis"}));svg.appendChild(sEl("line",{x1:left,y1:top,x2:left,y2:top+ph,class:"rb-chart-axis"}));
      pts.forEach(p=>{const x=left+(p.x-xmin)/Math.max(.0001,xmax-xmin)*pw,y=top+ph-(p.y-ymin)/Math.max(.0001,ymax-ymin)*ph;const c=sEl("circle",{cx:x,cy:y,r:4,fill:state.cfg.theme.accent||"#009EDB",opacity:.75});c.addEventListener("mousemove",ev=>tooltip(`${p.label?p.label+" · ":""}${p.x}, ${p.y}`,ev));c.addEventListener("mouseleave",hideTip);svg.appendChild(c);});body.appendChild(svg);
    }

    function renderTable(body, widget, result) {
      if(!result.rows||!result.rows.length){body.innerHTML='<div class="rb-empty">No rows.</div>';return;}
      const labels={};fields(widget.dataset).forEach(f=>labels[f.id]=f.label);
      const wrap=document.createElement("div");wrap.className="rb-table-wrap";
      wrap.innerHTML=`<table class="table table-sm table-hover rb-table"><thead><tr>${result.columns.map(c=>`<th>${esc(labels[c]||c)}</th>`).join("")}</tr></thead><tbody>${result.rows.map(r=>`<tr>${result.columns.map(c=>`<td title="${esc(r[c])}">${esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      body.appendChild(wrap);
    }

    function renderMatrix(body, widget, result) {
      if(!result.rows||!result.rows.length){body.innerHTML='<div class="rb-empty">No matrix data.</div>';return;}
      const wrap=document.createElement("div");wrap.className="rb-table-wrap";
      let html='<table class="table table-sm table-bordered rb-matrix"><thead><tr><th></th>'+result.columns.map(c=>`<th>${esc(c)}</th>`).join("")+'</tr></thead><tbody>';
      result.rows.forEach(r=>{html+=`<tr><th>${esc(r)}</th>`+result.columns.map(c=>`<td class="text-end">${esc(fmt(result.cells[r+"\u241f"+c]||0,widget.format))}</td>`).join("")+'</tr>';});
      html+='</tbody></table>';wrap.innerHTML=html;body.appendChild(wrap);
    }

    function renderSlicer(body, widget, result) {
      const wrap=document.createElement("div");wrap.className="rb-slicer";
      (result.items||[]).forEach(item=>{const b=document.createElement("button");b.type="button";b.innerHTML=`<span class="float-end badge bg-light text-dark border">${esc(item.value)}</span>${esc(item.label)}`;b.onclick=()=>{state.crossFilters=[{dataset:widget.dataset,field:result.field,op:"eq",value:item.filter_value}];showCross(`${fieldLabel(widget.dataset,result.field)} = ${item.label}`);queryNow();};wrap.appendChild(b);});
      body.appendChild(wrap);
    }

    function renderText(body, widget, result) { body.innerHTML=`<div class="rb-text-block">${esc(result.text||widget.text||"")}</div>`; }

    function legend(names) {
      const div=document.createElement("div");div.className="rb-legend";
      names.slice(0,10).forEach((n,i)=>{const s=document.createElement("span");s.innerHTML=`<span class="rb-legend-dot" style="background:${PALETTE[i%PALETTE.length]}"></span>${esc(n)}`;div.appendChild(s);});return div;
    }

    async function save() {
      const url=root.dataset.saveUrl;if(!url)return;
      const button=document.getElementById("rbSave"),status=document.getElementById("rbSaveState");
      if(button){button.disabled=true;button.innerHTML='<span class="spinner-border spinner-border-sm me-1"></span>Saving…';}
      try{
        const response=await fetch(url,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","Accept":"application/json","X-CSRFToken":options.csrf||""},body:JSON.stringify({name:state.reportName,description:state.description,share_scope:state.shareScope,config:state.cfg})});
        const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||"Could not save.");
        state.dirty=false;if(status)status.textContent="Saved";if(button)button.innerHTML='<i class="bi bi-check2 me-1"></i>Saved';
        setTimeout(()=>{if(button){button.innerHTML='<i class="bi bi-save me-1"></i>Save';button.disabled=false;}},900);
      }catch(err){alert(err.message||"Could not save the report.");if(button){button.disabled=false;button.innerHTML='<i class="bi bi-save me-1"></i>Save';}}
    }

    function renderExportMenu() {
      const menu=document.getElementById("rbExportMenu");if(!menu)return;
      menu.innerHTML=catalog.datasets.map(d=>`<li><a class="dropdown-item" href="${esc(root.dataset.exportUrl)}?dataset=${encodeURIComponent(d.id)}"><i class="bi bi-filetype-csv me-2"></i>${esc(d.name)}</a></li>`).join("");
    }

    function bindTop() {
      const refresh=document.getElementById("rbRefresh");if(refresh)refresh.onclick=queryNow;
      const clear=document.getElementById("rbCrossClear");if(clear)clear.onclick=clearCross;
      const name=document.getElementById("rbReportName");
      if(name)name.addEventListener("input",()=>{state.reportName=name.value;state.dirty=true;const s=document.getElementById("rbSaveState");if(s)s.textContent="Unsaved changes";});
      const saveBtn=document.getElementById("rbSave");if(saveBtn)saveBtn.onclick=save;
      const addFilterBtn=document.getElementById("rbAddFilter");if(addFilterBtn)addFilterBtn.onclick=addFilter;
      const themeBtn=document.getElementById("rbTheme");
      if(themeBtn)themeBtn.onclick=()=>{
        document.getElementById("rbThemeAccent").value=state.cfg.theme.accent||"#009EDB";
        document.getElementById("rbThemeBg").value=state.cfg.theme.background||"#F5F7FA";
        document.getElementById("rbThemeCard").value=state.cfg.theme.card_background||"#FFFFFF";
        new bootstrap.Modal(document.getElementById("rbThemeModal")).show();
      };
      ["rbThemeAccent","rbThemeBg","rbThemeCard"].forEach(id=>{
        const el=document.getElementById(id);if(!el)return;
        el.addEventListener("input",()=>{
          state.cfg.theme[id==="rbThemeAccent"?"accent":id==="rbThemeBg"?"background":"card_background"]=el.value;
          state.dirty=true;renderCanvas();
        });
      });

      const undo=document.getElementById("rbUndo"),redo=document.getElementById("rbRedo");
      if(undo)undo.onclick=()=>{if(!state.undo.length)return;state.redo.push(snapshot());restore(state.undo.pop());};
      if(redo)redo.onclick=()=>{if(!state.redo.length)return;state.undo.push(snapshot());restore(state.redo.pop());};

      const print=document.getElementById("rbPrint");if(print)print.onclick=()=>window.print();
      const full=document.getElementById("rbFullscreen");if(full)full.onclick=()=>{
        const target=document.querySelector(".rb-view-root")||document.documentElement;
        if(!document.fullscreenElement)target.requestFullscreen&&target.requestFullscreen();else document.exitFullscreen&&document.exitFullscreen();
      };
      window.addEventListener("beforeunload",e=>{if(design&&state.dirty){e.preventDefault();e.returnValue="";}});
    }

    function renderAll() {
      if(design){renderDataPane();renderAddMenu();renderInspector();}
      renderFilters();renderCanvas();renderExportMenu();syncHistory();
    }

    bindTop();
    renderAll();
    queryNow();
  }

  global.UNPASSReportBuilder = { mount };
})(window);
