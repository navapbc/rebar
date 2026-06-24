/**
 * Custom properties-panel provider: a "Rebar" group that surfaces what bpmn-js's stock
 * panel can't — the step's rebar KIND and its `<rebar:Config>` payload (the with/mode/
 * model/loop-bounds/branch-condition JSON). It is shown for the element types that carry
 * rebar semantics, and the config is editable in place, so a human can both READ what a
 * step does and CHANGE it (or fill one in for a freshly-drawn step) before Save.
 *
 * Structured-vs-raw (story a83a). For the KNOWN step kinds (scripted/agent/loop/map) we
 * render typed, per-field entries — `with.<field>` driven by the step's contract
 * (window.REBAR_CONTRACTS[name].consumes), plus mode/model (agent) and the loop/map
 * bounds — so authoring no longer means hand-editing JSON. Every structured field
 * read/writes a SLICE of the SAME parsed `rebar:Config` blob (parse → mutate slice →
 * re-serialize → updateModdleProperties), so the Python round-trip contract is unchanged
 * and the raw editor and the structured fields stay consistent (one source of truth).
 *
 * The raw JSON textarea is kept as an "Advanced (raw JSON)" fallback so an UNKNOWN /
 * uncommon config (keys outside the structured set) is always editable and never lost;
 * for a node whose kind is not one of the known kinds it is the ONLY editor. Field-level
 * invalids (a non-numeric bound, an empty required field) surface a visible entry error
 * via each entry's `validate` and DO NOT mutate the blob — the prior value is preserved.
 */
import {
  CollapsibleEntry,
  SelectEntry,
  TextAreaEntry,
  TextFieldEntry,
  isSelectEntryEdited,
  isTextAreaEntryEdited,
  isTextFieldEntryEdited,
} from "@bpmn-io/properties-panel";
import { useService } from "bpmn-js-properties-panel";

const LOW_PRIORITY = 500;
const REBAR_KINDS = [
  "bpmn:ScriptTask",
  "bpmn:ServiceTask",
  "bpmn:ExclusiveGateway",
  "bpmn:SubProcess",
];

// The closed set of structured step kinds (a83a). Anything else is "uncommon" and falls
// back to the raw JSON editor entirely.
function rebarKind(bo) {
  switch (bo.$type) {
    case "bpmn:ScriptTask":
      return "scripted";
    case "bpmn:ServiceTask":
      return "agent";
    case "bpmn:ExclusiveGateway":
      return "branch";
    case "bpmn:SubProcess": {
      const lc = bo.loopCharacteristics || {};
      if (lc.$type === "bpmn:MultiInstanceLoopCharacteristics") return "map";
      if (lc.$type === "bpmn:StandardLoopCharacteristics") return "loop";
      return "sub-process";
    }
    default:
      return null;
  }
}

const STRUCTURED_KINDS = ["scripted", "agent", "loop", "map"];

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

// Parse the node's `rebar:Config` blob into an object; an empty / malformed blob parses
// to {} so structured reads never throw (the raw editor remains the place to repair
// genuinely broken JSON).
function parseConfig(bo) {
  const c = configEl(bo);
  if (!c || !c.value) return {};
  try {
    const v = JSON.parse(c.value);
    return v && typeof v === "object" ? v : {};
  } catch (e) {
    return {};
  }
}

// THE single write path every structured field and the raw editor share: replace the
// node's whole `rebar:Config` value, creating the extensionElements/Config nodes on first
// write. `mutate(cfg)` receives the parsed object to edit in place.
function writeConfig(element, modeling, bpmnFactory, value) {
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
}

function mutateConfig(element, modeling, bpmnFactory, mutate) {
  const cfg = parseConfig(element.businessObject);
  mutate(cfg);
  writeConfig(element, modeling, bpmnFactory, JSON.stringify(cfg));
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

function ActionEntry(props) {
  const { element, id } = props;
  // The step's action — `uses` (scripted) / `prompt` (agent) — round-trips through the
  // element NAME, which isn't obvious; surface it as a first-class, labelled field so a
  // new step can be told what to run without knowing the name==action convention.
  const modeling = useService("modeling");
  const debounce = useService("debounceInput");
  const bo = element.businessObject;
  const label = bo.$type === "bpmn:ServiceTask" ? "Prompt id" : "Script (uses)";
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label={label}
      getValue={() => bo.name || ""}
      setValue={(v) => modeling.updateProperties(element, { name: v || "" })}
      debounce={debounce}
    />
  );
}

function PromptTextEntry(props) {
  const { element, id } = props;
  // The prompt TEXT (resolved from the reviewer / .rebar/prompts/<id>.md by the Python
  // side and injected as window.REBAR_PROMPTS) — read-only, so you can see what the agent
  // step actually runs without leaving the editor. Editing prompts stays a git-file
  // concern (the IR references a prompt id, never inlines the text).
  const debounce = useService("debounceInput");
  const bo = element.businessObject;
  return (
    <TextAreaEntry
      id={id}
      element={element}
      label="Prompt text (read-only)"
      rows={8}
      getValue={() => (window.REBAR_PROMPTS && window.REBAR_PROMPTS[bo.name]) || ""}
      setValue={() => {}}
      debounce={debounce}
      disabled
    />
  );
}

function formatContract(view) {
  // Render a scripted op's contract (window.REBAR_CONTRACTS[uses]) as legible read-only
  // text: its description + CONSUMES (input fields) / PRODUCES (output fields). A node
  // with no declared contract shows a defined empty state, never a blank/crash.
  // An opaque / contract-less node (checked === false, or no view) is UNCHECKED: the
  // static contract check has nothing to verify against, so flag it visibly rather than
  // let a blank read as "fine" (c768).
  if (!view || !view.has_contract || view.checked === false) {
    return "⚠ unchecked (opaque source)\n(no declared contract for this step)";
  }
  const fmt = (fields) =>
    !fields || fields.length === 0
      ? "  (none)"
      : fields
          .map((f) => {
            const req = f.required ? " (required)" : "";
            const typ = f.type ? `: ${f.type}` : "";
            const desc = f.description ? ` — ${f.description}` : "";
            return `  ${f.name}${typ}${req}${desc}`;
          })
          .join("\n");
  const lines = [];
  if (view.description) lines.push(view.description, "");
  lines.push("CONSUMES:", fmt(view.consumes), "", "PRODUCES:", fmt(view.produces));
  return lines.join("\n");
}

function ContractEntry(props) {
  const { element, id } = props;
  // The scripted op's I/O CONTRACT (resolved Python-side and injected as
  // window.REBAR_CONTRACTS, keyed by the `uses` op name == the element NAME) — read-only,
  // so a human can see what a step consumes/produces before wiring `${{ steps.… }}` refs.
  const debounce = useService("debounceInput");
  const bo = element.businessObject;
  return (
    <TextAreaEntry
      id={id}
      element={element}
      label="Contract (read-only)"
      rows={10}
      getValue={() =>
        formatContract(window.REBAR_CONTRACTS && window.REBAR_CONTRACTS[bo.name])
      }
      setValue={() => {}}
      debounce={debounce}
      disabled
    />
  );
}

function WhenEntry(props) {
  const { element, id } = props;
  // The branch's `when` is an EDITABLE structured field (a83a): it writes the
  // condition slice of rebar:Config like every other structured entry. (The deeper
  // branch UX — adding/removing arms + connection routing — is the deferred S9 scope;
  // this is just the condition field so `branch` is covered by structured fields.)
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");
  const getWhen = () => {
    const c = configEl(element.businessObject);
    try {
      return (c && JSON.parse(c.value || "{}").when) || "";
    } catch (e) {
      return "";
    }
  };
  const setWhen = (value) => {
    mutateConfig(element, modeling, bpmnFactory, (cfg) => {
      if (value) cfg.when = value;
      else delete cfg.when;
    });
  };
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="Condition (when → then, else otherwise)"
      getValue={getWhen}
      setValue={setWhen}
      debounce={debounce}
    />
  );
}

// ── Structured fields (a83a) ────────────────────────────────────────────────────
// Each structured entry reads/writes ONE slice of the shared `rebar:Config` blob via
// mutateConfig (parse → mutate → re-serialize), so the round-trip contract is unchanged
// and the raw "Advanced" editor stays consistent with the typed fields.

// Coerce a raw text field value to the contract field's declared type. Strings pass
// through; booleans accept true/false; numbers accept a finite numeric literal — anything
// non-coercible for number/boolean is reported via `coerceError` so the caller can show a
// field error and skip the write (never silently storing a bad value).
function coerceTyped(raw, type) {
  const t = String(type || "").toLowerCase();
  if (raw === "" || raw == null) return { value: "", empty: true };
  if (t === "number" || t === "integer") {
    const n = Number(raw);
    if (!Number.isFinite(n) || String(raw).trim() === "") {
      return { error: "Must be a number" };
    }
    if (t === "integer" && !Number.isInteger(n)) return { error: "Must be an integer" };
    return { value: n };
  }
  if (t === "boolean") {
    const s = String(raw).trim().toLowerCase();
    if (s === "true") return { value: true };
    if (s === "false") return { value: false };
    return { error: "Must be true or false" };
  }
  return { value: String(raw) };
}

// One typed `with.<field>` entry for a contract input field. A REQUIRED field that is
// emptied shows an error; a type-mismatched value shows an error; neither mutates the blob
// (the prior value is preserved, never silently dropped).
function WithFieldEntry(props) {
  const { element, id, field } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");
  const name = field.name;

  const getValue = () => {
    const w = parseConfig(element.businessObject).with || {};
    const v = w[name];
    return v == null ? "" : typeof v === "string" ? v : JSON.stringify(v);
  };

  const validate = (v) => {
    if ((v === "" || v == null) && field.required) return "Required";
    const c = coerceTyped(v, field.type);
    return c.error || null;
  };

  const setValue = (v, err) => {
    if (err) return; // invalid → leave the blob (and prior value) untouched
    const c = coerceTyped(v, field.type);
    if (c.error) return;
    mutateConfig(element, modeling, bpmnFactory, (cfg) => {
      const w = cfg.with && typeof cfg.with === "object" ? cfg.with : {};
      if (c.empty) {
        delete w[name];
      } else {
        w[name] = c.value;
      }
      if (Object.keys(w).length) cfg.with = w;
      else delete cfg.with;
    });
  };

  const typ = field.type ? ` (${field.type})` : "";
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label={`with.${name}${field.required ? " *" : ""}${typ}`}
      description={field.description || undefined}
      getValue={getValue}
      setValue={setValue}
      validate={validate}
      debounce={debounce}
    />
  );
}

// A plain text slice of the config (e.g. loop `var`/`while`/`until`, map `over`/`as`).
// Empty clears the key. `required` makes empty an error.
function ConfigTextEntry(props) {
  const { element, id, ckey, label, required, description } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");

  const getValue = () => {
    const v = parseConfig(element.businessObject)[ckey];
    return v == null ? "" : String(v);
  };
  const validate = (v) => ((v === "" || v == null) && required ? "Required" : null);
  const setValue = (v, err) => {
    if (err) return;
    mutateConfig(element, modeling, bpmnFactory, (cfg) => {
      if (v === "" || v == null) delete cfg[ckey];
      else cfg[ckey] = String(v);
    });
  };
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label={label}
      description={description || undefined}
      getValue={getValue}
      setValue={setValue}
      validate={validate}
      debounce={debounce}
    />
  );
}

// A NUMERIC slice of the config (loop `max_iterations`, map `max_concurrency`). Kept a
// TEXT field with a numeric `validate` rather than a number input so a NON-NUMERIC entry
// surfaces a visible error AND the prior numeric value is preserved (an HTML number input
// would silently swallow the bad keystrokes — defeating the "shows an error, no loss" AC).
function ConfigNumberEntry(props) {
  const { element, id, ckey, label, description } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");

  const getValue = () => {
    const v = parseConfig(element.businessObject)[ckey];
    return v == null ? "" : String(v);
  };
  const validate = (v) => {
    if (v === "" || v == null) return null; // empty clears it (optional bound)
    const n = Number(v);
    if (!Number.isFinite(n) || String(v).trim() === "") return "Must be a number";
    if (!Number.isInteger(n)) return "Must be an integer";
    return null;
  };
  const setValue = (v, err) => {
    if (err) return; // non-numeric → keep prior value, show error, no blob mutation
    mutateConfig(element, modeling, bpmnFactory, (cfg) => {
      if (v === "" || v == null) delete cfg[ckey];
      else cfg[ckey] = Number(v);
    });
  };
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label={label}
      description={description || undefined}
      getValue={getValue}
      setValue={setValue}
      validate={validate}
      debounce={debounce}
    />
  );
}

// Agent `mode`: a closed select over the three execution modes.
function ModeEntry(props) {
  const { element, id } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const getValue = () => parseConfig(element.businessObject).mode || "";
  const setValue = (v) =>
    mutateConfig(element, modeling, bpmnFactory, (cfg) => {
      if (!v) delete cfg.mode;
      else cfg.mode = v;
    });
  const getOptions = () => [
    { value: "", label: "(default)" },
    { value: "findings", label: "findings" },
    { value: "structured", label: "structured" },
    { value: "text", label: "text" },
  ];
  return (
    <SelectEntry
      id={id}
      element={element}
      label="mode"
      getValue={getValue}
      setValue={setValue}
      getOptions={getOptions}
    />
  );
}

// The list of `with.<field>` entries a step's contract declares (REBAR_CONTRACTS keyed by
// the element NAME == its uses/prompt id). No contract → no structured `with` fields (the
// raw editor remains available for ad-hoc `with` keys).
function withFieldEntries(element) {
  const bo = element.businessObject;
  const view = window.REBAR_CONTRACTS && window.REBAR_CONTRACTS[bo.name];
  const consumes = (view && view.consumes) || [];
  return consumes.map((field) => ({
    id: `rebar-with-${field.name}`,
    component: (p) => <WithFieldEntry {...p} field={field} />,
    isEdited: isTextFieldEntryEdited,
  }));
}

// The raw JSON editor — the shared blob's verbatim view. PRIMARY editor for an unknown
// kind; an "Advanced (raw JSON)" FALLBACK (kept reachable) for the known kinds so an
// uncommon config outside the structured set is always editable and never lost.
function RawConfigEntry(props) {
  const { element, id } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");

  const getValue = () => {
    const c = configEl(element.businessObject);
    return c ? c.value : "";
  };
  // The raw editor is the one place genuinely-broken JSON can be repaired, so it accepts
  // any text verbatim; malformed JSON surfaces as an error via the live /validate region
  // (story 998e), not by blocking the keystroke.
  const setValue = (value) => writeConfig(element, modeling, bpmnFactory, value || "");

  return (
    <TextAreaEntry
      id={id}
      element={element}
      label="Rebar config (JSON)"
      description={
        'Written verbatim to the IR. e.g. {"with": {"k": "${{ inputs.x }}"}} — ' +
        'plus mode/model/output_schema (agent), max_iterations/while/until (loop), ' +
        "over/as/max_concurrency (map), when (branch)."
      }
      rows={6}
      getValue={getValue}
      setValue={setValue}
      debounce={debounce}
    />
  );
}

// Wrap the raw editor in a collapsible "Advanced (raw JSON)" entry so it stays reachable
// without competing with the structured fields for the known kinds.
function AdvancedRawEntry(props) {
  const { element, id } = props;
  return (
    <CollapsibleEntry
      id={id}
      element={element}
      label="Advanced (raw JSON)"
      entries={[{ id: `${id}-raw`, component: RawConfigEntry }]}
    />
  );
}

// The structured entries for a known kind, in declaration order.
function structuredEntries(element, kind) {
  const entries = [];
  if (kind === "scripted") {
    entries.push({ id: "rebar-action", component: ActionEntry });
    entries.push({ id: "rebar-contract", component: ContractEntry });
    entries.push(...withFieldEntries(element));
  } else if (kind === "agent") {
    entries.push({ id: "rebar-action", component: ActionEntry });
    entries.push({ id: "rebar-contract", component: ContractEntry });
    entries.push({ id: "rebar-prompt-text", component: PromptTextEntry });
    entries.push({
      id: "rebar-mode",
      component: ModeEntry,
      isEdited: isSelectEntryEdited,
    });
    entries.push({
      id: "rebar-model",
      component: (p) => <ConfigTextEntry {...p} ckey="model" label="model" />,
      isEdited: isTextFieldEntryEdited,
    });
    entries.push(...withFieldEntries(element));
  } else if (kind === "loop") {
    entries.push({
      id: "rebar-loop-var",
      component: (p) => <ConfigTextEntry {...p} ckey="var" label="var (loop variable)" />,
      isEdited: isTextFieldEntryEdited,
    });
    entries.push({
      id: "rebar-loop-max",
      component: (p) => (
        <ConfigNumberEntry {...p} ckey="max_iterations" label="max_iterations" />
      ),
      isEdited: isTextFieldEntryEdited,
    });
    entries.push({
      id: "rebar-loop-while",
      component: (p) => <ConfigTextEntry {...p} ckey="while" label="while" />,
      isEdited: isTextFieldEntryEdited,
    });
    entries.push({
      id: "rebar-loop-until",
      component: (p) => <ConfigTextEntry {...p} ckey="until" label="until" />,
      isEdited: isTextFieldEntryEdited,
    });
  } else if (kind === "map") {
    entries.push({
      id: "rebar-map-over",
      component: (p) => <ConfigTextEntry {...p} ckey="over" label="over" required />,
      isEdited: isTextFieldEntryEdited,
    });
    entries.push({
      id: "rebar-map-as",
      component: (p) => <ConfigTextEntry {...p} ckey="as" label="as (item variable)" />,
      isEdited: isTextFieldEntryEdited,
    });
    entries.push({
      id: "rebar-map-index",
      component: (p) => <ConfigTextEntry {...p} ckey="index_var" label="index_var" />,
      isEdited: isTextFieldEntryEdited,
    });
    entries.push({
      id: "rebar-map-conc",
      component: (p) => (
        <ConfigNumberEntry {...p} ckey="max_concurrency" label="max_concurrency" />
      ),
      isEdited: isTextFieldEntryEdited,
    });
  }
  return entries;
}

function rebarGroup(element) {
  const bo = element.businessObject;
  const kind = rebarKind(bo);
  const entries = [{ id: "rebar-kind", component: KindEntry }];

  if (kind === "branch") {
    // Branch gets a structured `when` condition field (a83a). The deeper branch UX —
    // arm add/remove + connection routing — is the deferred S9 scope.
    entries.push({ id: "rebar-when", component: WhenEntry });
    entries.push({
      id: "rebar-config",
      component: RawConfigEntry,
      isEdited: isTextAreaEntryEdited,
    });
    return { id: "rebar", label: "Rebar", entries };
  }

  if (STRUCTURED_KINDS.includes(kind)) {
    // KNOWN kind: structured fields are the primary path; the raw editor stays reachable
    // as an "Advanced (raw JSON)" fallback for uncommon keys.
    entries.push(...structuredEntries(element, kind));
    entries.push({ id: "rebar-config-advanced", component: AdvancedRawEntry });
    return { id: "rebar", label: "Rebar", entries };
  }

  // UNKNOWN / uncommon kind: the raw JSON editor is the only editor (nothing lost).
  entries.push({
    id: "rebar-config",
    component: RawConfigEntry,
    isEdited: isTextAreaEntryEdited,
  });
  return { id: "rebar", label: "Rebar", entries };
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
