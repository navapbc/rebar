// Browser probe: morph a scripted step to an agent step (the change-type the UI offers)
// and report whether its <rebar:Config> survives. Guards against bpmn-js's documented
// "morph drops extensionElements" behavior regressing our editor. Usage: node browser_morph.mjs <url>
import { chromium } from "playwright";
const url = process.argv[2];
const b = await chromium.launch();
const p = await b.newPage();
const errs = [];
p.on("pageerror", (e) => errs.push(e.message));
await p.goto(url, { waitUntil: "load" });
await p.waitForFunction(() => !!window.__rebarModeler, null, { timeout: 15000 });
await p.waitForTimeout(600);
const cfg = (bo) => {
  const ee = bo.extensionElements;
  const c = ee && (ee.values || []).find((v) => v.$type === "rebar:Config");
  return c ? c.value : null;
};
const out = await p.evaluate(
  (cfgSrc) => {
    const cfg = eval("(" + cfgSrc + ")");
    const m = window.__rebarModeler;
    const el = m.get("elementRegistry").get("commits");
    const before = cfg(el.businessObject);
    const nw = m.get("bpmnReplace").replaceElement(el, { type: "bpmn:ServiceTask" });
    return { id: nw.id, type: nw.businessObject.$type, before, after: cfg(nw.businessObject) };
  },
  cfg.toString()
);
console.log(JSON.stringify({ errs, ...out }, null, 2));
await b.close();
