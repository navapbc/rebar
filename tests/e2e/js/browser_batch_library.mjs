// Browser probe for the library-backed batch criteria editing (story B-UX): load the editor
// on the batch-demo fixture, select the BATCH ServiceTask, and exercise the dropdowns + the
// in-editor authoring the editor must now provide instead of free-text typing:
//   (a) RENDER  — the criterion `prompt` field is a SELECT whose options are the library
//                 entries (window.REBAR_LIBRARY) plus a "➕ Create new…" sentinel.
//   (b) SELECT  — picking an existing library id persists into rebar:Config (the IR source).
//   (c) TRIGGER — opening the `when` "➕ New trigger…" form, naming a trigger + keywords, and
//                 adding it sets the criterion's `when` to the full ${{ steps... }} expression.
//   (d) AUTHOR  — the prompt "➕ Create new…" form creates a NEW criterion (POST /library/create
//                 → .rebar/prompts/<id>.md) and references the new id on the criterion.
// Usage: node browser_batch_library.mjs <editor-url>
import { chromium } from "playwright";

const url = process.argv[2];
const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

await page.goto(url, { waitUntil: "load" });
await page.waitForFunction(() => !!window.__rebarModeler, null, {
  timeout: 15000,
});
await page.waitForTimeout(500);

const readConfig = (id) =>
  page.evaluate((eid) => {
    const m = window.__rebarModeler;
    const bo = m.get("elementRegistry").get(eid).businessObject;
    const ee = bo.extensionElements;
    const cfg = ee && (ee.values || []).find((v) => v.$type === "rebar:Config");
    return cfg ? cfg.value : null;
  }, id);

// Identify the BATCH service task (rebar:Config carries a `batch` object).
const ids = await page.evaluate(() => {
  const m = window.__rebarModeler;
  const cfgOf = (bo) => {
    const ee = bo.extensionElements;
    const c = ee && (ee.values || []).find((v) => v.$type === "rebar:Config");
    try {
      return c ? JSON.parse(c.value || "{}") : {};
    } catch (e) {
      return {};
    }
  };
  const svc = m
    .get("elementRegistry")
    .filter(
      (e) => e.businessObject && e.businessObject.$type === "bpmn:ServiceTask",
    );
  let batch = null;
  for (const el of svc) {
    const cfg = cfgOf(el.businessObject);
    if (cfg && typeof cfg.batch === "object" && cfg.batch) batch = el.id;
  }
  return { batch };
});

// Ensure-open a group by header text (idempotent — a blind click would toggle it closed).
const ensureGroupOpen = async (groupRe) => {
  await page.evaluate((re) => {
    const rx = new RegExp(re, "i");
    document.querySelectorAll(".bio-properties-panel-group").forEach((g) => {
      const hdr = g.querySelector(".bio-properties-panel-group-header");
      if (!hdr || !rx.test(hdr.textContent)) return;
      const body = g.querySelector(
        ".bio-properties-panel-group-entries, .bio-properties-panel-list",
      );
      if (!body || !body.classList.contains("open")) hdr.click();
    });
  }, groupRe);
  await page.waitForTimeout(250);
};

// Expand a criterion collapsible idempotently — the header click TOGGLES, so only click it
// when the criterion's prompt field is not already visible (re-clicking would close it).
const expandCriterion = async (i) => {
  const sel = `#bio-properties-panel-criterion-${i}-prompt`;
  const visible = await page.evaluate((s) => {
    const el = document.querySelector(s);
    return !!(el && el.offsetParent !== null);
  }, sel);
  if (!visible) {
    await page.click(
      `[data-entry-id="criterion-${i}"] .bio-properties-panel-collapsible-entry-header`,
    );
    await page.waitForTimeout(250);
  }
};

const fillField = async (sel, value) => {
  await page.fill(sel, value);
  await page.dispatchEvent(sel, "input");
  await page.locator(sel).blur();
  await page.waitForTimeout(400);
};

// Select the batch step and open its criteria + authoring groups.
await page.evaluate((eid) => {
  const m = window.__rebarModeler;
  m.get("selection").select(m.get("elementRegistry").get(eid));
}, ids.batch);
await page.waitForTimeout(300);
await ensureGroupOpen("batch criteria");

// ── (a) RENDER: the criterion prompt field is a SELECT of library options ─────────
await expandCriterion(0);
const PROMPT0 = "#bio-properties-panel-criterion-0-prompt";
const promptIsSelect = await page.evaluate(
  (sel) => {
    const el = document.querySelector(sel);
    return el ? el.tagName.toLowerCase() : null;
  },
  PROMPT0,
);
const promptOptions = await page.$$eval(`${PROMPT0} option`, (els) =>
  els.map((e) => e.value),
);

// ── (b) SELECT an existing library criterion → persists into rebar:Config ─────────
await expandCriterion(1);
const PROMPT1 = "#bio-properties-panel-criterion-1-prompt";
await page.selectOption(PROMPT1, "ticket-quality");
await page.waitForTimeout(300);
const configAfterSelect = await readConfig(ids.batch);

// ── (c) NEW TRIGGER from the `when` dropdown → sets the ${{ steps... }} expression ─
const WHEN1 = "#bio-properties-panel-criterion-1-when";
await page.selectOption(WHEN1, "__rebar_new_trigger__");
await page.waitForTimeout(300);
await ensureGroupOpen("new criterion");
await fillField("#bio-properties-panel-rebar-trigger-name", "perf");
await fillField("#bio-properties-panel-rebar-trigger-keywords", "latency, slow");
await page.click("#bio-properties-panel-rebar-trigger-save");
await page.waitForTimeout(400);
const configAfterTrigger = await readConfig(ids.batch);
const triggersConfig = await page.evaluate(() => {
  const m = window.__rebarModeler;
  const els = m
    .get("elementRegistry")
    .filter((e) => e.businessObject && e.businessObject.name === "overlay_triggers");
  if (!els.length) return null;
  const bo = els[0].businessObject;
  const ee = bo.extensionElements;
  const c = ee && (ee.values || []).find((v) => v.$type === "rebar:Config");
  return c ? c.value : null;
});

// ── (d) AUTHOR a NEW criterion via the prompt "➕ Create new…" form ─────────────────
const NEW_ID = "my-new-crit";
await expandCriterion(0);
await page.selectOption(PROMPT0, "__rebar_create__");
await page.waitForTimeout(300);
await ensureGroupOpen("new criterion");
await fillField("#bio-properties-panel-rebar-author-id", NEW_ID);
await fillField(
  "#bio-properties-panel-rebar-author-body",
  "Check the new thing is correct.",
);
await page.click("#bio-properties-panel-rebar-author-save");
await page.waitForTimeout(900);
const authorStatus = await page.evaluate(() => {
  const el = document.querySelector("#bio-properties-panel-rebar-author-status");
  return el ? el.textContent : "";
});
const configAfterAuthor = await readConfig(ids.batch);

// ── SAVE the edited batch to the IR (so the Python side can reload-assert) ─────────
await page.click("#save");
await page.waitForTimeout(800);
const status = await page.evaluate(
  () => document.getElementById("status").textContent,
);

console.log(
  JSON.stringify(
    {
      errors,
      ids,
      promptIsSelect,
      promptOptions,
      configAfterSelect,
      configAfterTrigger,
      triggersConfig,
      newId: NEW_ID,
      authorStatus,
      configAfterAuthor,
      status,
    },
    null,
    2,
  ),
);
await browser.close();
