// Browser edit→save probe: load the editor, edit a step's Rebar config via the panel,
// click Save, and report. The caller (Python) then checks the IR file persisted the edit.
// Usage: node browser_edit.mjs <editor-url>
import { chromium } from "playwright";

const url = process.argv[2];
const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

await page.goto(url, { waitUntil: "load" });
await page.waitForFunction(() => !!window.__rebarModeler, null, { timeout: 15000 });
await page.waitForTimeout(600);

// Select the 'commits' step so the Rebar config entry appears.
await page.evaluate(() => {
  const m = window.__rebarModeler;
  m.get("selection").select(m.get("elementRegistry").get("commits"));
});
// Expand the (collapsed-by-default) Rebar group by clicking its header.
await page.waitForTimeout(300);
await page.evaluate(() => {
  const hdr = Array.from(document.querySelectorAll(".bio-properties-panel-group-header")).find(
    (h) => /rebar/i.test(h.textContent)
  );
  if (hdr) hdr.click();
});
await page.waitForSelector("#bio-properties-panel-rebar-config", { state: "visible", timeout: 5000 });

// Edit the config JSON: add a key under `with`.
const newConfig = JSON.stringify({ with: { ticket_id: "${{ inputs.ticket_id }}", note: "EDITED_BY_TEST" } });
await page.fill("#bio-properties-panel-rebar-config", newConfig);
await page.dispatchEvent("#bio-properties-panel-rebar-config", "change");
await page.waitForTimeout(600);

// Confirm the edit reached the model before saving.
const inModel = await page.evaluate(() => {
  const m = window.__rebarModeler;
  const bo = m.get("elementRegistry").get("commits").businessObject;
  const ee = bo.extensionElements;
  const cfg = ee && (ee.values || []).find((v) => v.$type === "rebar:Config");
  return cfg ? cfg.value : null;
});

await page.click("#save");
await page.waitForTimeout(800);
const status = await page.evaluate(() => document.getElementById("status").textContent);

console.log(JSON.stringify({ errors, inModel, status }, null, 2));
await browser.close();
