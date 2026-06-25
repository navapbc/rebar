// Browser probe for the v3 `batch` step's visual editing (epic A, story A4): load the editor
// on the batch-demo fixture, select the BATCH ServiceTask, and exercise the criteria-list UI
// the editor must provide:
//   1. RENDER — the Rebar group shows the batch finder; a "Batch criteria" ListGroup renders one
//      collapsible item per criterion, and the security criterion's `when` overlay is visible.
//   2. EDIT   — change criterion-0's prompt id; assert it writes back into rebar:Config.
//   3. ADD    — click the list (+) add button; assert a new criterion appears in the config.
//   4. REMOVE — click a criterion's (×) remove button; assert the config shrinks back.
//   5. OVERLAY — the `if:` predicate field renders for a prompt (agent) step.
//   6. SAVE   — persist an edit to the IR so the Python side can reload-assert.
// Usage: node browser_batch.mjs <editor-url>
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

// Identify the BATCH service task (rebar:Config carries a `batch` object) and the prompt
// (agent) step that carries an `if:` overlay — both are bpmn:ServiceTask.
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
  let overlay = null;
  for (const el of svc) {
    const cfg = cfgOf(el.businessObject);
    if (cfg && typeof cfg.batch === "object" && cfg.batch) batch = el.id;
    else if (cfg && cfg.if) overlay = el.id;
  }
  return { batch, overlay };
});

// Group open-state is keyed by group id and PERSISTS across selections, so a blind header
// click would TOGGLE an already-open group closed. Ensure-open idempotently instead: click a
// matching group's header only when its body is not already open.
const selectAndExpand = async (id, groupRe) => {
  await page.evaluate((eid) => {
    const m = window.__rebarModeler;
    m.get("selection").select(m.get("elementRegistry").get(eid));
  }, id);
  await page.waitForTimeout(300);
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

// ── 1. RENDER ──────────────────────────────────────────────────────────────────
await selectAndExpand(ids.batch, "rebar|batch criteria");
await page.waitForSelector("#bio-properties-panel-rebar-batch-finder", {
  state: "visible",
  timeout: 5000,
});
const finderValue = await page.inputValue(
  "#bio-properties-panel-rebar-batch-finder",
);
const ladderVisible = !!(await page.$(
  "#bio-properties-panel-rebar-batch-ladder",
));
const budgetVisible = !!(await page.$(
  "#bio-properties-panel-rebar-batch-budget",
));

const criteriaItemCount = () =>
  page.evaluate(
    () =>
      document.querySelectorAll(
        '[data-group-id="group-rebar-criteria"] .bio-properties-panel-collapsible-entry',
      ).length,
  );
const itemCountBefore = await criteriaItemCount();

// Expand criterion-1 (security) and read its `when` overlay field.
await page.click(
  '[data-entry-id="criterion-1"] .bio-properties-panel-collapsible-entry-header',
);
await page.waitForTimeout(200);
const whenValue = await page.inputValue(
  "#bio-properties-panel-criterion-1-when",
);

// ── 2. EDIT criterion-0's prompt ─────────────────────────────────────────────────
await page.click(
  '[data-entry-id="criterion-0"] .bio-properties-panel-collapsible-entry-header',
);
await page.waitForTimeout(200);
const PROMPT0 = "#bio-properties-panel-criterion-0-prompt";
await page.fill(PROMPT0, "ticket-quality");
await page.dispatchEvent(PROMPT0, "input");
await page.locator(PROMPT0).blur();
await page.waitForTimeout(300);
const configAfterEdit = await readConfig(ids.batch);

// ── 3. ADD a criterion ───────────────────────────────────────────────────────────
await page.click(
  '[data-group-id="group-rebar-criteria"] .bio-properties-panel-add-entry',
);
await page.waitForTimeout(300);
const configAfterAdd = await readConfig(ids.batch);
const itemCountAfterAdd = await criteriaItemCount();

// ── 4. REMOVE the last criterion ─────────────────────────────────────────────────
await page.evaluate(() => {
  const btns = document.querySelectorAll(
    '[data-group-id="group-rebar-criteria"] .bio-properties-panel-remove-entry',
  );
  if (btns.length) btns[btns.length - 1].click();
});
await page.waitForTimeout(300);
const configAfterRemove = await readConfig(ids.batch);
const itemCountAfterRemove = await criteriaItemCount();

// ── 5. OVERLAY `if:` on a prompt step + CONVERT agent → batch ─────────────────────
await selectAndExpand(ids.overlay, "rebar");
await page.waitForTimeout(200);
const ifFieldPresent = !!(await page.$("#bio-properties-panel-rebar-if"));
const ifValue = ifFieldPresent
  ? await page.inputValue("#bio-properties-panel-rebar-if")
  : "";

// The notify (agent) step exposes the ServiceTask kind toggle; switching it to "batch" must
// seed a cfg.batch (so a freshly-drawn/agent ServiceTask can BECOME a batch step).
const kindTogglePresent = !!(await page.$(
  "#bio-properties-panel-rebar-service-kind",
));
await page.selectOption("#bio-properties-panel-rebar-service-kind", "batch");
await page.waitForTimeout(300);
const overlayConfigAfterConvert = await readConfig(ids.overlay);
// Revert so this step does not pollute the saved IR with an invalid (empty) batch.
await page.selectOption("#bio-properties-panel-rebar-service-kind", "agent");
await page.waitForTimeout(300);
const overlayConfigAfterRevert = await readConfig(ids.overlay);

// Re-select the batch step before saving (so the SAVE persists the batch criterion edit).
await selectAndExpand(ids.batch, "rebar");

// ── 6. SAVE the edited batch to the IR ───────────────────────────────────────────
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
      finderValue,
      ladderVisible,
      budgetVisible,
      itemCountBefore,
      whenValue,
      configAfterEdit,
      configAfterAdd,
      itemCountAfterAdd,
      configAfterRemove,
      itemCountAfterRemove,
      ifFieldPresent,
      ifValue,
      kindTogglePresent,
      overlayConfigAfterConvert,
      overlayConfigAfterRevert,
      status,
    },
    null,
    2,
  ),
);
await browser.close();
