/**
 * The prompt LIBRARY + CREATE/EDIT side panel and the typed INSERTION chooser (6592).
 *
 * A vanilla-DOM panel (no extra framework) mounted next to the canvas. It:
 *   - lists built-in + project prompts from GET /prompts, grouped by the CLOSED
 *     category vocabulary (review/verifier/transform/code/exploration), in a dropdown;
 *   - shows the typed INSERTION chooser: pick a scripted op (from window.REBAR_CONTRACTS)
 *     or a prompt (from /prompts), grouped by category, and insert a valid uses:/prompt:
 *     step (via insertTypedStep);
 *   - a CREATE/EDIT form (id + front-matter fields + a body textarea) that on Save POSTs
 *     to /prompt/save, SHOWS the resolved write target path (from GET /prompt?id= or the
 *     fresh /prompt?id= probe) BEFORE saving, and surfaces server errors (collision /
 *     invalid / neither-writable) inline;
 *   - guards discard-of-unsaved-edits (confirm) and empty/invalid id client-side.
 *
 * All fetches carry the per-session token (window.REBAR_TOKEN) the host page injected.
 */
import { insertTypedStep } from "./insertionProvider";

const CATEGORIES = ["review", "verifier", "transform", "code", "exploration"];
const ID_RE = /^[a-z0-9][a-z0-9-]*$/;

const tok = () => window.REBAR_TOKEN || "";

async function api(path, opts = {}) {
  const headers = Object.assign({ "X-Rebar-Token": tok() }, opts.headers || {});
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  const body = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, body };
}

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const kid of kids) n.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
  return n;
}

// The scripted ops with declared contracts (window.REBAR_CONTRACTS is keyed by op/prompt
// name → contract view). We can't reliably split ops from prompts there, so the op list
// is best-effort: any contract whose name isn't a known prompt id is treated as an op.
function scriptedOps(promptIds) {
  const all = Object.keys(window.REBAR_CONTRACTS || {});
  return all.filter((name) => !promptIds.has(name));
}

export function mountPromptLibrary(modeler, container) {
  let prompts = [];
  let dirty = false; // unsaved-edit guard

  const panel = el("div", { class: "rebar-lib", id: "rebar-lib" });
  container.appendChild(panel);

  const status = el("div", { class: "rebar-lib-status", id: "rebar-lib-status" });
  const targetLine = el("div", { class: "rebar-lib-target", id: "rebar-lib-target" });

  // The create/edit form is on-demand (item 11): hidden until "New prompt" or loading a
  // library prompt reveals it, so it isn't shown persistently next to the "New prompt" button.
  const editForm = el("div", { class: "rebar-edit-form", id: "rebar-edit-form" });
  editForm.style.display = "none";
  function showEditForm(show) {
    editForm.style.display = show ? "" : "none";
  }

  // ── INSERTION chooser (on-demand: revealed by the "Add step" button) ─────────
  // The structural kinds (branch/loop/map/batch) carry no action name — their config is
  // edited in the panel after insert — so the "choice" dropdown only applies to the leaf
  // kinds (prompt/scripted op). NAMELESS_KINDS drives hiding the choice select for them.
  const NAMELESS_KINDS = new Set(["branch", "loop", "map", "batch"]);
  const insertKind = el("select", { id: "rebar-insert-kind" });
  insertKind.appendChild(el("option", { value: "service" }, "prompt (prompt:)"));
  insertKind.appendChild(el("option", { value: "script" }, "scripted op (uses:)"));
  insertKind.appendChild(el("option", { value: "batch" }, "batch (finder + criteria)"));
  insertKind.appendChild(el("option", { value: "branch" }, "branch (conditional)"));
  insertKind.appendChild(el("option", { value: "loop" }, "loop"));
  insertKind.appendChild(el("option", { value: "map" }, "map (fan-out)"));
  const insertChoice = el("select", { id: "rebar-insert-choice" });
  const insertBtn = el("button", { id: "rebar-insert-btn", type: "button" }, "Insert step");
  const insertStatus = el("div", { class: "rebar-lib-status", id: "rebar-insert-status" });

  function setInsertStatus(msg, cls) {
    insertStatus.textContent = msg;
    insertStatus.className = "rebar-lib-status " + (cls || "");
  }

  function refreshInsertChoices() {
    insertChoice.textContent = "";
    const kind = insertKind.value;
    // Structural kinds need no op/prompt choice — hide the choice select for them.
    if (NAMELESS_KINDS.has(kind)) {
      insertChoice.style.display = "none";
      return;
    }
    insertChoice.style.display = "";
    const promptIds = new Set(prompts.map((p) => p.id));
    if (kind === "service") {
      // prompts grouped by category
      for (const cat of [...CATEGORIES, "uncategorized"]) {
        const group = prompts.filter((p) => (p.category || "uncategorized") === cat);
        if (!group.length) continue;
        const og = el("optgroup", { label: cat });
        for (const p of group) og.appendChild(el("option", { value: p.id }, `${p.id}${p.title ? " — " + p.title : ""}`));
        insertChoice.appendChild(og);
      }
    } else {
      const ops = scriptedOps(promptIds);
      const og = el("optgroup", { label: "scripted ops" });
      for (const op of ops) og.appendChild(el("option", { value: op }, op));
      insertChoice.appendChild(og.children.length ? og : el("option", { value: "" }, "(no ops)"));
    }
  }
  insertKind.addEventListener("change", refreshInsertChoices);
  insertBtn.addEventListener("click", () => {
    const kind = insertKind.value;
    const structural = NAMELESS_KINDS.has(kind);
    const name = structural ? "" : insertChoice.value;
    if (!structural && !name) return;
    // insertTypedStep selects the new element, so its edit panel opens for further fields.
    insertTypedStep(modeler, kind, name);
    const label = name ? ` "${name}"` : "";
    setInsertStatus(`inserted ${kind} step${label} — edit it in the panel`, "ok");
  });

  // ── "Add step" toggle: the insert chooser is on-demand, not persistently shown ───
  const addStepBtn = el("button", { id: "rebar-add-step", type: "button" }, "➕ Add step");
  const insertPanel = el("div", { class: "rebar-insert-panel", id: "rebar-insert-panel" });
  insertPanel.style.display = "none";
  function showInsert(show) {
    insertPanel.style.display = show ? "" : "none";
    addStepBtn.textContent = show ? "✕ Cancel add step" : "➕ Add step";
    if (show) refreshInsertChoices();
  }
  addStepBtn.addEventListener("click", () =>
    showInsert(insertPanel.style.display === "none"),
  );
  // Selecting an element on the canvas reverts to the normal edit panel (hide insert).
  try {
    modeler.get("eventBus").on("selection.changed", (e) => {
      if (e && e.newSelection && e.newSelection.length) showInsert(false);
    });
  } catch (err) {
    /* eventBus unavailable in a degraded mount — the toggle still works manually */
  }

  // A canvas affordance (context-pad/palette) calls this to open insert mode.
  window.__rebarOpenInsert = (kind) => {
    insertKind.value = kind === "service" ? "service" : "script";
    showInsert(true);
    panel.scrollIntoView({ block: "start" });
  };

  // ── LIBRARY dropdown + EDIT form ─────────────────────────────────────────────
  const libSelect = el("select", { id: "rebar-lib-select" });
  const idInput = el("input", { id: "rebar-prompt-id", placeholder: "prompt-id" });
  const titleInput = el("input", { id: "rebar-prompt-title", placeholder: "title" });
  const catSelect = el("select", { id: "rebar-prompt-category" });
  for (const c of ["", ...CATEGORIES]) catSelect.appendChild(el("option", { value: c }, c || "(none)"));
  const descInput = el("input", { id: "rebar-prompt-description", placeholder: "description" });
  const bodyArea = el("textarea", { id: "rebar-prompt-body", rows: "10", placeholder: "prompt body…" });
  const overwriteBox = el("input", { id: "rebar-prompt-overwrite", type: "checkbox" });
  const newBtn = el("button", { id: "rebar-prompt-new", type: "button" }, "New prompt");
  const saveBtn = el("button", { id: "rebar-prompt-save", type: "button" }, "Save prompt");
  // Inline live id-collision notice (story B-UX item 13): shown when the typed id already
  // exists in the library, BEFORE save, so the author isn't surprised by a save rejection.
  const idCheck = el("div", { class: "rebar-lib-idcheck", id: "rebar-prompt-idcheck" });

  for (const n of [idInput, titleInput, descInput, bodyArea]) n.addEventListener("input", () => (dirty = true));

  // The set of existing library/prompt ids, for the live collision check. Prefer the freshest
  // source: window.REBAR_LIBRARY (re-fetched after authoring) unioned with the listed prompts.
  function knownIds() {
    const ids = new Set();
    const lib = Array.isArray(window.REBAR_LIBRARY) ? window.REBAR_LIBRARY : [];
    for (const e of lib) if (e && e.id) ids.add(e.id);
    for (const p of prompts) if (p && p.id) ids.add(p.id);
    return ids;
  }

  // Re-evaluate the id-collision notice. A collision with "overwrite" OFF is flagged as an
  // error (save would be rejected); with "overwrite" ON it's a neutral "will update" note.
  function refreshIdCheck() {
    const id = idInput.value.trim();
    if (!id) return (idCheck.textContent = "");
    if (!ID_RE.test(id)) {
      idCheck.textContent = "invalid id: lowercase letters/digits/dashes, not starting with a dash";
      idCheck.className = "rebar-lib-idcheck err";
      return;
    }
    if (knownIds().has(id)) {
      if (overwriteBox.checked) {
        idCheck.textContent = `“${id}” already exists — saving will UPDATE it`;
        idCheck.className = "rebar-lib-idcheck";
      } else {
        idCheck.textContent = `“${id}” already exists — enable “overwrite if exists” to update`;
        idCheck.className = "rebar-lib-idcheck err";
      }
    } else {
      idCheck.textContent = `“${id}” is available`;
      idCheck.className = "rebar-lib-idcheck ok";
    }
  }
  idInput.addEventListener("input", refreshIdCheck);
  overwriteBox.addEventListener("change", refreshIdCheck);

  function setStatus(msg, cls) {
    status.textContent = msg;
    status.className = "rebar-lib-status " + (cls || "");
  }

  async function showTarget(id) {
    if (!id) return targetLine.textContent = "";
    const { body } = await api("/prompt?id=" + encodeURIComponent(id));
    const t = body && body.target;
    targetLine.textContent = t ? `→ writes to: ${t.path || "(nowhere)"} [${t.kind}]` : "";
  }
  idInput.addEventListener("change", () => showTarget(idInput.value.trim()));

  function maybeDiscard() {
    return !dirty || window.confirm("Discard unsaved prompt edits?");
  }

  async function loadInto(id) {
    if (!maybeDiscard()) {
      libSelect.value = idInput.value; // revert the dropdown
      return;
    }
    const { ok, body } = await api("/prompt?id=" + encodeURIComponent(id));
    if (!ok) return setStatus("could not load prompt: " + ((body.errors || []).join("; ")), "err");
    idInput.value = body.id;
    const m = body.meta || {};
    titleInput.value = m.title || "";
    catSelect.value = m.category || "";
    descInput.value = m.description || "";
    bodyArea.value = body.text || "";
    overwriteBox.checked = true; // editing an existing prompt → overwrite
    targetLine.textContent = body.target ? `→ writes to: ${body.target.path} [${body.target.kind}]` : "";
    dirty = false;
    showEditForm(true); // loading a prompt reveals the (otherwise hidden) edit form
    refreshIdCheck();
    setStatus(`loaded "${id}"`, "ok");
  }
  libSelect.addEventListener("change", () => libSelect.value && loadInto(libSelect.value));

  newBtn.addEventListener("click", () => {
    if (!maybeDiscard()) return;
    idInput.value = "";
    titleInput.value = "";
    catSelect.value = "";
    descInput.value = "";
    bodyArea.value = "";
    overwriteBox.checked = false;
    targetLine.textContent = "";
    idCheck.textContent = "";
    dirty = false;
    showEditForm(true); // "New prompt" reveals the form (it is not persistently shown)
    setStatus("new prompt — fill in id + body", "");
  });

  saveBtn.addEventListener("click", async () => {
    const id = idInput.value.trim();
    if (!ID_RE.test(id)) return setStatus("invalid id: lowercase letters/digits/dashes, not starting with a dash", "err");
    if (!bodyArea.value) return setStatus("body is empty", "err");
    const meta = {};
    if (titleInput.value) meta.title = titleInput.value;
    if (catSelect.value) meta.category = catSelect.value;
    if (descInput.value) meta.description = descInput.value;
    const { ok, body } = await api("/prompt/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, meta, body: bodyArea.value, overwrite: overwriteBox.checked }),
    });
    if (!ok) return setStatus("save rejected: " + ((body.errors || ["unknown error"]).join("; ")), "err");
    dirty = false;
    setStatus(`saved "${id}" → ${body.path} [${body.kind}]`, "ok");
    await refresh(); // pick up the new/edited prompt in the library + insert chooser
  });

  async function refresh() {
    const { ok, body } = await api("/prompts");
    if (!ok) return setStatus("could not list prompts", "err");
    prompts = Array.isArray(body) ? body : [];
    // Rebuild the library dropdown, grouped by category.
    libSelect.textContent = "";
    libSelect.appendChild(el("option", { value: "" }, "— select a prompt —"));
    for (const cat of [...CATEGORIES, "uncategorized"]) {
      const group = prompts.filter((p) => (p.category || "uncategorized") === cat);
      if (!group.length) continue;
      const og = el("optgroup", { label: cat });
      for (const p of group) {
        const mark = p.source === "project" ? " (project)" : "";
        og.appendChild(el("option", { value: p.id }, `${p.id}${mark}`));
      }
      libSelect.appendChild(og);
    }
    refreshInsertChoices();
  }

  // ── Layout ───────────────────────────────────────────────────────────────────
  // INSERT: an on-demand panel (item 5). The "Add step" button toggles it; selecting an
  // element on the canvas reverts to the normal edit panel (handled by selection.changed).
  insertPanel.appendChild(insertKind);
  insertPanel.appendChild(insertChoice);
  insertPanel.appendChild(insertBtn);
  panel.appendChild(addStepBtn);
  panel.appendChild(insertPanel);
  // The insert status sits with the insert control (item 17) — OUTSIDE the insert panel, so
  // it survives the panel auto-hiding when inserting selects the new element (item 5).
  panel.appendChild(insertStatus);
  panel.appendChild(el("hr"));

  // LIBRARY: pick a prompt (loads it into the edit form) or start a new one.
  panel.appendChild(el("h3", {}, "Prompt library"));
  panel.appendChild(libSelect);
  panel.appendChild(newBtn);

  // CREATE / EDIT: a single on-demand form (item 11) revealed by "New prompt" or by loading a
  // library prompt — not persistently shown alongside the "New prompt" button.
  editForm.appendChild(el("hr"));
  editForm.appendChild(el("h3", {}, "Create / edit prompt"));
  editForm.appendChild(el("label", {}, "id ", idInput));
  editForm.appendChild(idCheck);
  editForm.appendChild(el("label", {}, "title ", titleInput));
  editForm.appendChild(el("label", {}, "category ", catSelect));
  editForm.appendChild(el("label", {}, "description ", descInput));
  editForm.appendChild(bodyArea);
  editForm.appendChild(
    el("label", { class: "rebar-overwrite-label" }, overwriteBox, " overwrite if exists"),
  );
  editForm.appendChild(saveBtn);
  editForm.appendChild(targetLine);
  panel.appendChild(editForm);
  panel.appendChild(status);

  // Test hook: expose the panel API so the e2e/Node check can drive it deterministically.
  window.__rebarPromptLibrary = {
    refresh,
    insert: (k, n) => insertTypedStep(modeler, k, n),
    getPrompts: () => prompts,
    showInsert,
    showEditForm,
  };

  refresh();
  return panel;
}
