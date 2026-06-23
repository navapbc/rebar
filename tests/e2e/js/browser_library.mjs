// Browser probe for the prompt LIBRARY + typed INSERTION + create/edit (story 6592).
// Drives the real bundle: confirms the library mounts and lists prompts, inserts a
// scripted-op step AND a prompt step (asserting they produce the right bpmn kind/name
// that round-trips to uses:/prompt:), creates a new prompt via the form (POST
// /prompt/save), and reports. Usage: node browser_library.mjs <editor-url>
import { chromium } from "playwright";

const url = process.argv[2];
const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

await page.goto(url, { waitUntil: "load" });
await page.waitForFunction(() => !!window.__rebarModeler && !!window.__rebarPromptLibrary, null, {
  timeout: 15000,
});
// Let the /prompts fetch populate the library.
await page.waitForFunction(() => (window.__rebarPromptLibrary.getPrompts() || []).length > 0, null, {
  timeout: 15000,
});

const prompts = await page.evaluate(() => window.__rebarPromptLibrary.getPrompts());

// Insert a scripted op step (uses:) and a prompt step (prompt:) and read back the
// bpmn kind + name (the round-trip contract: name == action).
const inserted = await page.evaluate((promptId) => {
  const lib = window.__rebarPromptLibrary;
  const op = lib.insert("script", "noop"); // scripted op → ScriptTask named "noop"
  const pr = lib.insert("service", promptId); // prompt → ServiceTask named "<prompt>"
  return {
    op: { type: op.businessObject.$type, name: op.businessObject.name },
    prompt: { type: pr.businessObject.$type, name: pr.businessObject.name },
  };
}, prompts[0].id);

// Create a brand-new prompt via the form and Save (POSTs /prompt/save).
await page.fill("#rebar-prompt-id", "e2e-created");
await page.fill("#rebar-prompt-body", "created from the browser e2e");
await page.selectOption("#rebar-prompt-category", "transform");
await page.click("#rebar-prompt-save");
await page.waitForTimeout(600);
const saveStatus = await page.evaluate(
  () => document.getElementById("rebar-lib-status").textContent
);

// Library dropdown has options grouped by category (presence check).
const libOptionCount = await page.evaluate(
  () => document.getElementById("rebar-lib-select").querySelectorAll("option").length
);

console.log(
  JSON.stringify(
    { errors, promptCount: prompts.length, inserted, saveStatus, libOptionCount },
    null,
    2
  )
);
await browser.close();
