/**
 * Custom properties-panel provider: a "Rebar" group that surfaces what bpmn-js's stock
 * panel can't — the step's rebar KIND and its `<rebar:Config>` payload (the with/mode/
 * model/loop-bounds/branch-condition JSON). It is shown for the element types that carry
 * rebar semantics, and the config is editable in place, so a human can both READ what a
 * step does and CHANGE it (or fill one in for a freshly-drawn step) before Save.
 *
 * Structured-only authoring (story a83a; da27 AC "no raw JSON textarea"). Every step kind
 * is edited through typed, per-field entries — `with.<field>` driven by the step's contract
 * (window.REBAR_CONTRACTS[name].consumes), plus mode/model (agent), the loop/map bounds, and
 * the branch `when` condition — so authoring NEVER means hand-editing JSON. There is NO raw
 * JSON textarea anywhere in the panel: the free-form `rebar:Config` editor has been removed.
 * Every structured field read/writes a SLICE of the SAME parsed `rebar:Config` blob (parse →
 * mutate slice → re-serialize → updateModdleProperties), so the Python round-trip contract is
 * unchanged. Keys outside the structured set are NOT shown, but a slice-write preserves them
 * verbatim (mutateConfig edits one key and re-serializes the rest), so they are never lost on
 * round-trip — they are simply not hand-editable in the UI. Field-level invalids (a non-numeric
 * bound, an empty required field) surface a visible entry error via each entry's `validate` and
 * DO NOT mutate the blob — the prior value is preserved.
 */
import {
  ListGroup,
  SelectEntry,
  TextAreaEntry,
  TextFieldEntry,
  isSelectEntryEdited,
  isTextFieldEntryEdited,
} from "@bpmn-io/properties-panel";
import { useService } from "bpmn-js-properties-panel";
import { useEffect, useState } from "preact/hooks";

const LOW_PRIORITY = 500;
const REBAR_KINDS = [
  "bpmn:ScriptTask",
  "bpmn:ServiceTask",
  "bpmn:ExclusiveGateway",
  "bpmn:SubProcess",
];

// A v3 `batch` step and an `agent` step are BOTH `bpmn:ServiceTask` — they are told apart by
// the `batch` object in the node's rebar:Config (the same key the Python serializer reads).
function isBatch(bo) {
  const cfg = parseConfig(bo);
  return !!(cfg && typeof cfg.batch === "object" && cfg.batch);
}

// The closed set of structured step kinds (a83a). Anything else is "uncommon" and falls
// back to the raw JSON editor entirely.
function rebarKind(bo) {
  switch (bo.$type) {
    case "bpmn:ScriptTask":
      return "scripted";
    case "bpmn:ServiceTask":
      return isBatch(bo) ? "batch" : "agent";
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

const STRUCTURED_KINDS = ["scripted", "agent", "batch", "loop", "map"];

function kindOf(bo) {
  switch (bo.$type) {
    case "bpmn:ScriptTask":
      return "scripted (uses)";
    case "bpmn:ServiceTask":
      return isBatch(bo) ? "batch (finder + criteria)" : "agent (prompt)";
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
// to {} so structured reads never throw. A genuinely-broken blob is surfaced by the live
// /validate region (story 998e) rather than edited as raw JSON in this panel.
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
    modeling.updateModdleProperties(element, ee, {
      values: [...(ee.values || []), c],
    });
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
      label="Step type"
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
      getValue={() =>
        (window.REBAR_PROMPTS && window.REBAR_PROMPTS[bo.name]) || ""
      }
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
  lines.push(
    "CONSUMES:",
    fmt(view.consumes),
    "",
    "PRODUCES:",
    fmt(view.produces),
  );
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
        formatContract(
          window.REBAR_CONTRACTS && window.REBAR_CONTRACTS[bo.name],
        )
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
    if (t === "integer" && !Number.isInteger(n))
      return { error: "Must be an integer" };
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
  const { element, id, ckey, label, required, description, placeholder } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");

  const getValue = () => {
    const v = parseConfig(element.businessObject)[ckey];
    return v == null ? "" : String(v);
  };
  const validate = (v) =>
    (v === "" || v == null) && required ? "Required" : null;
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
      placeholder={placeholder || undefined}
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
    if (!Number.isFinite(n) || String(v).trim() === "")
      return "Must be a number";
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
    { value: "", label: "(default — findings for a reviewer prompt)" },
    { value: "findings", label: "findings — structured findings list (default)" },
    { value: "structured", label: "structured — JSON matching the output schema" },
    { value: "text", label: "text — raw freeform text" },
  ];
  return (
    <SelectEntry
      id={id}
      element={element}
      label="Output mode"
      description="How the agent's response is parsed: findings (a list of findings; the default for reviewer prompts), structured (JSON against the output schema), or text (raw text). Leave blank to use the prompt's default."
      getValue={getValue}
      setValue={setValue}
      getOptions={getOptions}
    />
  );
}

// A `bpmn:ServiceTask` is EITHER an agent step (prompt) or a v3 batch step — a closed select
// lets the author convert between them, so a freshly-drawn ServiceTask can BECOME a batch.
// Switching to batch seeds an (invalid-until-filled) cfg.batch the criteria UI then completes;
// switching to agent drops cfg.batch. Either way the rest of cfg is preserved (slice-write).
function ServiceKindEntry(props) {
  const { element, id } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const getValue = () => (isBatch(element.businessObject) ? "batch" : "agent");
  const setValue = (v) =>
    mutateConfig(element, modeling, bpmnFactory, (cfg) => {
      if (v === "batch") {
        if (!cfg.batch || typeof cfg.batch !== "object")
          cfg.batch = { prompt: "", criteria: [] };
      } else {
        delete cfg.batch;
      }
    });
  const getOptions = () => [
    { value: "agent", label: "agent (prompt)" },
    { value: "batch", label: "batch (finder + criteria)" },
  ];
  return (
    <SelectEntry
      id={id}
      element={element}
      label="ServiceTask kind"
      getValue={getValue}
      setValue={setValue}
      getOptions={getOptions}
    />
  );
}

// ── Batch step fields (epic A: the v3 `batch` step) ───────────────────────────────
// A batch step's params live under cfg.batch (NOT cfg directly), so these read/write a
// slice of cfg.batch — keeping the SAME parse → mutate → re-serialize round-trip contract.

// The batch FINDER prompt (cfg.batch.prompt) — the single packing/finder pass the runner
// applies per batch. Required (a batch with no finder is meaningless).
function BatchFinderEntry(props) {
  const { element, id } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");
  const getValue = () =>
    (parseConfig(element.businessObject).batch || {}).prompt || "";
  const validate = (v) => (v === "" || v == null ? "Required" : null);
  const setValue = (v, err) => {
    if (err) return;
    mutateConfig(element, modeling, bpmnFactory, (cfg) => {
      const batch = cfg.batch && typeof cfg.batch === "object" ? cfg.batch : {};
      if (v === "" || v == null) delete batch.prompt;
      else batch.prompt = String(v);
      cfg.batch = batch;
    });
  };
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="finder (prompt id) *"
      getValue={getValue}
      setValue={setValue}
      validate={validate}
      debounce={debounce}
    />
  );
}

// A NUMERIC slice of cfg.batch (usd_budget). Text field + numeric validate so a bad value
// shows an error and keeps the prior one (mirrors ConfigNumberEntry, but float-allowing).
function BatchNumberEntry(props) {
  const { element, id, ckey, label } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");
  const getValue = () => {
    const v = (parseConfig(element.businessObject).batch || {})[ckey];
    return v == null ? "" : String(v);
  };
  const validate = (v) => {
    if (v === "" || v == null) return null; // empty clears it (optional)
    const n = Number(v);
    if (!Number.isFinite(n) || String(v).trim() === "")
      return "Must be a number";
    return null;
  };
  const setValue = (v, err) => {
    if (err) return;
    mutateConfig(element, modeling, bpmnFactory, (cfg) => {
      const batch = cfg.batch && typeof cfg.batch === "object" ? cfg.batch : {};
      if (v === "" || v == null) delete batch[ckey];
      else batch[ckey] = Number(v);
      cfg.batch = batch;
    });
  };
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label={label}
      getValue={getValue}
      setValue={setValue}
      validate={validate}
      debounce={debounce}
    />
  );
}

// model_ladder (cfg.batch.model_ladder) is an ordered escalation list of model ids, edited as
// an add/remove LIST of rows (story B-UX item 18) rather than a comma string. Each row is one
// model id at its index; add appends an empty id, remove drops the row. Reads/writes the same
// cfg.batch.model_ladder array slice, so the round-trip is unchanged.
function readLadder(element) {
  const v = (parseConfig(element.businessObject).batch || {}).model_ladder;
  return Array.isArray(v) ? v : [];
}

function writeLadder(element, modeling, bpmnFactory, list) {
  mutateConfig(element, modeling, bpmnFactory, (cfg) => {
    const batch = cfg.batch && typeof cfg.batch === "object" ? cfg.batch : {};
    // Keep rows verbatim (trimmed) — a freshly-ADDED empty row must survive so it renders
    // for editing (filtering empties here would silently drop the add, like the criteria
    // list keeps an empty {prompt:""}). An empty/whitespace id is a transient editing state
    // the user fills in; lint catches a still-empty entry on save. Drop the key only when the
    // whole list is gone (last row removed).
    const rows = list.map((s) => String(s).trim());
    if (rows.length) batch.model_ladder = rows;
    else delete batch.model_ladder;
    cfg.batch = batch;
  });
}

// One model-id row of the ladder.
function LadderRowEntry(props) {
  const { element, id, index } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");
  const getValue = () => readLadder(element)[index] || "";
  const setValue = (v) => {
    const list = readLadder(element).slice();
    list[index] = String(v || "");
    writeLadder(element, modeling, bpmnFactory, list);
  };
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label={`model ${index + 1}`}
      placeholder="claude-sonnet-4-6"
      getValue={getValue}
      setValue={setValue}
      debounce={debounce}
    />
  );
}

// The "Model ladder" ListGroup (add/remove ordered model ids) for a batch step.
function modelLadderGroup(element, modeling, bpmnFactory) {
  const list = readLadder(element);
  const items = list.map((m, i) => ({
    id: `ladder-${i}`,
    label: m || `model ${i + 1}`,
    entries: [
      {
        id: `ladder-${i}-id`,
        component: (p) => <LadderRowEntry {...p} index={i} />,
        isEdited: isTextFieldEntryEdited,
      },
    ],
    remove: () => {
      const next = readLadder(element).slice();
      next.splice(i, 1);
      writeLadder(element, modeling, bpmnFactory, next);
    },
  }));
  return {
    id: "rebar-ladder",
    label: "Model ladder",
    component: ListGroup,
    element,
    items,
    add: (event) => {
      if (event && event.stopPropagation) event.stopPropagation();
      writeLadder(element, modeling, bpmnFactory, [...readLadder(element), ""]);
    },
  };
}

// ── Library-backed criterion pickers + in-editor authoring (story B-UX) ───────────
// A batch criterion's `prompt` and `when` are SELECTED from library-/IR-backed dropdowns
// (no free-text typing) and new criteria/prompts + overlay triggers can be CREATED in the
// panel. The picker sources are injected by editor.py: window.REBAR_LIBRARY (the authorable
// prompt+criterion list from enumerate_library) and window.REBAR_OVERLAY_TRIGGERS (this
// workflow's overlay_triggers outputs, as {stepId,name,expr,label}); the client maintains
// the latter as triggers are added.

const SENTINEL_CREATE = "__rebar_create__"; // "➕ Create new criterion/prompt…" in the prompt select
const SENTINEL_NEW_TRIGGER = "__rebar_new_trigger__"; // "➕ New trigger…" in the when select
const WHEN_ALWAYS = "__rebar_always__"; // "(always include)" → clears `when`

// A tiny module-level store for the transient AUTHORING forms (create criterion/prompt, and
// create overlay trigger). It is NOT persisted to rebar:Config — it only drives which form
// fields are visible and remembers which criterion opened the form. Components subscribe via
// useAuthoring() and force a re-render through notifyAuthoring() (the properties-panel does
// not re-run getGroups on a non-model event, so the form's visibility is store-driven).
const authoring = {
  open: false,
  kind: "criterion",
  id: "",
  body: "",
  // Routing-fields overlay for authoring a plan-review CRITERION (story 6e31). Collected only
  // when kind === "criterion" and POSTed as a `routing` object to /library/create, which writes
  // the .rebar/criteria_routing.json overlay + activation (author_criterion_overlay). Defaults
  // mirror the packaged routing floor so a minimal form still produces a valid entry.
  routingExec: "1-TURN", // 1-TURN | 2-STEP | AGENT | DET
  routingLevels: "epic,story,task", // applies_at.levels (comma-separated)
  routingBlockThreshold: "0.95", // block_threshold, number in [0,1]
  routingPosture: "advisory", // default_posture: advisory | blocking
  routingFailMode: "open", // DET only: open | closed
  routingDetector: "", // DET only: a detector id, or `<prefix>*` for an id_prefix class
  targetIndex: null,
  // What the create-prompt save assigns the new id to: "criterion" (a batch criterion's
  // prompt at targetIndex) or "name" (the selected step's NAME == its prompt/uses action,
  // for an agent step's library-backed prompt picker — story B-UX item 7).
  targetKind: "criterion",
  // What a newly-added overlay trigger's expression is assigned to: "criterion" (a batch
  // criterion's `when` at triggerTargetIndex) or "if" (the selected step's `if` overlay
  // predicate — story B-UX item 9, the agent/scripted step-level inclusion select).
  triggerTargetKind: "criterion",
  // The bpmn id of the element whose criterion opened a form. Used to detect a selection
  // change so a form is never applied to the wrong element/index (see resetAuthoring +
  // authoringGroup): the store is module-global, so without this guard a form opened on
  // batch step A would write its new id/trigger to whatever element B is selected next.
  targetElementId: "",
  status: "",
  triggerOpen: false,
  triggerName: "",
  triggerKeywords: "",
  triggerTargetIndex: null,
  _subs: new Set(),
};

// Close both authoring forms and clear their transient state (id/body/trigger fields,
// remembered target index + element). Leaves `status` for the caller to set/clear.
function resetAuthoring() {
  authoring.open = false;
  authoring.kind = "criterion";
  authoring.id = "";
  authoring.body = "";
  authoring.routingExec = "1-TURN";
  authoring.routingLevels = "epic,story,task";
  authoring.routingBlockThreshold = "0.95";
  authoring.routingPosture = "advisory";
  authoring.routingFailMode = "open";
  authoring.routingDetector = "";
  authoring.targetIndex = null;
  authoring.targetKind = "criterion";
  authoring.targetElementId = "";
  authoring.triggerOpen = false;
  authoring.triggerName = "";
  authoring.triggerKeywords = "";
  authoring.triggerTargetIndex = null;
  authoring.triggerTargetKind = "criterion";
}

function notifyAuthoring() {
  authoring._subs.forEach((fn) => fn());
}

function useAuthoring() {
  const [, bump] = useState(0);
  useEffect(() => {
    const fn = () => bump((n) => n + 1);
    authoring._subs.add(fn);
    return () => authoring._subs.delete(fn);
  }, []);
  return authoring;
}

// Read the criterion at cfg.batch.criteria[index] (defensively, never throwing).
function criterionAt(element, index) {
  return (
    ((parseConfig(element.businessObject).batch || {}).criteria || [])[index] ||
    {}
  );
}

// Write one key of the criterion at cfg.batch.criteria[index] (empty/null deletes it).
function setCriterionKey(element, modeling, bpmnFactory, index, key, value) {
  mutateConfig(element, modeling, bpmnFactory, (cfg) => {
    const batch = cfg.batch && typeof cfg.batch === "object" ? cfg.batch : {};
    const crit = Array.isArray(batch.criteria) ? batch.criteria : [];
    const c = crit[index] && typeof crit[index] === "object" ? crit[index] : {};
    if (value === "" || value == null) delete c[key];
    else c[key] = String(value);
    crit[index] = c;
    batch.criteria = crit;
    cfg.batch = batch;
  });
}

// The criterion `prompt` SELECT options: every window.REBAR_LIBRARY entry (value=id,
// label=`<id> — <description>`), the current value (so a hand-authored id still shows), and a
// "➕ Create new…" sentinel that opens the authoring form.
function libraryOptions(current) {
  const lib = Array.isArray(window.REBAR_LIBRARY) ? window.REBAR_LIBRARY : [];
  const opts = [{ value: "", label: "(none)" }];
  const seen = new Set([""]);
  for (const e of lib) {
    if (!e || seen.has(e.id)) continue;
    seen.add(e.id);
    // Keep the id readable; truncate a long description so it doesn't overflow the
    // dropdown (story B-UX item 2). The full description still resolves Python-side.
    const full = e.description ? String(e.description) : "";
    const short = full.length > 60 ? `${full.slice(0, 60)}…` : full;
    const desc = short ? ` — ${short}` : "";
    const tag = e.kind === "criterion" ? "[criterion] " : "";
    opts.push({ value: e.id, label: `${tag}${e.id}${desc}` });
  }
  if (current && !seen.has(current)) {
    opts.push({ value: current, label: `${current} (custom)` });
  }
  opts.push({ value: SENTINEL_CREATE, label: "➕ Create new criterion/prompt…" });
  return opts;
}

// The criterion `prompt` field, as a library-backed SELECT (B-UX): no free-text typing.
function CriterionPromptEntry(props) {
  const { element, id, index } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const getValue = () => {
    const v = criterionAt(element, index).prompt;
    return v == null ? "" : String(v);
  };
  const setValue = (v) => {
    if (v === SENTINEL_CREATE) {
      // Open the authoring form targeting THIS criterion; do NOT write the sentinel.
      authoring.open = true;
      authoring.kind = "criterion";
      authoring.targetKind = "criterion";
      authoring.targetIndex = index;
      authoring.targetElementId = element.businessObject.id || element.id;
      authoring.status = "";
      notifyAuthoring();
      return;
    }
    setCriterionKey(element, modeling, bpmnFactory, index, "prompt", v);
  };
  return (
    <SelectEntry
      id={id}
      element={element}
      label="prompt id *"
      getValue={getValue}
      setValue={setValue}
      getOptions={() => libraryOptions(getValue())}
    />
  );
}

// The criterion `when` SELECT options: this workflow's overlay-trigger outputs (value=the full
// `${{ steps.<id>.outputs.<name> }}` expression), an "(always include)" option that clears
// `when`, the current value (if hand-authored), and a "➕ New trigger…" sentinel.
function overlayWhenOptions(current) {
  const trigs = Array.isArray(window.REBAR_OVERLAY_TRIGGERS)
    ? window.REBAR_OVERLAY_TRIGGERS
    : [];
  const opts = [{ value: WHEN_ALWAYS, label: "(always include)" }];
  const seen = new Set();
  for (const t of trigs) {
    if (!t || seen.has(t.expr)) continue;
    seen.add(t.expr);
    opts.push({ value: t.expr, label: t.label || t.expr });
  }
  if (current && !seen.has(current)) {
    opts.push({ value: current, label: `${current} (custom)` });
  }
  opts.push({ value: SENTINEL_NEW_TRIGGER, label: "➕ New trigger…" });
  return opts;
}

// The criterion `when` overlay predicate, as a SELECT over this workflow's overlay triggers.
function CriterionWhenEntry(props) {
  const { element, id, index } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const getStored = () => {
    const v = criterionAt(element, index).when;
    return v == null ? "" : String(v);
  };
  const getValue = () => getStored() || WHEN_ALWAYS;
  const setValue = (v) => {
    if (v === SENTINEL_NEW_TRIGGER) {
      authoring.triggerOpen = true;
      authoring.triggerTargetKind = "criterion";
      authoring.triggerTargetIndex = index;
      authoring.targetElementId = element.businessObject.id || element.id;
      authoring.status = "";
      notifyAuthoring();
      return;
    }
    const stored = v === WHEN_ALWAYS ? "" : v;
    setCriterionKey(element, modeling, bpmnFactory, index, "when", stored);
  };
  return (
    <SelectEntry
      id={id}
      element={element}
      label="Include this criterion when"
      description="The criterion is applied only when this overlay trigger fires (else skipped). Pick (always include) to apply it unconditionally."
      getValue={getValue}
      setValue={setValue}
      getOptions={() => overlayWhenOptions(getStored())}
    />
  );
}

// The step-level `if` overlay predicate, as a SELECT over this workflow's overlay triggers —
// the SAME picker the batch criterion `when` uses (story B-UX item 9), so a step's conditional
// inclusion is chosen, not free-typed. Writes/reads the cfg.if slice; "(always include)" clears
// it; "➕ New trigger…" opens the trigger authoring form targeting this step's `if`.
function IfPredicateSelectEntry(props) {
  const { element, id } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const getStored = () => {
    const v = parseConfig(element.businessObject).if;
    return v == null ? "" : String(v);
  };
  const getValue = () => getStored() || WHEN_ALWAYS;
  const setValue = (v) => {
    if (v === SENTINEL_NEW_TRIGGER) {
      authoring.triggerOpen = true;
      authoring.triggerTargetKind = "if";
      authoring.triggerTargetIndex = null;
      authoring.targetElementId = element.businessObject.id || element.id;
      authoring.status = "";
      notifyAuthoring();
      return;
    }
    const stored = v === WHEN_ALWAYS ? "" : v;
    mutateConfig(element, modeling, bpmnFactory, (cfg) => {
      if (stored) cfg.if = stored;
      else delete cfg.if;
    });
  };
  return (
    <SelectEntry
      id={id}
      element={element}
      label="Include this step when"
      description="The step runs only when this overlay trigger fires (else skipped). Pick (always include) to run it unconditionally."
      getValue={getValue}
      setValue={setValue}
      getOptions={() => overlayWhenOptions(getStored())}
    />
  );
}

// The agent step's `prompt` id (round-tripped through the element NAME), as a library-backed
// SELECT (story B-UX item 7) — the same affordance the batch criterion prompt uses, instead of
// a free-text field. Picks a window.REBAR_LIBRARY entry; a hand-authored id not in the list is
// preserved as a "(custom)" option; "➕ Create new…" opens the prompt authoring form.
function PromptIdSelectEntry(props) {
  const { element, id } = props;
  const modeling = useService("modeling");
  const bo = element.businessObject;
  const getValue = () => bo.name || "";
  const setValue = (v) => {
    if (v === SENTINEL_CREATE) {
      authoring.open = true;
      authoring.kind = "prompt";
      authoring.targetKind = "name";
      authoring.targetIndex = null;
      authoring.targetElementId = bo.id || element.id;
      authoring.status = "";
      notifyAuthoring();
      return;
    }
    modeling.updateProperties(element, { name: v || "" });
  };
  return (
    <SelectEntry
      id={id}
      element={element}
      label="Prompt id"
      description="The prompt this agent step runs (from the prompt/criterion library). Pick ➕ Create new… to author one in place."
      getValue={getValue}
      setValue={setValue}
      getOptions={() => libraryOptions(getValue())}
    />
  );
}

// The "Batch criteria" ListGroup: a per-criterion collapsible (prompt SELECT + `when` SELECT)
// with the stock add (+) / remove (×) affordances. add/remove edit cfg.batch.criteria via
// mutateConfig (no hooks — they fire on click, so they take modeling/bpmnFactory captured by
// the provider).
function batchCriteriaGroup(element, modeling, bpmnFactory) {
  const criteria =
    (parseConfig(element.businessObject).batch || {}).criteria || [];
  const list = Array.isArray(criteria) ? criteria : [];
  const items = list.map((c, i) => ({
    id: `criterion-${i}`,
    label: (c && c.prompt) || `criterion ${i + 1}`,
    entries: [
      {
        id: `criterion-${i}-prompt`,
        component: (p) => <CriterionPromptEntry {...p} index={i} />,
        isEdited: isSelectEntryEdited,
      },
      {
        id: `criterion-${i}-when`,
        component: (p) => <CriterionWhenEntry {...p} index={i} />,
        isEdited: isSelectEntryEdited,
      },
    ],
    remove: () =>
      mutateConfig(element, modeling, bpmnFactory, (cfg) => {
        const batch =
          cfg.batch && typeof cfg.batch === "object" ? cfg.batch : {};
        const cr = Array.isArray(batch.criteria) ? batch.criteria : [];
        cr.splice(i, 1);
        batch.criteria = cr;
        cfg.batch = batch;
      }),
  }));
  return {
    id: "rebar-criteria",
    label: "Batch criteria",
    component: ListGroup,
    element,
    items,
    add: (event) => {
      if (event && event.stopPropagation) event.stopPropagation();
      mutateConfig(element, modeling, bpmnFactory, (cfg) => {
        const batch =
          cfg.batch && typeof cfg.batch === "object" ? cfg.batch : {};
        const cr = Array.isArray(batch.criteria) ? batch.criteria : [];
        cr.push({ prompt: "" });
        batch.criteria = cr;
        cfg.batch = batch;
      });
    },
  };
}

// ── Authoring forms (create criterion/prompt + create overlay trigger) ────────────
// These render INTO a dedicated "Authoring" group on a batch step. Each field component is
// store-driven (useAuthoring) and renders null until its form is opened by the matching
// sentinel, so the panel stays uncluttered until the author asks to create something.

// Always-visible info + status line (keeps the group non-empty and surfaces the last result).
function AuthoringInfoEntry(props) {
  const { id } = props;
  const a = useAuthoring();
  const hint = a.open
    ? "Authoring a new entry — set an id + body, then Create & use."
    : a.triggerOpen
      ? "Adding a new overlay trigger — set a name + keywords, then Add & use."
      : "Pick “➕ Create new…” in a prompt/criterion picker, or “➕ New trigger…” in an inclusion picker, to author here.";
  return (
    <div class="bio-properties-panel-entry" data-entry-id={id}>
      <div style="font-size:11px;color:#555;padding:2px 0;">{hint}</div>
      {a.status ? (
        <div
          id="bio-properties-panel-rebar-author-status"
          class={/^error/.test(a.status) ? "err" : "ok"}
          style="font-size:11px;margin-top:2px;"
        >
          {a.status}
        </div>
      ) : null}
    </div>
  );
}

function AuthoringKindEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  if (!a.open) return null;
  return (
    <SelectEntry
      id={id}
      element={element}
      label="new entry kind"
      getValue={() => a.kind}
      setValue={(v) => {
        a.kind = v || "criterion";
        notifyAuthoring();
      }}
      getOptions={() => [
        { value: "criterion", label: "criterion (plan-review)" },
        { value: "prompt", label: "prompt" },
      ]}
    />
  );
}

function AuthoringIdEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const debounce = useService("debounceInput");
  if (!a.open) return null;
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="new id (letters/digits/dashes)"
      getValue={() => a.id}
      setValue={(v) => {
        a.id = v || "";
      }}
      debounce={debounce}
    />
  );
}

function AuthoringBodyEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const debounce = useService("debounceInput");
  if (!a.open) return null;
  return (
    <TextAreaEntry
      id={id}
      element={element}
      label="body / rubric (markdown)"
      rows={6}
      getValue={() => a.body}
      setValue={(v) => {
        a.body = v || "";
      }}
      debounce={debounce}
    />
  );
}

// ── Routing-fields form (story 6e31): the plan-review ROUTING overlay for a criterion ──────
// Shown only when authoring a criterion (a.kind === "criterion"). Each field drives one key of
// the `routing` object POSTed to /library/create; fail_mode + detector are DET-only.
function isCriterionAuthoring(a) {
  return a.open && a.kind === "criterion";
}

// Build the `routing` object from the authoring store (or null when not authoring a criterion).
function buildRouting(a) {
  if (a.kind !== "criterion") return null;
  const levels = (a.routingLevels || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const n = parseFloat(a.routingBlockThreshold);
  const routing = {
    exec: a.routingExec || "1-TURN",
    applies_at: levels.length ? { levels } : {},
    block_threshold: Number.isFinite(n) ? n : 0.95,
    default_posture: a.routingPosture || "advisory",
  };
  if ((a.routingExec || "") === "DET") {
    routing.fail_mode = a.routingFailMode || "open";
    const det = (a.routingDetector || "").trim();
    if (det) routing.detector = det.endsWith("*") ? { id_prefix: det.slice(0, -1) } : { id: det };
  }
  return routing;
}

function AuthoringRoutingExecEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  if (!isCriterionAuthoring(a)) return null;
  return (
    <SelectEntry
      id={id}
      element={element}
      label="exec (how the criterion runs)"
      getValue={() => a.routingExec}
      setValue={(v) => {
        a.routingExec = v || "1-TURN";
        notifyAuthoring();
      }}
      getOptions={() => [
        { value: "1-TURN", label: "1-TURN (single LLM call)" },
        { value: "2-STEP", label: "2-STEP (LLM)" },
        { value: "AGENT", label: "AGENT (tool-using LLM)" },
        { value: "DET", label: "DET (deterministic detector)" },
      ]}
    />
  );
}

function AuthoringRoutingLevelsEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const debounce = useService("debounceInput");
  if (!isCriterionAuthoring(a)) return null;
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="applies_at levels (comma-separated: epic,story,task)"
      getValue={() => a.routingLevels}
      setValue={(v) => {
        a.routingLevels = v || "";
      }}
      debounce={debounce}
    />
  );
}

function AuthoringRoutingThresholdEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const debounce = useService("debounceInput");
  if (!isCriterionAuthoring(a)) return null;
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="block_threshold (0–1)"
      getValue={() => a.routingBlockThreshold}
      setValue={(v) => {
        a.routingBlockThreshold = v || "";
      }}
      debounce={debounce}
    />
  );
}

function AuthoringRoutingPostureEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  if (!isCriterionAuthoring(a)) return null;
  return (
    <SelectEntry
      id={id}
      element={element}
      label="default_posture"
      getValue={() => a.routingPosture}
      setValue={(v) => {
        a.routingPosture = v || "advisory";
        notifyAuthoring();
      }}
      getOptions={() => [
        { value: "advisory", label: "advisory (coaching)" },
        { value: "blocking", label: "blocking (fails the gate)" },
      ]}
    />
  );
}

function AuthoringRoutingFailModeEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  if (!isCriterionAuthoring(a) || a.routingExec !== "DET") return null;
  return (
    <SelectEntry
      id={id}
      element={element}
      label="fail_mode (DET: on detector abstain)"
      getValue={() => a.routingFailMode}
      setValue={(v) => {
        a.routingFailMode = v || "open";
        notifyAuthoring();
      }}
      getOptions={() => [
        { value: "open", label: "open (abstain → advisory)" },
        { value: "closed", label: "closed (abstain → block)" },
      ]}
    />
  );
}

function AuthoringRoutingDetectorEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const debounce = useService("debounceInput");
  if (!isCriterionAuthoring(a) || a.routingExec !== "DET") return null;
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="detector selector (id, or a 'prefix*' class)"
      getValue={() => a.routingDetector}
      setValue={(v) => {
        a.routingDetector = v || "";
      }}
      debounce={debounce}
    />
  );
}

// Save button: POST /library/create (create_prompt under config.repo_root()), refresh
// window.REBAR_LIBRARY, then assign the new id to the criterion that opened the form.
function AuthoringSaveEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  if (!a.open) return null;
  const onSave = async () => {
    const newId = (a.id || "").trim();
    if (!newId) {
      a.status = "error: enter an id";
      notifyAuthoring();
      return;
    }
    a.status = "saving…";
    notifyAuthoring();
    try {
      const r = await fetch("/library/create", {
        method: "POST",
        headers: {
          "X-Rebar-Token": window.REBAR_TOKEN,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          id: newId,
          kind: a.kind,
          title: newId,
          body: a.body || "",
          // A plan-review criterion carries its ROUTING overlay so the backend
          // (author_criterion_overlay) writes .rebar/criteria_routing.json + activation
          // atomically. Omitted for plain prompts (kind !== "criterion").
          ...(buildRouting(a) ? { routing: buildRouting(a) } : {}),
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || !body.ok) {
        a.status = "error: " + (body.errors || ["save failed"]).join("; ");
        notifyAuthoring();
        return;
      }
      // Refresh the library so the new id is an option everywhere.
      try {
        const lr = await fetch("/library", {
          headers: { "X-Rebar-Token": window.REBAR_TOKEN },
        });
        if (lr.ok) window.REBAR_LIBRARY = await lr.json();
      } catch (e) {
        /* keep the prior library on a refresh failure (the new id still shows as current) */
      }
      const idx = a.targetIndex;
      const targetKind = a.targetKind;
      a.open = false;
      a.status = "created " + newId;
      a.id = "";
      a.body = "";
      a.routingExec = "1-TURN";
      a.routingLevels = "epic,story,task";
      a.routingBlockThreshold = "0.95";
      a.routingPosture = "advisory";
      a.routingFailMode = "open";
      a.routingDetector = "";
      if (targetKind === "name") {
        // Agent-step prompt picker (item 7): the new id IS the step's prompt action.
        modeling.updateProperties(element, { name: newId });
      } else if (idx != null) {
        setCriterionKey(element, modeling, bpmnFactory, idx, "prompt", newId);
      }
      notifyAuthoring();
    } catch (e) {
      a.status = "error: " + e.message;
      notifyAuthoring();
    }
  };
  return (
    <div class="bio-properties-panel-entry" data-entry-id={id}>
      <button
        type="button"
        id={"bio-properties-panel-" + id}
        class="bio-properties-panel-add-entry"
        onClick={onSave}
      >
        Create &amp; use
      </button>
    </div>
  );
}

// Find this workflow's overlay_triggers step element (its NAME is the op id "overlay_triggers";
// its bpmn id IS the IR step id). Returns null when the workflow has no such step.
function findTriggersElement(elementRegistry) {
  const all = elementRegistry.filter(
    (e) => e.businessObject && e.businessObject.name === "overlay_triggers",
  );
  return all && all.length ? all[0] : null;
}

function TriggerNameEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const debounce = useService("debounceInput");
  if (!a.triggerOpen) return null;
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="trigger name"
      getValue={() => a.triggerName}
      setValue={(v) => {
        a.triggerName = v || "";
      }}
      debounce={debounce}
    />
  );
}

function TriggerKeywordsEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const debounce = useService("debounceInput");
  if (!a.triggerOpen) return null;
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="keywords (comma-separated)"
      getValue={() => a.triggerKeywords}
      setValue={(v) => {
        a.triggerKeywords = v || "";
      }}
      debounce={debounce}
    />
  );
}

// Add button: write {name: [keywords]} to the overlay_triggers step's with.keyword_triggers,
// push the new {stepId,name,expr,label} into window.REBAR_OVERLAY_TRIGGERS, and select the new
// trigger's expression on the criterion that opened the form.
function TriggerSaveEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const elementRegistry = useService("elementRegistry");
  if (!a.triggerOpen) return null;
  const onAdd = () => {
    const name = (a.triggerName || "").trim();
    if (!name) {
      a.status = "error: enter a trigger name";
      notifyAuthoring();
      return;
    }
    const trig = findTriggersElement(elementRegistry);
    if (!trig) {
      a.status = "error: no overlay_triggers step in this workflow";
      notifyAuthoring();
      return;
    }
    const keywords = String(a.triggerKeywords || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    mutateConfig(trig, modeling, bpmnFactory, (cfg) => {
      const w = cfg.with && typeof cfg.with === "object" ? cfg.with : {};
      const kt =
        w.keyword_triggers && typeof w.keyword_triggers === "object"
          ? w.keyword_triggers
          : {};
      kt[name] = keywords;
      w.keyword_triggers = kt;
      cfg.with = w;
    });
    const stepId = trig.businessObject.id || trig.id;
    const expr = "${{ steps." + stepId + ".outputs." + name + " }}";
    const list = Array.isArray(window.REBAR_OVERLAY_TRIGGERS)
      ? window.REBAR_OVERLAY_TRIGGERS
      : [];
    if (!list.some((t) => t.expr === expr)) {
      list.push({ stepId, name, expr, label: stepId + "." + name });
      window.REBAR_OVERLAY_TRIGGERS = list;
    }
    const idx = a.triggerTargetIndex;
    const targetKind = a.triggerTargetKind;
    a.triggerOpen = false;
    a.status = "added trigger " + name;
    a.triggerName = "";
    a.triggerKeywords = "";
    if (targetKind === "if") {
      // Step-level `if` overlay select (item 9): assign the new trigger expr to cfg.if.
      mutateConfig(element, modeling, bpmnFactory, (cfg) => {
        if (expr) cfg.if = expr;
        else delete cfg.if;
      });
    } else if (idx != null) {
      setCriterionKey(element, modeling, bpmnFactory, idx, "when", expr);
    }
    notifyAuthoring();
  };
  return (
    <div class="bio-properties-panel-entry" data-entry-id={id}>
      <button
        type="button"
        id={"bio-properties-panel-" + id}
        class="bio-properties-panel-add-entry"
        onClick={onAdd}
      >
        Add &amp; use
      </button>
    </div>
  );
}

// The "Authoring" group on a batch step: the create-criterion/prompt form and the
// create-overlay-trigger form, each store-driven (visible only once its sentinel is picked).
function authoringGroup(element) {
  // The store is module-global, so a form left open on a previously-selected batch step
  // would otherwise apply its new id/trigger to THIS element's criterion at the remembered
  // index (wrong element). If a form is open for a DIFFERENT element, close it on render so
  // the create form never targets the wrong element/index.
  const elId = element.businessObject.id || element.id;
  if (
    (authoring.open || authoring.triggerOpen) &&
    authoring.targetElementId !== elId
  ) {
    resetAuthoring();
  }
  return {
    id: "rebar-authoring",
    label: "New criterion / prompt / trigger",
    entries: [
      { id: "rebar-author-info", component: AuthoringInfoEntry },
      {
        id: "rebar-author-kind",
        component: AuthoringKindEntry,
        isEdited: isSelectEntryEdited,
      },
      {
        id: "rebar-author-id",
        component: AuthoringIdEntry,
        isEdited: isTextFieldEntryEdited,
      },
      { id: "rebar-author-body", component: AuthoringBodyEntry },
      // Routing-fields (story 6e31): only render for kind === "criterion" (each component
      // self-guards). fail_mode + detector additionally self-guard on exec === "DET".
      {
        id: "rebar-author-routing-exec",
        component: AuthoringRoutingExecEntry,
        isEdited: isSelectEntryEdited,
      },
      {
        id: "rebar-author-routing-levels",
        component: AuthoringRoutingLevelsEntry,
        isEdited: isTextFieldEntryEdited,
      },
      {
        id: "rebar-author-routing-threshold",
        component: AuthoringRoutingThresholdEntry,
        isEdited: isTextFieldEntryEdited,
      },
      {
        id: "rebar-author-routing-posture",
        component: AuthoringRoutingPostureEntry,
        isEdited: isSelectEntryEdited,
      },
      {
        id: "rebar-author-routing-failmode",
        component: AuthoringRoutingFailModeEntry,
        isEdited: isSelectEntryEdited,
      },
      {
        id: "rebar-author-routing-detector",
        component: AuthoringRoutingDetectorEntry,
        isEdited: isTextFieldEntryEdited,
      },
      { id: "rebar-author-save", component: AuthoringSaveEntry },
      {
        id: "rebar-trigger-name",
        component: TriggerNameEntry,
        isEdited: isTextFieldEntryEdited,
      },
      {
        id: "rebar-trigger-keywords",
        component: TriggerKeywordsEntry,
        isEdited: isTextFieldEntryEdited,
      },
      { id: "rebar-trigger-save", component: TriggerSaveEntry },
    ],
  };
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

// The structured entries for a known kind, in declaration order.
function structuredEntries(element, kind) {
  const entries = [];
  // The `if:` overlay predicate (epic A: conditional criterion/step inclusion) is a
  // non-structural step key that round-trips via rebar:Config — editable on the common
  // leaf steps (scripted/agent) so a step can be conditionally INCLUDED from the editor.
  const ifEntry = {
    id: "rebar-if",
    component: IfPredicateSelectEntry,
    isEdited: isSelectEntryEdited,
  };
  if (kind === "scripted") {
    entries.push({ id: "rebar-action", component: ActionEntry });
    entries.push({ id: "rebar-contract", component: ContractEntry });
    entries.push(ifEntry);
    entries.push(...withFieldEntries(element));
  } else if (kind === "agent") {
    entries.push({
      id: "rebar-service-kind",
      component: ServiceKindEntry,
      isEdited: isSelectEntryEdited,
    });
    entries.push({
      id: "rebar-action",
      component: PromptIdSelectEntry,
      isEdited: isSelectEntryEdited,
    });
    entries.push({ id: "rebar-contract", component: ContractEntry });
    entries.push({ id: "rebar-prompt-text", component: PromptTextEntry });
    entries.push({
      id: "rebar-mode",
      component: ModeEntry,
      isEdited: isSelectEntryEdited,
    });
    entries.push({
      id: "rebar-model",
      component: (p) => (
        <ConfigTextEntry
          {...p}
          ckey="model"
          label="Model"
          placeholder="claude-sonnet-4-6"
          description="Optional model id (e.g. claude-sonnet-4-6). Leave blank to use the workflow/config/env default (claude-opus-4-8)."
        />
      ),
      isEdited: isTextFieldEntryEdited,
    });
    entries.push(ifEntry);
    entries.push(...withFieldEntries(element));
  } else if (kind === "batch") {
    // A v3 batch step's structural params live in cfg.batch.{prompt,usd_budget,model_ladder};
    // the criteria LIST (add/remove/edit) is a separate ListGroup (batchCriteriaGroup).
    entries.push({
      id: "rebar-service-kind",
      component: ServiceKindEntry,
      isEdited: isSelectEntryEdited,
    });
    entries.push({ id: "rebar-batch-finder", component: BatchFinderEntry });
    entries.push({
      id: "rebar-batch-budget",
      component: (p) => (
        <BatchNumberEntry
          {...p}
          ckey="usd_budget"
          label="usd_budget (USD cost ceiling)"
        />
      ),
      isEdited: isTextFieldEntryEdited,
    });
    // model_ladder is edited as an add/remove LIST (see modelLadderGroup), not a comma
    // string (story B-UX item 18), so it is NOT pushed here as a single field.
    entries.push(ifEntry);
  } else if (kind === "loop") {
    entries.push({
      id: "rebar-loop-var",
      component: (p) => (
        <ConfigTextEntry {...p} ckey="var" label="var (loop variable)" />
      ),
      isEdited: isTextFieldEntryEdited,
    });
    entries.push({
      id: "rebar-loop-max",
      component: (p) => (
        <ConfigNumberEntry
          {...p}
          ckey="max_iterations"
          label="max_iterations"
        />
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
      component: (p) => (
        <ConfigTextEntry {...p} ckey="over" label="over" required />
      ),
      isEdited: isTextFieldEntryEdited,
    });
    entries.push({
      id: "rebar-map-as",
      component: (p) => (
        <ConfigTextEntry {...p} ckey="as" label="as (item variable)" />
      ),
      isEdited: isTextFieldEntryEdited,
    });
    entries.push({
      id: "rebar-map-index",
      component: (p) => (
        <ConfigTextEntry {...p} ckey="index_var" label="index_var" />
      ),
      isEdited: isTextFieldEntryEdited,
    });
    entries.push({
      id: "rebar-map-conc",
      component: (p) => (
        <ConfigNumberEntry
          {...p}
          ckey="max_concurrency"
          label="max_concurrency"
        />
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
    // Branch's only step config is the `when` condition, edited via a structured field
    // (a83a). The deeper branch UX — arm add/remove + connection routing — is deferred
    // S9 scope. No raw JSON editor: any non-`when` keys round-trip via the slice-write.
    entries.push({ id: "rebar-when", component: WhenEntry });
    return { id: "rebar", label: "Step behavior", entries };
  }

  if (STRUCTURED_KINDS.includes(kind)) {
    // KNOWN kind: structured fields are the sole editor. Keys outside the structured set
    // are not shown but are preserved verbatim by the slice-write (mutateConfig), so no
    // raw JSON fallback is needed and none is offered.
    entries.push(...structuredEntries(element, kind));
    return { id: "rebar", label: "Step behavior", entries };
  }

  // A bare sub-process (or any other rebar element with no structured step config) shows
  // its kind only; it carries no editable step config, so there is no raw JSON editor.
  return { id: "rebar", label: "Rebar", entries };
}

class RebarPropertiesProvider {
  constructor(propertiesPanel, modeling, bpmnFactory) {
    this._modeling = modeling;
    this._bpmnFactory = bpmnFactory;
    propertiesPanel.registerProvider(LOW_PRIORITY, this);
  }
  getGroups(element) {
    return (groups) => {
      const bo = element.businessObject;
      if (bo && REBAR_KINDS.includes(bo.$type)) {
        groups.push(rebarGroup(element));
        const k = rebarKind(bo);
        // A batch step gets two more groups: its editable, add/remove criteria LIST and the
        // model-ladder LIST (story B-UX item 18).
        if (k === "batch") {
          groups.push(
            batchCriteriaGroup(element, this._modeling, this._bpmnFactory),
          );
          groups.push(
            modelLadderGroup(element, this._modeling, this._bpmnFactory),
          );
        }
        // The in-panel authoring forms (create criterion/prompt + create overlay trigger) are
        // reachable from a batch criterion's pickers AND from an agent step's prompt/`if`
        // pickers and a scripted step's `if` picker (story B-UX items 7 + 9), so the group is
        // rendered for all of those kinds.
        if (k === "batch" || k === "agent" || k === "scripted") {
          groups.push(authoringGroup(element));
        }
      }
      return groups;
    };
  }
}
RebarPropertiesProvider.$inject = [
  "propertiesPanel",
  "modeling",
  "bpmnFactory",
];

export default {
  __init__: ["rebarPropertiesProvider"],
  rebarPropertiesProvider: ["type", RebarPropertiesProvider],
};
