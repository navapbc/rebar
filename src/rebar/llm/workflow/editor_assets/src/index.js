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

async function save() {
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

open(window.REBAR_DIAGRAM);
