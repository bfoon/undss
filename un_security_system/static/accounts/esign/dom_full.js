const { JSDOM } = require("jsdom");
const fs = require("fs");
const dom = new JSDOM(`<!doctype html><html><body></body></html>`, { runScripts: "outside-only" });
const win = dom.window, doc = win.document;
global.window = win; global.document = doc;
win.requestAnimationFrame = cb => setTimeout(cb, 0);
win.confirm = () => true;
win.alert = m => console.log("ALERT:", m);
win.eval(fs.readFileSync("/home/claude/undss/un_security_system/static/accounts/esign/formula.js", "utf8"));
win.eval(fs.readFileSync("/home/claude/undss/un_security_system/static/accounts/esign/formula-builder.js", "utf8"));

const fields = [
  {key:"items", label:"Items", type:"table", columns:[{key:"qty",label:"Qty",kind:"number"},{key:"price",label:"Unit price",kind:"number"},{key:"line",label:"Line total",kind:"number"}]},
  {key:"freight", label:"Freight", type:"number"},
  {key:"subtotal", label:"Subtotal", type:"number"},
  {key:"staff_type", label:"Staff type", type:"select", options:["National","International"]},
];
let saved = null, fails = 0;
const check = (name, cond, extra="") => { console.log((cond?"  ok   ":"  FAIL ")+name+(extra?"  "+extra:"")); if(!cond) fails++; };
const click = node => node.dispatchEvent(new win.MouseEvent("click", {bubbles:true}));

win.StudioFormulaBuilder.open({
  title:"Grand total", expr:"", scope:"field", selfKey:"grand_total", fields,
  context:[{token:"@today",label:"Today's date"}], onSave: e => { saved = e; },
});

const modal = doc.querySelector(".fxb-modal");
check("modal is in the document", !!modal);
check("modal is visible", modal.classList.contains("show") && modal.style.display === "block");
check("backdrop added", !!doc.querySelector(".modal-backdrop"));

const cards = [...modal.querySelectorAll(".fxb-card")];
console.log("\npalette cards:", cards.length);
check("questions are listed", cards.some(c => c.textContent.includes("Freight")));
check("table columns offered as a total", cards.some(c => c.dataset.token === "SUM([items.line])"));
check("the field being edited is excluded", !cards.some(c => c.dataset.token === "[grand_total]"));

// ── click to insert ──────────────────────────────────────────────────────
click(cards.find(c => c.dataset.token === "SUM([items.line])"));
click([...modal.querySelectorAll(".fxb-op")].find(b => b.dataset.op.trim() === "+"));
click(cards.find(c => c.dataset.token === "[freight]"));
const canvas = modal.querySelector("#fxbCanvas");
console.log("\nafter three clicks:", modal.querySelector("#fxbRaw").value);
check("expression built by clicking", modal.querySelector("#fxbRaw").value === "SUM([items.line]) + [freight]");
check("chips rendered", canvas.querySelectorAll(".fxb-chip").length > 0, canvas.querySelectorAll(".fxb-chip").length+" chips");
check("chips show labels, not keys", canvas.textContent.includes("Items · Line total") || canvas.textContent.includes("Freight"));
check("status says ready", modal.querySelector("#fxbStatus").textContent.includes("Ready"));
check("save enabled", !modal.querySelector("#fxbSave").disabled);
console.log("readout:", modal.querySelector("#fxbReadout").textContent.replace(/\s+/g," ").trim());

// ── drag and drop ────────────────────────────────────────────────────────
const dt = { data:{}, setData(k,v){this.data[k]=v;}, getData(k){return this.data[k];}, effectAllowed:"" };
const card = cards.find(c => c.dataset.token === "[subtotal]");
const ds = new win.MouseEvent("dragstart", {bubbles:true});
Object.defineProperty(ds, "dataTransfer", {value: dt});
card.dispatchEvent(ds);
const gaps = [...canvas.querySelectorAll(".fxb-gap")];
check("drop gaps exist", gaps.length > 0, gaps.length+" gaps");
const drop = new win.MouseEvent("drop", {bubbles:true, clientX:0, clientY:0});
Object.defineProperty(drop, "dataTransfer", {value: dt});
gaps[0].dispatchEvent(drop);
console.log("after dragging Subtotal to the front:", modal.querySelector("#fxbRaw").value);
check("drag inserted the token", modal.querySelector("#fxbRaw").value.indexOf("[subtotal]") === 0);

// ── click a chip to remove it ────────────────────────────────────────────
const before = canvas.querySelectorAll(".fxb-chip").length;
click(canvas.querySelector(".fxb-chip"));
check("clicking a chip removes it", canvas.querySelectorAll(".fxb-chip").length === before - 1);

// ── validation blocks a bad reference ────────────────────────────────────
const raw = modal.querySelector("#fxbRaw");
raw.value = "[does_not_exist] * 2";
raw.dispatchEvent(new win.Event("input", {bubbles:true}));
check("unknown question is refused", modal.querySelector("#fxbSave").disabled,
      modal.querySelector("#fxbStatus").textContent.trim());
raw.value = "SUM([items.line]) * ";
raw.dispatchEvent(new win.Event("input", {bubbles:true}));
check("bad syntax is refused", modal.querySelector("#fxbSave").disabled,
      modal.querySelector("#fxbStatus").textContent.trim());

// ── a ready-made preset ──────────────────────────────────────────────────
const preset = [...modal.querySelectorAll(".fxb-card[data-preset]")][0];
click(preset);
console.log("\npreset applied:", modal.querySelector("#fxbRaw").value);

// ── save ─────────────────────────────────────────────────────────────────
raw.value = "ROUND(SUM([items.line]) * 1.15, 2)";
raw.dispatchEvent(new win.Event("input", {bubbles:true}));
click(modal.querySelector("#fxbSave"));
check("onSave received the expression", saved === "ROUND(SUM([items.line]) * 1.15, 2)", String(saved));
check("modal closed", !modal.classList.contains("show"));
check("backdrop removed", !doc.querySelector(".modal-backdrop"));

// ── row scope ────────────────────────────────────────────────────────────
saved = null;
win.StudioFormulaBuilder.open({
  title:"Line total", expr:"", scope:"row", columnKey:"line",
  row:{key:"items", columns:fields[0].columns}, fields, onSave: e => { saved = e; },
});
const rowCards = [...modal.querySelectorAll(".fxb-card")];
check("this row's cells offered first", modal.querySelector(".fxb-group-name").textContent.includes("This row"));
check("the column being edited is excluded", !rowCards.some(c => c.dataset.token === "[line]"));
click(rowCards.find(c => c.dataset.token === "[qty]"));
click([...modal.querySelectorAll(".fxb-op")].find(b => b.dataset.op.trim() === "*"));
click(rowCards.find(c => c.dataset.token === "[price]"));
click(modal.querySelector("#fxbSave"));
check("row formula saved", saved === "[qty] * [price]", String(saved));

// ── a function card brings its closing bracket ───────────────────────────
saved = null;
win.StudioFormulaBuilder.open({title:"T", expr:"", scope:"field", fields, onSave:e=>{saved=e;}});
click([...modal.querySelectorAll(".fxb-card")].find(c => c.dataset.token === "ROUND("));
console.log("\nafter clicking ROUND(:", modal.querySelector("#fxbRaw").value);
check("brackets stay balanced", modal.querySelector("#fxbRaw").value === "ROUND()");

console.log(fails ? `\n${fails} FAILURE(S)` : "\nALL DOM INTERACTIONS OK");
process.exit(fails ? 1 : 0);

// ── plain-language problem messages ──────────────────────────────────────
console.log("\nmessages people will actually see:");
const say = expr => {
  const raw2 = modal.querySelector("#fxbRaw");
  raw2.value = expr;
  raw2.dispatchEvent(new win.Event("input", {bubbles:true}));
  console.log("  " + JSON.stringify(expr).padEnd(34), "->",
    modal.querySelector("#fxbStatus").textContent.trim(),
    modal.querySelector("#fxbSave").disabled ? "(blocked)" : "(allowed)");
};
["[subtotal] SUM([items.line])", "ROUND([freight]", "[freight] +", "SUM([items.line]))",
 "[freight] SUM([items.line])", "ROUND(SUM([items.line]) * 1.15, 2)"].forEach(say);
