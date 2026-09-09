/**
 * Typed context-pad and palette insertion. The category-grouped chooser maps scripted
 * ops to named ScriptTasks (`uses:`) and prompts to named ServiceTasks (`prompt:`), so
 * both round-trip through the IR. Canvas entries open the side-panel chooser.
 */

const LOW_PRIORITY = 900;

// Create + place a typed step shape. `kind` is "script" (scripted `uses:`) or
// "service" (agent `prompt:`); `name` is the op id / prompt id (the action).
export function createTypedStep(modeler, kind, name) {
  const elementFactory = modeler.get("elementFactory");
  const bpmnType = kind === "service" ? "bpmn:ServiceTask" : "bpmn:ScriptTask";
  const shape = elementFactory.createShape({ type: bpmnType });
  // Set the action NAME up front so the round-trip emits the right uses:/prompt:.
  shape.businessObject.name = name || "";
  return shape;
}

// Leaf kinds round-trip through NAME == action. Structural kinds are completed in the
// panel; batch starts as a ServiceTask with a recognizable rebar:Config seed.
const KIND_BPMN_TYPE = {
  script: "bpmn:ScriptTask",
  service: "bpmn:ServiceTask",
  batch: "bpmn:ServiceTask",
  branch: "bpmn:ExclusiveGateway",
  loop: "bpmn:SubProcess",
  map: "bpmn:SubProcess",
};

// Whether a kind carries an action NAME (a scripted op id / prompt id). The structural kinds
// (branch/loop/map/batch) do not — their config is edited in the panel after insert.
const KIND_HAS_NAME = { script: true, service: true };

// Insert on the root, seed structural config, and select the new step so its typed
// editor opens immediately. Used by the side panel and E2E harness.
export function insertTypedStep(modeler, kind, name) {
  const modeling = modeler.get("modeling");
  const bpmnFactory = modeler.get("bpmnFactory");
  const canvas = modeler.get("canvas");
  const selection = modeler.get("selection");
  const root = canvas.getRootElement();
  const bpmnType = KIND_BPMN_TYPE[kind] || "bpmn:ScriptTask";
  const stepName = KIND_HAS_NAME[kind] ? name || "" : "";
  const shape = modeling.createShape(
    { type: bpmnType, name: stepName },
    { x: 200, y: 200 },
    root,
  );
  if (shape.businessObject) shape.businessObject.name = stepName;

  // Seed the structural shape so rebarKind() classifies it correctly on selection.
  if (kind === "loop" || kind === "map") {
    const lcType =
      kind === "map"
        ? "bpmn:MultiInstanceLoopCharacteristics"
        : "bpmn:StandardLoopCharacteristics";
    const lc = bpmnFactory.create(lcType);
    lc.$parent = shape.businessObject;
    modeling.updateProperties(shape, { loopCharacteristics: lc });
  } else if (kind === "batch") {
    seedBatchConfig(modeling, bpmnFactory, shape);
  }

  selection.select(shape);
  return shape;
}

// Seed a freshly-inserted batch ServiceTask with an (invalid-until-filled) rebar:Config
// `batch` object, so the editor recognizes it as a batch step and shows the batch fields.
function seedBatchConfig(modeling, bpmnFactory, shape) {
  const bo = shape.businessObject;
  let ee = bo.extensionElements;
  if (!ee) {
    ee = bpmnFactory.create("bpmn:ExtensionElements", { values: [] });
    ee.$parent = bo;
    modeling.updateProperties(shape, { extensionElements: ee });
  }
  const value = JSON.stringify({ batch: { prompt: "", criteria: [] } });
  const cfg = bpmnFactory.create("rebar:Config", { value });
  cfg.$parent = ee;
  modeling.updateModdleProperties(shape, ee, {
    values: [...(ee.values || []), cfg],
  });
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
