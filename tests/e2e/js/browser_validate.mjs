// Browser live-validation probe (story 998e): load the editor, select a scripted step,
// drive its config through INVALID -> VALID via the validation controller, and exercise
// the UNAVAILABLE state by stubbing /validate to a 500. Reports the observed states +
// whether the Save button is blocked, so the Python side can assert the wiring.
// Usage: node browser_validate.mjs <editor-url>
import { chromium } from "playwright";

const url = process.argv[2];
const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

await page.goto(url, { waitUntil: "load" });
await page.waitForFunction(() => !!window.__rebarModeler && !!window.__rebarValidation, null, {
  timeout: 15000,
});
await page.waitForTimeout(400);

// Pick a scripted step that has an input contract (any ScriptTask in the demo).
const stepId = await page.evaluate(() => {
  const m = window.__rebarModeler;
  const el = m
    .get("elementRegistry")
    .filter((e) => e.businessObject && e.businessObject.$type === "bpmn:ScriptTask")[0];
  return el ? el.id : null;
});

// Drive an INVALID result through the controller (deterministic — no keyboard race).
const invalid = await page.evaluate(() => {
  const v = window.__rebarValidation;
  v.setResult({ ok: false, unavailable: false, errors: [{ path: "$.policy", message: "bad" }] });
  const region = document.getElementById("rebar-validate-region");
  const save = document.getElementById("save");
  return {
    visible: region && region.style.display !== "none",
    cls: region ? region.className : "",
    text: region ? region.textContent : "",
    saveDisabled: !!(save && save.disabled),
    state: v.state.value,
  };
});

// Now a VALID result CLEARS the error and re-enables Save.
const valid = await page.evaluate(() => {
  const v = window.__rebarValidation;
  v.setResult({ ok: true, unavailable: false, errors: [] });
  const region = document.getElementById("rebar-validate-region");
  const save = document.getElementById("save");
  return {
    hidden: !region || region.style.display === "none",
    saveDisabled: !!(save && save.disabled),
    state: v.state.value,
  };
});

// UNAVAILABLE: stub /validate to a 500 and run a real round-trip via validateNow.
await page.route("**/validate", (route) =>
  route.fulfill({
    status: 500,
    contentType: "application/json",
    body: JSON.stringify({ ok: false, unavailable: true, errors: [{ path: "", message: "boom" }] }),
  })
);
const unavailable = await page.evaluate(async (id) => {
  const m = window.__rebarModeler;
  const v = window.__rebarValidation;
  const el = m.get("elementRegistry").get(id);
  await v.validateNow(el.businessObject);
  const region = document.getElementById("rebar-validate-region");
  const save = document.getElementById("save");
  return {
    visible: region && region.style.display !== "none",
    cls: region ? region.className : "",
    text: region ? region.textContent : "",
    saveDisabled: !!(save && save.disabled),
    state: v.state.value,
  };
}, stepId);

// The per-kind HELP panel rendered all five kinds.
const help = await page.evaluate(() => {
  const panel = document.getElementById("rebar-kind-help");
  return {
    present: !!panel,
    text: panel ? panel.textContent : "",
  };
});

console.log(JSON.stringify({ errors, stepId, invalid, valid, unavailable, help }, null, 2));
await browser.close();
