// Runs the real form_designer.html script in a DOM and drives the inspector.
const { JSDOM } = require("jsdom"); const fs = require("fs");
const tplPath = "/home/claude/undss/un_security_system/templates/accounts/esign/studio/form_designer.html";
let tpl = fs.readFileSync(tplPath, "utf8");

// the page body, with Django tags stripped the way the server would resolve them
let body = tpl.split("{% block content %}")[1].split("{% endblock %}")[0];
body = body.replace(/\{%.*?%\}/gs, "").replace(/\{\{.*?\}\}/gs, "");
// the designer's own script (the last <script> block in extra_js)
const scripts = [...tpl.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const main = scripts[scripts.length - 1];

const dom = new JSDOM(`<!doctype html><html><body>${body}</body></html>`, { runScripts: "outside-only" });
const win = dom.window, doc = win.document;
global.window = win; global.document = doc;
win.requestAnimationFrame = cb => setTimeout(cb, 0);
win.confirm = () => true;
win.alert = m => console.log("ALERT:", m);
win.fetch = () => Promise.resolve({ok:true, json:()=>Promise.resolve({})});
win.eval(fs.readFileSync("/home/claude/undss/un_security_system/static/accounts/esign/formula.js","utf8"));
win.eval(fs.readFileSync("/home/claude/undss/un_security_system/static/accounts/esign/formula-builder.js","utf8"));
win.StudioRules = { catalog: () => [], Builder: function(){}, states: () => ({}) };
win.StudioNet = { getJSON: () => Promise.resolve({}), notify: () => {} };
win.StudioPDF = { renderInto: () => {} };

const CFG = { prefill: {"":"Nothing"}, widths:[3,4,6,8,9,12], templates: [], flows: [],
  save_url:"/s/", preview_url:"/p/", fill_url:"/f/", public_url:"/pub/", flow_new_url:"/nf/",
  designer_url:"/esign/flows/0/design/", automation_url:"/auto/", delivery_summary:"Each signer gets a copy." };
const INIT = { id: 1, name:"Test form", description:"", category:"", workflow_id:null,
  share_scope:"office", access:"internal", is_published:false, owner_sees_submissions:true,
  reference_prefix:"FRM", submit_message:"", version:3,
  schema:{version:1, theme:{accent:"#009EDB",header_bg:"#005A8B",header_text:"#FFF",label_color:"#334155",font:"helvetica",density:"comfortable",rounded:true},
    header:{title:"Test form",subtitle:"",logo:"",align:"left",show_reference:true},
    elements:[
      {id:"t1",type:"table",width:12,label:"Items",key:"items",required:false,help:"",placeholder:"",fill_by:"submitter",prefill:"",
       columns:[{key:"item",label:"Item",kind:"text",width:4},{key:"qty",label:"Qty",kind:"number",width:1},
                {key:"price",label:"Unit price",kind:"number",width:2},{key:"line",label:"Line total",kind:"number",width:2,formula:"[qty] * [price]",total:"sum"}],
       rows:3, show_total:true, allow_add:true, presentation:{}, canvas:{}, state_rules:[]},
      {id:"f1",type:"number",width:6,label:"Subtotal",key:"subtotal",required:false,help:"",placeholder:"",fill_by:"submitter",prefill:"",
       formula:{enabled:true,expr:"SUM([items.line])",output:"number",decimals:2,recalc:"always"},
       presentation:{}, canvas:{}, state_rules:[]},
      {id:"f2",type:"text",width:6,label:"Requested by",key:"requested_by",required:true,help:"",placeholder:"",fill_by:"submitter",prefill:"",
       presentation:{}, canvas:{}, state_rules:[]},
    ]}};

let failed = 0;
const check = (n,c,x="") => { console.log((c?"  ok   ":"  FAIL ")+n+(x?"  "+x:"")); if(!c) failed++; };
const click = n => n.dispatchEvent(new win.MouseEvent("click",{bubbles:true}));
const select = n => n.dispatchEvent(new win.MouseEvent("pointerdown",{bubbles:true}));

try {
  win.eval(`(function(){ const CFG = ${JSON.stringify(CFG)}, INIT = ${JSON.stringify(INIT)}, CSRF = "tok";\n` +
           main.replace(/^\s*\(function\(\)\{[\s\S]*?const CFG = .*?;\n/, "") );
  console.log("designer script ran without throwing\n");
} catch (e) {
  console.log("!! designer script THREW:", e.message);
  process.exit(1);
}

check("canvas drew the fields", doc.querySelectorAll("#paper [data-id]").length === 3,
      doc.querySelectorAll("#paper [data-id]").length + " blocks");
check("calculated column badged on the canvas", doc.querySelector("#paper").innerHTML.includes("fb-fx-badge"));

// select the calculated field
select(doc.querySelector('#paper [data-id="f1"]'));
const insp = doc.getElementById("inspector");
check("inspector opened for the field", insp.innerHTML.includes("Subtotal"));
check("formula section shown", !!insp.querySelector("#fxOpen"), insp.querySelector("#fxOpen") ? "" : "no Build button");
check("preview reads in words", (insp.querySelector("#fxPreview")||{}).textContent || "",
      (insp.querySelector("#fxPreview")||{}).textContent);

// click Build -> the modal must open
if (insp.querySelector("#fxOpen")) {
  click(insp.querySelector("#fxOpen"));
  const modal = doc.querySelector(".fxb-modal");
  check("Build opens the dialog", !!modal && modal.classList.contains("show"));
  if (modal) {
    check("dialog is pre-filled", modal.querySelector("#fxbRaw").value === "SUM([items.line])",
          modal.querySelector("#fxbRaw").value);
    const raw = modal.querySelector("#fxbRaw");
    raw.value = "SUM([items.line]) * 1.15";
    raw.dispatchEvent(new win.Event("input",{bubbles:true}));
    click(modal.querySelector("#fxbSave"));
    check("saving writes back to the schema",
          doc.getElementById("saveState").textContent === "Unsaved changes");
  }
}

// now the table's column formula button
select(doc.querySelector('#paper [data-id="t1"]'));
const fxCols = doc.querySelectorAll("#inspector [data-colfx]");
check("every column has an fx button", fxCols.length === 4, fxCols.length + " buttons");
if (fxCols.length) {
  click(fxCols[3]);
  const modal = doc.querySelector(".fxb-modal");
  check("column dialog opens in row scope",
        modal.classList.contains("show") && modal.querySelector(".fxb-group-name").textContent.includes("This row"));
  check("column dialog pre-filled", modal.querySelector("#fxbRaw").value === "[qty] * [price]",
        modal.querySelector("#fxbRaw").value);
}
// ticking the box on a plain field should open the builder by itself
select(doc.querySelector('#paper [data-id="f2"]'));
const tick = doc.querySelector('#inspector [data-k="formula.enabled"]');
check("a plain field offers the calculation tick", !!tick);
if (tick) {
  tick.checked = true;
  tick.dispatchEvent(new win.Event("change", {bubbles:true}));
  const opened = () => doc.querySelector(".fxb-modal").classList.contains("show");
  setTimeout(() => {
    check("ticking the box opens the builder", opened());
    console.log(failed ? `\n${failed} FAILURE(S)` : "\nDESIGNER OK");
    process.exit(failed ? 1 : 0);
  }, 20);
} else {
  console.log(failed ? `\n${failed} FAILURE(S)` : "\nDESIGNER OK");
  process.exit(failed ? 1 : 0);
}
const _unused = (""); //
process.exit(failed ? 1 : 0);
