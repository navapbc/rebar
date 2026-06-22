// Real-browser diagnostic / E2E probe for the workflow editor. Loads the running editor
// in headless Chromium, captures console errors + uncaught exceptions, and drives the
// modeler (via the window.__rebarModeler test hook) to verify the diagram rendered, the
// properties panel reacts to selection, and the Rebar group is present + editable.
// Usage: node browser_diag.mjs <editor-url>   ->  prints a JSON report.
import { chromium } from "playwright";

const url = process.argv[2];
const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on("console", (m) => {
  if (m.type() === "error") errors.push("console.error: " + m.text());
});
page.on("pageerror", (e) => errors.push("pageerror: " + (e.stack || e.message)));

await page.goto(url, { waitUntil: "load" });
await page.waitForFunction(() => !!window.__rebarModeler, null, { timeout: 15000 });
await page.waitForTimeout(800);

// Diagram render: shapes, connections, exact-overlap among top-level shapes.
const render = await page.evaluate(() => {
  const m = window.__rebarModeler;
  const reg = m.get("elementRegistry");
  const shapes = reg.filter((e) => e.type !== "label" && e.waypoints === undefined && e.parent);
  const conns = reg.filter((e) => e.waypoints !== undefined);
  const boxes = shapes
    .filter((s) => s.x !== undefined)
    .map((s) => ({ id: s.id, x: s.x, y: s.y, w: s.width, h: s.height }));
  const seen = new Set();
  let overlaps = 0;
  for (const b of boxes) {
    const key = Math.round(b.x) + "," + Math.round(b.y);
    if (seen.has(key)) overlaps++;
    seen.add(key);
  }
  return {
    shapeCount: shapes.length,
    connectionCount: conns.length,
    exactOverlaps: overlaps,
  };
});

// Selection -> properties panel reacts; Rebar group present with kind + editable config.
async function inspect(id) {
  await page.evaluate((id) => {
    const m = window.__rebarModeler;
    const el = m.get("elementRegistry").get(id);
    m.get("selection").select(el);
  }, id);
  // Wait for the panel header to reflect THIS element (the panel re-renders async).
  await page
    .waitForFunction(
      (id) => {
        const h = document.querySelector(".bio-properties-panel-header-type, .bio-properties-panel-header");
        return h && document.querySelector(`[data-element-id="${id}"]`);
      },
      id,
      { timeout: 4000 }
    )
    .catch(() => {});
  await page.waitForTimeout(300);
  return page.evaluate((id) => {
    const m = window.__rebarModeler;
    const el = m.get("elementRegistry").get(id);
    const titles = Array.from(
      document.querySelectorAll(".bio-properties-panel-group-header-title")
    ).map((e) => e.textContent.trim());
    return { selected: id, type: el && el.businessObject && el.businessObject.$type, groups: titles, rebarGroup: titles.some((t) => /rebar/i.test(t)) };
  }, id);
}

const onScript = await inspect("commits");
const onAgent = await inspect("review");
const onLoop = await inspect("refine_loop");
const onBranch = await inspect("decide");

console.log(JSON.stringify({ errors, render, onScript, onAgent, onLoop, onBranch }, null, 2));
await browser.close();
