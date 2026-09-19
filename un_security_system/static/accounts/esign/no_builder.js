// The exact situation in the screenshot: the builder script never arrived.
const { JSDOM } = require("jsdom"); const fs = require("fs");
const tpl = fs.readFileSync("/home/claude/undss/un_security_system/templates/accounts/esign/studio/form_designer.html","utf8");
let body = tpl.split("{% block content %}")[1].split("{% endblock %}")[0]
  .replace(/\{%.*?%\}/gs,"").replace(/\{\{.*?\}\}/gs,"");
const scripts = [...tpl.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
const main = scripts[scripts.length-1];
const dom = new JSDOM(`<!doctype html><html><body>${body}</body></html>`,{runScripts:"outside-only"});
const win = dom.window, doc = win.document;
global.window = win; global.document = doc;
win.requestAnimationFrame = cb => setTimeout(cb,0);
let prompted = null, alerted = null;
win.prompt = (msg, def) => { prompted = {msg, def}; return "[qty] * [price] * 2"; };
win.alert = m => { alerted = m; };
win.confirm = () => true;
// formula.js loads; formula-builder.js deliberately does NOT
win.eval(fs.readFileSync("/home/claude/undss/un_security_system/static/accounts/esign/formula.js","utf8"));
win.StudioRules={catalog:()=>[],Builder:function(){},states:()=>({})};
win.StudioNet={getJSON:()=>Promise.resolve({}),notify:()=>{}}; win.StudioPDF={renderInto:()=>{}};

const CFG={prefill:{"":"Nothing"},widths:[3,4,6,8,9,12],templates:[],flows:[],save_url:"/s/",preview_url:"/p/",
  fill_url:"/f/",public_url:"/pub/",flow_new_url:"/nf/",designer_url:"/d/0/design/",automation_url:"/a/",delivery_summary:"x"};
const INIT={id:1,name:"T",description:"",category:"",workflow_id:null,share_scope:"office",access:"internal",
  is_published:false,owner_sees_submissions:true,reference_prefix:"FRM",submit_message:"",version:1,
  schema:{version:1,theme:{accent:"#009EDB",header_bg:"#005A8B",header_text:"#FFF",label_color:"#334155",font:"helvetica",density:"comfortable",rounded:true},
   header:{title:"T",subtitle:"",logo:"",align:"left",show_reference:true},
   elements:[{id:"t1",type:"table",width:12,label:"Items",key:"items",required:false,help:"",placeholder:"",fill_by:"submitter",prefill:"",
     columns:[{key:"no",label:"No.",kind:"number",width:1},{key:"desc",label:"Description",kind:"text",width:4},
              {key:"qty",label:"Qty",kind:"number",width:2,total:"count"},{key:"amount",label:"Amount",kind:"number",width:2,total:"sum"}],
     rows:3,show_total:true,allow_add:true,presentation:{},canvas:{},state_rules:[]}]}};
win.eval(`(function(){ const CFG=${JSON.stringify(CFG)}, INIT=${JSON.stringify(INIT)}, CSRF="t";\n` +
  main.replace(/^\s*\(function\(\)\{[\s\S]*?const CFG = .*?;\n/,""));

let failed=0; const check=(n,c,x="")=>{console.log((c?"  ok   ":"  FAIL ")+n+(x?"  "+x:""));if(!c)failed++;};
doc.querySelector('#paper [data-id="t1"]').dispatchEvent(new win.MouseEvent("pointerdown",{bubbles:true}));
const insp = doc.getElementById("inspector");

check("builder is genuinely absent", !win.StudioFormulaBuilder);
check("the inspector says so", insp.textContent.includes("formula builder did not load"));
check("it names the remedy", insp.textContent.includes("collectstatic"));
const fx = insp.querySelectorAll("[data-colfx]");
check("fx buttons still rendered", fx.length === 4, fx.length+" buttons");

fx[3].dispatchEvent(new win.MouseEvent("click",{bubbles:true}));
check("clicking fx now does something", !!prompted, prompted ? "prompt shown" : "STILL DEAD");
check("prompt names the column", prompted && prompted.msg.includes("Amount"));
check("the typed formula was saved",
      INIT.schema.elements[0].columns[3].formula === undefined ||
      doc.getElementById("saveState").textContent === "Unsaved changes",
      doc.getElementById("saveState").textContent);

// a bad formula must be refused even in the fallback
win.prompt = () => "SUM([amount] * ";
fx[2].dispatchEvent(new win.MouseEvent("click",{bubbles:true}));
check("fallback still validates", !!alerted && alerted.includes("cannot be read"), String(alerted).split("\n")[0]);
console.log(failed?`\n${failed} FAILURE(S)`:"\nFALLBACK OK — no more dead buttons");
process.exit(failed?1:0);
