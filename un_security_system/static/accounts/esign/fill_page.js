// The fill page: markup exactly as _form_sheet.html renders it, then attach().
const { JSDOM } = require("jsdom"); const fs = require("fs");
const html = `<form id="f">
 <div class="fs-item" data-el="t1" data-type="table" data-key="items" data-mine="1">
  <table data-table="f_items">
   <tbody>
    <tr><td><input data-col="item" value=""></td><td><input data-col="qty" value=""></td>
        <td><input data-col="price" value=""></td><td><input data-col="line" value="" readonly></td></tr>
    <tr><td><input data-col="item" value="Router"></td><td><input data-col="qty" value="3"></td>
        <td><input data-col="price" value="1250.50"></td><td><input data-col="line" value="" readonly></td></tr>
    <tr><td><input data-col="item" value="Cable"></td><td><input data-col="qty" value="10"></td>
        <td><input data-col="price" value="45"></td><td><input data-col="line" value="" readonly></td></tr>
   </tbody>
   <tfoot><tr><th></th><th></th><th></th><th><span data-total="line">0</span></th></tr></tfoot>
  </table>
 </div>
 <div class="fs-item" data-el="f1" data-type="number" data-key="subtotal" data-mine="1">
   <input name="f_subtotal" value="999"></div>
 <div class="fs-item" data-el="f2" data-type="number" data-key="vat" data-mine="1">
   <input name="f_vat" value=""></div>
 <div class="fs-item" data-el="f3" data-type="number" data-key="freight" data-mine="1">
   <input name="f_freight" value="200"></div>
 <div class="fs-item" data-el="f4" data-type="number" data-key="total" data-mine="1">
   <input name="f_total" value=""></div>
</form>`;
const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {runScripts:"outside-only"});
const win = dom.window, doc = win.document;
global.window = win; global.document = doc;
win.requestAnimationFrame = cb => cb();
win.eval(fs.readFileSync("/home/claude/undss/un_security_system/static/accounts/esign/formula.js","utf8"));

const elements = [
  {id:"t1",type:"table",key:"items",show_total:true,columns:[
    {key:"item",label:"Item",kind:"text"},{key:"qty",label:"Qty",kind:"number"},
    {key:"price",label:"Price",kind:"number"},
    {key:"line",label:"Line total",kind:"number",formula:"[qty] * [price]",total:"sum"}]},
  {id:"f1",type:"number",key:"subtotal",formula:{enabled:true,expr:"SUM([items.line])",output:"number",decimals:2}},
  {id:"f2",type:"number",key:"vat",formula:{enabled:true,expr:"ROUND([subtotal] * 0.15, 2)",output:"number",decimals:2}},
  {id:"f3",type:"number",key:"freight"},
  {id:"f4",type:"number",key:"total",formula:{enabled:true,expr:"[subtotal] + [vat] + [freight]",output:"number",decimals:2}},
];
const handle = win.StudioFormula.attach(doc.getElementById("f"), elements, {});
let failed = 0;
const check = (n,c,x="") => { console.log((c?"  ok   ":"  FAIL ")+n+(x?"  "+x:"")); if(!c) failed++; };
const val = sel => doc.querySelector(sel).value;
const rows = [...doc.querySelectorAll("tbody tr")];

check("attach returned a handle", !!handle);
check("blank row stays blank", rows[0].querySelector('[data-col="line"]').value === "", JSON.stringify(rows[0].querySelector('[data-col="line"]').value));
check("row 2 calculated", rows[1].querySelector('[data-col="line"]').value === "3751.50", rows[1].querySelector('[data-col="line"]').value);
check("row 3 calculated", rows[2].querySelector('[data-col="line"]').value === "450.00", rows[2].querySelector('[data-col="line"]').value);
check("footer total", doc.querySelector("[data-total=line]").textContent === "4,201.50", doc.querySelector("[data-total=line]").textContent);
check("tampered subtotal overwritten", val('[name=f_subtotal]') === "4201.50", val('[name=f_subtotal]'));
check("vat chained off subtotal", val('[name=f_vat]') === "630.23", val('[name=f_vat]'));
check("grand total chained", val('[name=f_total]') === "5031.73", val('[name=f_total]'));
check("calculated boxes are read-only", doc.querySelector('[name=f_total]').readOnly && rows[1].querySelector('[data-col="line"]').readOnly);
check("typed boxes stay editable", !doc.querySelector('[name=f_freight]').readOnly);

// typing must recalculate
const qty = rows[1].querySelector('[data-col="qty"]');
qty.value = "4";
qty.dispatchEvent(new win.Event("input", {bubbles:true}));
check("typing recalculates the row", rows[1].querySelector('[data-col="line"]').value === "5002.00", rows[1].querySelector('[data-col="line"]').value);
check("and everything downstream", val('[name=f_total]') === "6469.80", val('[name=f_total]'));

// filling the first row must not shift anything
const q0 = rows[0].querySelector('[data-col="qty"]'), p0 = rows[0].querySelector('[data-col="price"]');
q0.value = "2"; p0.value = "10";
p0.dispatchEvent(new win.Event("input", {bubbles:true}));
check("first row calculates in place", rows[0].querySelector('[data-col="line"]').value === "20.00", rows[0].querySelector('[data-col="line"]').value);
check("rows below unchanged", rows[1].querySelector('[data-col="line"]').value === "5002.00", rows[1].querySelector('[data-col="line"]').value);
console.log(failed ? `\n${failed} FAILURE(S)` : "\nFILL PAGE OK");
process.exit(failed?1:0);
