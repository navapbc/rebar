/**
 * rebar workflow visual editor — the browser app.
 *
 * A bpmn-js *Modeler* (its palette is BPMN-only, so edits stay inside a metamodel that
 * maps back to the rebar IR) wired with three bpmn-io pieces so a human can actually
 * READ and EDIT a workflow:
 *
 *   - bpmn-auto-layout : on open we DISCARD any incoming DI and lay the diagram out
 *     fresh, left-to-right, so parallel steps get their own rows and edges dock to node
 *     edges (fixes the "everything on one row / arrows through the text" problem — the
 *     Python serializer no longer hand-rolls geometry).
 *   - bpmn-js-properties-panel + a custom Rebar provider : a side panel that shows each
 *     step's kind and its rebar config (the `<rebar:Config>` JSON, the agent prompt, the
 *     branch condition, loop bounds, …) and lets the user EDIT it — previously invisible.
 *   - the rebar moddle extension : so `<rebar:Config>`/`<rebar:Agent>` survive save.
 *
 * Save serializes back to BPMN and POSTs it to `/save` with the per-session token; the
 * Python side round-trips it to the IR (the visual format is never written to git).
 *
 * The host page injects three globals: REBAR_DIAGRAM (BPMN xml), REBAR_TOKEN, and
 * REBAR_MODDLE (the descriptor).
 */
import BpmnModeler from "bpmn-js/lib/Modeler";
import {
  BpmnPropertiesPanelModule,
  BpmnPropertiesProviderModule,
} from "bpmn-js-properties-panel";

import rebarPropertiesProviderModule from "./rebarProvider";
import rebarInsertionProviderModule from "./insertionProvider";
import { mountPromptLibrary } from "./promptLibrary";
import { mountConfigValidation, renderKindHelp } from "./configValidation";

import "bpmn-js/dist/assets/diagram-js.css";
import "bpmn-js/dist/assets/bpmn-js.css";
import "bpmn-js/dist/assets/bpmn-font/css/bpmn.css";
import "@bpmn-io/properties-panel/dist/assets/properties-panel.css";

const status = (msg, cls) => {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = cls || "";
};

const modeler = new BpmnModeler({
  container: "#canvas",
  propertiesPanel: { parent: "#properties" },
  additionalModules: [
    BpmnPropertiesPanelModule,
    BpmnPropertiesProviderModule,
    rebarPropertiesProviderModule,
    rebarInsertionProviderModule,
  ],
  moddleExtensions: { rebar: window.REBAR_MODDLE },
});

async function open(xml) {
  // The Python serializer ships a ready-to-render layout (layered left-to-right, edges
  // docked to node edges, sub-processes expanded inline), so we import it directly.
  try {
    await modeler.importXML(xml);
    modeler.get("canvas").zoom("fit-viewport");
    status("ready — edits stay in the BPMN metamodel; only the IR file is written");
  } catch (e) {
    status("open failed: " + e.message, "err");
  }
}

// Live config validation (debounced /validate round-trip) + Save-blocking gate.
const validation = mountConfigValidation(modeler);
// Test hook: expose the validation controller so the E2E / Node harness can assert the
// error / unavailable states and the Save gate without driving a real keyboard.
window.__rebarValidation = validation;

async function save() {
  // Fail-closed Save: refuse client-side while validation errors exist OR while in the
  // 'unavailable' state, mirroring the disabled Save button (defense in depth).
  if (validation.isSaveBlocked()) {
    status("save blocked: fix config validation first", "err");
    return;
  }
  try {
    const { xml } = await modeler.saveXML({ format: true });
    const r = await fetch("/save", {
      method: "POST",
      body: xml,
      headers: { "X-Rebar-Token": window.REBAR_TOKEN },
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) status("saved to IR", "ok");
    else status("rejected: " + (body.errors || ["unknown error"]).join("; "), "err");
  } catch (e) {
    status("save failed: " + e.message, "err");
  }
}

document.getElementById("save").addEventListener("click", save);

// Test hook: expose the modeler so the headless browser E2E can drive selection /
// inspect state deterministically (harmless in normal use).
window.__rebarModeler = modeler;

// Mount the prompt LIBRARY + create/edit + typed INSERTION panel (story 6592) into its
// own container, if the host page provided one.
const libContainer = document.getElementById("rebar-library");
if (libContainer) {
  try {
    mountPromptLibrary(modeler, libContainer);
  } catch (e) {
    console.error("prompt library failed to mount:", e);
  }
  // The per-kind HELP panel: element types + expected JSON shape per kind, driven by
  // window.REBAR_KIND_HELP (the single Python source of truth, also served by /help).
  try {
    renderKindHelp(libContainer);
  } catch (e) {
    console.error("kind help panel failed to render:", e);
  }
}

open(window.REBAR_DIAGRAM);
