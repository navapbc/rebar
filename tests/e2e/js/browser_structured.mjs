// Browser structured-fields probe (story a83a): load the editor, select the LOOP
// SubProcess, and exercise the structured per-field entries that replace the raw JSON
// textarea for the common path:
//   1. ROUND-TRIP — edit the structured `max_iterations` field; assert it writes back into
//      the node's rebar:Config blob (the same blob the Python serializer reads).
//   2. FIELD ERROR / NO LOSS — type a NON-NUMERIC max_iterations; assert a field error is
//      shown AND the prior numeric value survives in the blob (never silently dropped).
//   3. RAW FALLBACK — assert the "Advanced (raw JSON)" entry is present for the known kind
//      (the raw editor stays reachable for uncommon keys).
// Usage: node browser_structured.mjs <editor-url>
import { chromium } from "playwright";

const url = process.argv[2];
const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

await page.goto(url, { waitUntil: "load" });
await page.waitForFunction(() => !!window.__rebarModeler, null, { timeout: 15000 });
await page.waitForTimeout(500);

// Find the loop SubProcess element id (StandardLoopCharacteristics → "loop").
const loopId = await page.evaluate(() => {
  const m = window.__rebarModeler;
  const el = m.get("elementRegistry").filter((e) => {
    const bo = e.businessObject;
    return (
      bo &&
      bo.$type === "bpmn:SubProcess" &&
      bo.loopCharacteristics &&
      bo.loopCharacteristics.$type === "bpmn:StandardLoopCharacteristics"
    );
  })[0];
  return el ? el.id : null;
});

const readConfig = (id) =>
  page.evaluate((eid) => {
    const m = window.__rebarModeler;
    const bo = m.get("elementRegistry").get(eid).businessObject;
    const ee = bo.extensionElements;
    const cfg = ee && (ee.values || []).find((v) => v.$type === "rebar:Config");
    return cfg ? cfg.value : null;
  }, id);

// Select the loop and expand the Rebar group so its entries mount.
await page.evaluate((id) => {
  const m = window.__rebarModeler;
  m.get("selection").select(m.get("elementRegistry").get(id));
}, loopId);
await page.waitForTimeout(300);
await page.evaluate(() => {
  const hdr = Array.from(document.querySelectorAll(".bio-properties-panel-group-header")).find(
    (h) => /rebar/i.test(h.textContent)
  );
  if (hdr) hdr.click();
});
await page.waitForSelector("#bio-properties-panel-rebar-loop-max", {
  state: "visible",
  timeout: 5000,
});

const configBefore = await readConfig(loopId);

// The bpmn-js Textfield commits its value on blur (and on debounced input); fill + blur is
// the deterministic way to commit a structured edit without a keystroke/debounce race.
const SEL = "#bio-properties-panel-rebar-loop-max";
const commit = async (val) => {
  await page.fill(SEL, val);
  await page.dispatchEvent(SEL, "input");
  await page.locator(SEL).blur();
  await page.waitForTimeout(300);
};

// 1. ROUND-TRIP: set a valid new max_iterations through the STRUCTURED field.
await commit("7");
const configAfterValid = await readConfig(loopId);

// 2. FIELD ERROR + NO LOSS: a non-numeric value shows a field error and keeps prior value.
await commit("not-a-number");
const invalid = await page.evaluate(() => {
  const entry = document.querySelector('[data-entry-id="rebar-loop-max"]');
  const err = entry && entry.querySelector(".bio-properties-panel-error");
  return {
    hasError: !!(entry && entry.className.includes("has-error")),
    errorText: err ? err.textContent : "",
  };
});
const configAfterInvalid = await readConfig(loopId);

// 3. NO RAW JSON editor for the known kind (da27 AC "no raw JSON textarea"): neither the
// removed "Advanced (raw JSON)" fallback nor a bare raw-config entry is present.
const rawFallback = await page.evaluate(() => {
  const adv = document.querySelector('[data-entry-id="rebar-config-advanced"]');
  const raw = document.querySelector('[data-entry-id="rebar-config"]');
  return { present: !!adv || !!raw };
});

// 4. Persist the VALID edit to the IR (save), so the Python side can reload-assert.
// Re-set the field to a clean valid value first (the invalid keystroke left an error).
await commit("9");
await page.click("#save");
await page.waitForTimeout(800);
const status = await page.evaluate(() => document.getElementById("status").textContent);

console.log(
  JSON.stringify(
    { errors, loopId, configBefore, configAfterValid, invalid, configAfterInvalid, rawFallback, status },
    null,
    2
  )
);
await browser.close();
