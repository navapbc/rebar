/**
 * Custom properties-panel provider: a "Rebar" group that surfaces what bpmn-js's stock
 * panel can't — the step's rebar KIND and its `<rebar:Config>` payload (the with/mode/
 * model/loop-bounds/branch-condition JSON). It is shown for the element types that carry
 * rebar semantics, and the config is editable in place, so a human can both READ what a
 * step does and CHANGE it (or fill one in for a freshly-drawn step) before Save.
 *
 * The config is presented as JSON (one textarea) deliberately: it is the exact payload
 * the Python round-trip reads back from `<rebar:Config>`, so what you edit is what gets
 * written to the IR — no lossy field mapping in between. Structured per-field entries can
 * layer on later without changing the round-trip contract.
 */
import {
  TextAreaEntry,
  TextFieldEntry,
  isTextAreaEntryEdited,
} from "@bpmn-io/properties-panel";
import { useService } from "bpmn-js-properties-panel";

const LOW_PRIORITY = 500;
const REBAR_KINDS = [
  "bpmn:ScriptTask",
  "bpmn:ServiceTask",
  "bpmn:ExclusiveGateway",
  "bpmn:SubProcess",
];

function kindOf(bo) {
  switch (bo.$type) {
    case "bpmn:ScriptTask":
      return "scripted (uses)";
    case "bpmn:ServiceTask":
      return "agent (prompt)";
    case "bpmn:ExclusiveGateway":
      return "branch";
    case "bpmn:SubProcess": {
      const lc = bo.loopCharacteristics || {};
      if (lc.$type === "bpmn:MultiInstanceLoopCharacteristics") return "map";
      if (lc.$type === "bpmn:StandardLoopCharacteristics") return "loop";
      return "sub-process";
    }
    default:
      return bo.$type;
  }
}

function configEl(bo) {
  const ee = bo.extensionElements;
  return ee && (ee.values || []).find((v) => v.$type === "rebar:Config");
}

function KindEntry(props) {
  const { element, id } = props;
  // TextFieldEntry always calls useDebounce, so a `debounce` service is required even for
  // a read-only field — omitting it throws "debounceFn is not a function" and takes the
  // whole group down.
  const debounce = useService("debounceInput");
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="Rebar kind"
      getValue={() => kindOf(element.businessObject)}
      setValue={() => {}}
      debounce={debounce}
      disabled
    />
  );
}

function ConfigEntry(props) {
  const { element, id } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");

  const getValue = () => {
    const c = configEl(element.businessObject);
    return c ? c.value : "";
  };

  const setValue = (value) => {
    const bo = element.businessObject;
    let ee = bo.extensionElements;
    if (!ee) {
      ee = bpmnFactory.create("bpmn:ExtensionElements", { values: [] });
      ee.$parent = bo;
      modeling.updateProperties(element, { extensionElements: ee });
    }
    let c = configEl(bo);
    if (!c) {
      c = bpmnFactory.create("rebar:Config", { value: value || "" });
      c.$parent = ee;
      modeling.updateModdleProperties(element, ee, { values: [...(ee.values || []), c] });
    } else {
      modeling.updateModdleProperties(element, c, { value: value || "" });
    }
  };

  return (
    <TextAreaEntry
      id={id}
      element={element}
      label="Rebar config (JSON)"
      rows={6}
      getValue={getValue}
      setValue={setValue}
      debounce={debounce}
    />
  );
}

function rebarGroup(element) {
  return {
    id: "rebar",
    label: "Rebar",
    entries: [
      { id: "rebar-kind", component: KindEntry },
      { id: "rebar-config", component: ConfigEntry, isEdited: isTextAreaEntryEdited },
    ],
  };
}

class RebarPropertiesProvider {
  constructor(propertiesPanel) {
    propertiesPanel.registerProvider(LOW_PRIORITY, this);
  }
  getGroups(element) {
    return (groups) => {
      const bo = element.businessObject;
      if (bo && REBAR_KINDS.includes(bo.$type)) {
        groups.push(rebarGroup(element));
      }
      return groups;
    };
  }
}
RebarPropertiesProvider.$inject = ["propertiesPanel"];

export default {
  __init__: ["rebarPropertiesProvider"],
  rebarPropertiesProvider: ["type", RebarPropertiesProvider],
};
