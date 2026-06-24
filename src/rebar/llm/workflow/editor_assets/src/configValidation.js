/**
 * Live config validation + per-kind help (story 998e).
 *
 * Wires the editor to the Python `/validate` endpoint so a step's `<rebar:Config>`
 * JSON is checked against its input contract BEFORE Save, with three DISTINCT states
 * mirrored from the server's defined shape `{ok, errors:[{path,message}], unavailable}`:
 *
 *   - VALID    → the inline error region is cleared and Save is enabled.
 *   - ERRORS   → a red inline error region lists `errors[]` and Save is BLOCKED.
 *   - UNAVAILABLE → a distinct, VISIBLE amber banner ("validation unavailable"); this
 *     fires when the response has `unavailable:true` OR the POST fails / times out /
 *     returns 500. It is NEVER rendered as valid and NEVER silently swallowed, and Save
 *     is BLOCKED (fail-closed) just like for errors.
 *
 * Edits are DEBOUNCED (~300ms) so we don't POST on every keystroke. The per-kind HELP
 * panel is driven by window.REBAR_KIND_HELP (the single Python source of truth, also
 * served by GET /help) so it never drifts from the contracts.
 */

const DEBOUNCE_MS = 300;
const VALIDATE_TIMEOUT_MS = 4000;

const tok = () => window.REBAR_TOKEN || "";

// Map a bpmn business-object to the {kind, action} the /validate endpoint expects.
// kind is the rebar element kind; action is the op id (scripted) / prompt id (agent),
// which round-trips through the element NAME. Control nodes have no action contract.
export function kindAndAction(bo) {
  switch (bo.$type) {
    case "bpmn:ScriptTask":
      return { kind: "scripted", action: bo.name || "" };
    case "bpmn:ServiceTask":
      return { kind: "agent", action: bo.name || "" };
    case "bpmn:ExclusiveGateway":
      return { kind: "branch", action: null };
    case "bpmn:SubProcess": {
      const lc = bo.loopCharacteristics || {};
      if (lc.$type === "bpmn:MultiInstanceLoopCharacteristics")
        return { kind: "map", action: null };
      if (lc.$type === "bpmn:StandardLoopCharacteristics")
        return { kind: "loop", action: null };
      return { kind: "sub-process", action: null };
    }
    default:
      return { kind: bo.$type, action: null };
  }
}

function configValue(bo) {
  const ee = bo.extensionElements;
  const c = ee && (ee.values || []).find((v) => v.$type === "rebar:Config");
  return c ? c.value || "" : "";
}

// POST /validate with a hard timeout. Any failure (network, timeout, non-200, 500,
// unparseable body) is mapped to the UNAVAILABLE state — never a false "valid".
async function postValidate(kind, action, config) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), VALIDATE_TIMEOUT_MS);
  try {
    const r = await fetch("/validate", {
      method: "POST",
      signal: ctrl.signal,
      headers: { "X-Rebar-Token": tok(), "Content-Type": "application/json" },
      body: JSON.stringify({ kind, action, config }),
    });
    const body = await r.json().catch(() => null);
    if (!r.ok || !body || typeof body.ok !== "boolean") {
      return { ok: false, unavailable: true, errors: body && body.errors ? body.errors : [] };
    }
    return body;
  } catch (e) {
    return {
      ok: false,
      unavailable: true,
      errors: [{ path: "", message: String((e && e.message) || e) }],
    };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Mount the validation wiring + help panel. Returns a small controller exposing the
 * current state (used by the Save gate and tests).
 */
export function mountConfigValidation(modeler, hostDoc) {
  const doc = hostDoc || document;
  // state: "valid" | "errors" | "unavailable" — Save is blocked unless "valid".
  const state = { value: "valid", errors: [] };

  const region = doc.createElement("div");
  region.id = "rebar-validate-region";
  region.style.display = "none";

  // The error / unavailable region lives inside the properties panel so it is adjacent
  // to the config textarea the author is editing.
  const mount = () => {
    const props = doc.getElementById("properties");
    if (props && region.parentNode !== props) props.insertBefore(region, props.firstChild);
  };

  function render() {
    mount();
    if (state.value === "valid") {
      region.style.display = "none";
      region.className = "";
      region.textContent = "";
    } else if (state.value === "unavailable") {
      region.style.display = "block";
      region.className = "rebar-validate-unavailable";
      region.textContent =
        "⚠ validation unavailable — the validator could not be reached or errored. " +
        "Save is blocked until validation succeeds.";
    } else {
      region.style.display = "block";
      region.className = "rebar-validate-errors";
      const lines = (state.errors || []).map((e) => {
        const p = e.path ? `${e.path}: ` : "";
        return `• ${p}${e.message}`;
      });
      region.textContent = "Config errors (fix before Save):\n" + lines.join("\n");
    }
    updateSaveGate();
  }

  function updateSaveGate() {
    const btn = doc.getElementById("save");
    if (btn) btn.disabled = state.value !== "valid";
  }

  function setResult(res) {
    if (res.unavailable) state.value = "unavailable";
    else if (res.ok) state.value = "valid";
    else state.value = "errors";
    state.errors = res.errors || [];
    render();
  }

  let timer = null;
  let seq = 0;
  function scheduleValidate(bo) {
    if (timer) clearTimeout(timer);
    const { kind, action } = kindAndAction(bo);
    const config = configValue(bo);
    const mySeq = ++seq;
    timer = setTimeout(async () => {
      const res = await postValidate(kind, action, config);
      if (mySeq === seq) setResult(res); // ignore a stale in-flight response
    }, DEBOUNCE_MS);
  }

  // Re-validate whenever the selection changes or any element property changes (the
  // config textarea writes through modeling, so this fires on every debounced edit).
  let current = null;
  try {
    const selection = modeler.get("selection");
    const eventBus = modeler.get("eventBus");
    eventBus.on("selection.changed", (e) => {
      const el = (e.newSelection || [])[0];
      current = el || null;
      if (current && current.businessObject) scheduleValidate(current.businessObject);
      else {
        state.value = "valid";
        state.errors = [];
        render();
      }
    });
    eventBus.on("elements.changed", () => {
      const el = (selection.get() || [])[0];
      if (el && el.businessObject) {
        current = el;
        scheduleValidate(el.businessObject);
      }
    });
  } catch (e) {
    // No modeler services (unit/Node harness): the controller still works via validateNow.
  }

  const controller = {
    state,
    render,
    setResult, // test hook: feed a server result directly
    validateNow: async (bo) => {
      const { kind, action } = kindAndAction(bo);
      const res = await postValidate(kind, action, configValue(bo));
      setResult(res);
      return res;
    },
    isSaveBlocked: () => state.value !== "valid",
  };
  render();
  return controller;
}

// Render the per-kind HELP panel from window.REBAR_KIND_HELP (single source of truth).
export function renderKindHelp(container, help) {
  const data = help || window.REBAR_KIND_HELP || {};
  const doc = container.ownerDocument || document;
  const wrap = doc.createElement("div");
  wrap.id = "rebar-kind-help";
  const h = doc.createElement("h3");
  h.textContent = "Element types & config shapes";
  wrap.appendChild(h);
  for (const kind of Object.keys(data)) {
    const info = data[kind] || {};
    const block = doc.createElement("div");
    block.className = "rebar-help-kind";
    const title = doc.createElement("strong");
    title.textContent = `${kind} — ${info.title || ""}`;
    block.appendChild(title);
    if (info.summary) {
      const s = doc.createElement("div");
      s.className = "rebar-help-summary";
      s.textContent = info.summary;
      block.appendChild(s);
    }
    const pre = doc.createElement("pre");
    pre.className = "rebar-help-shape";
    pre.textContent = JSON.stringify(info.shape || {}, null, 2);
    block.appendChild(pre);
    wrap.appendChild(block);
  }
  container.appendChild(wrap);
  return wrap;
}
