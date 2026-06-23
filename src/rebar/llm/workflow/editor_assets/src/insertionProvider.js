/**
 * Typed insertion for the workflow editor (story 6592).
 *
 * MECHANISM: a custom **bpmn-js context-pad provider** plus a **palette provider**.
 * Both let the user insert a new step by picking EITHER a scripted op OR a prompt from
 * a typed, category-grouped chooser; the chosen entry creates a real bpmn shape whose
 * NAME is the action (the editor's name==action convention, see rebarProvider.jsx), so
 * it round-trips to a valid `uses:` (scripted) or `prompt:` (agent) IR step on Save:
 *
 *   - a SCRIPTED op  → a `bpmn:ScriptTask`  whose name is the op id → `uses: <op>`
 *   - a PROMPT       → a `bpmn:ServiceTask` whose name is the prompt id → `prompt: <id>`
 *
 * The op list comes from `window.REBAR_CONTRACTS` (the scripted ops with declared
 * contracts) and the prompt list from the `/prompts` endpoint (built-in + project),
 * both grouped by the CLOSED category vocabulary (review/verifier/transform/code/
 * exploration). The actual chooser UI lives in the side panel (promptLibrary.js); the
 * context-pad/palette entries open it pre-filtered to "insert" mode so insertion is
 * reachable from the canvas too.
 */

const LOW_PRIORITY = 900;

// Create + place a typed step shape. `kind` is "script" (scripted `uses:`) or
// "service" (agent `prompt:`); `name` is the op id / prompt id (the action).
export function createTypedStep(modeler, kind, name) {
  const elementFactory = modeler.get("elementFactory");
  const create = modeler.get("create");
  const bpmnType = kind === "service" ? "bpmn:ServiceTask" : "bpmn:ScriptTask";
  const shape = elementFactory.createShape({ type: bpmnType });
  // Set the action NAME up front so the round-trip emits the right uses:/prompt:.
  shape.businessObject.name = name || "";
  return shape;
}

// Programmatic insert (used by the side-panel "Insert" button and the e2e harness):
// drop a typed step at a sensible spot on the root and select it.
export function insertTypedStep(modeler, kind, name) {
  const elementFactory = modeler.get("elementFactory");
  const modeling = modeler.get("modeling");
  const canvas = modeler.get("canvas");
  const selection = modeler.get("selection");
  const root = canvas.getRootElement();
  const bpmnType = kind === "service" ? "bpmn:ServiceTask" : "bpmn:ScriptTask";
  const shape = modeling.createShape(
    { type: bpmnType, name: name || "" },
    { x: 200, y: 200 },
    root,
  );
  if (shape.businessObject) shape.businessObject.name = name || "";
  selection.select(shape);
  return shape;
}

class RebarInsertionProvider {
  constructor(contextPad, palette, create, elementFactory, translate) {
    this._create = create;
    this._elementFactory = elementFactory;
    this._translate = translate;
    contextPad.registerProvider(LOW_PRIORITY, this);
    palette.registerProvider(LOW_PRIORITY, this);
  }

  _start(kind, name) {
    const self = this;
    return function (event) {
      const shape = self._elementFactory.createShape({
        type: kind === "service" ? "bpmn:ServiceTask" : "bpmn:ScriptTask",
      });
      shape.businessObject.name = name || "";
      self._create.start(event, shape);
    };
  }

  // Palette entries: open the side-panel chooser (the typed, grouped picker). We keep
  // the heavy category list in the panel and expose two quick-create palette actions.
  getPaletteEntries() {
    const start = (kind) => (event) => {
      const shape = this._elementFactory.createShape({
        type: kind === "service" ? "bpmn:ServiceTask" : "bpmn:ScriptTask",
      });
      this._create.start(event, shape);
    };
    return {
      "rebar-insert-script": {
        group: "rebar",
        className: "bpmn-icon-script-task",
        title: "Insert scripted op (uses:)",
        action: { dragstart: start("script"), click: () => window.__rebarOpenInsert?.("script") },
      },
      "rebar-insert-prompt": {
        group: "rebar",
        className: "bpmn-icon-service-task",
        title: "Insert prompt step (prompt:)",
        action: { dragstart: start("service"), click: () => window.__rebarOpenInsert?.("service") },
      },
    };
  }

  getContextPadEntries() {
    return {
      "rebar-add-script": {
        group: "rebar",
        className: "bpmn-icon-script-task",
        title: "Add scripted op (uses:)",
        action: { click: () => window.__rebarOpenInsert?.("script") },
      },
      "rebar-add-prompt": {
        group: "rebar",
        className: "bpmn-icon-service-task",
        title: "Add prompt step (prompt:)",
        action: { click: () => window.__rebarOpenInsert?.("service") },
      },
    };
  }
}
RebarInsertionProvider.$inject = [
  "contextPad",
  "palette",
  "create",
  "elementFactory",
  "translate",
];

export default {
  __init__: ["rebarInsertionProvider"],
  rebarInsertionProvider: ["type", RebarInsertionProvider],
};
