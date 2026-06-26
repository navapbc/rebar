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
  const { element, id, ckey, label, required, description } = props;
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

// The model_ladder (cfg.batch.model_ladder) — an ordered escalation list, edited as a
// comma-separated field and stored as a string array (empty clears it).
function BatchLadderEntry(props) {
  const { element, id } = props;
  const modeling = useService("modeling");
  const bpmnFactory = useService("bpmnFactory");
  const debounce = useService("debounceInput");
  const getValue = () => {
    const v = (parseConfig(element.businessObject).batch || {}).model_ladder;
    return Array.isArray(v) ? v.join(", ") : "";
  };
  const setValue = (v) => {
    mutateConfig(element, modeling, bpmnFactory, (cfg) => {
      const batch = cfg.batch && typeof cfg.batch === "object" ? cfg.batch : {};
      const list = String(v || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      if (list.length) batch.model_ladder = list;
      else delete batch.model_ladder;
      cfg.batch = batch;
    });
  };
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="model_ladder (comma-separated)"
      getValue={getValue}
      setValue={setValue}
      debounce={debounce}
    />
  );
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
  targetIndex: null,
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
  authoring.targetIndex = null;
  authoring.targetElementId = "";
  authoring.triggerOpen = false;
  authoring.triggerName = "";
  authoring.triggerKeywords = "";
  authoring.triggerTargetIndex = null;
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
    const desc = e.description ? ` — ${e.description}` : "";
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
      label="when (overlay predicate)"
      getValue={getValue}
      setValue={setValue}
      getOptions={() => overlayWhenOptions(getStored())}
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
      : "Pick “➕ Create new…” in a criterion's prompt, or “➕ New trigger…” in its when, to author here.";
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
      a.open = false;
      a.status = "created " + newId;
      a.id = "";
      a.body = "";
      if (idx != null) {
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
    a.triggerOpen = false;
    a.status = "added trigger " + name;
    a.triggerName = "";
    a.triggerKeywords = "";
    if (idx != null) {
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
    component: (p) => (
      <ConfigTextEntry
        {...p}
        ckey="if"
        label="if (overlay predicate)"
        description="Step is included only when this expression is truthy (else skipped)."
      />
    ),
    isEdited: isTextFieldEntryEdited,
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
    entries.push({
      id: "rebar-batch-ladder",
      component: BatchLadderEntry,
      isEdited: isTextFieldEntryEdited,
    });
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
    return { id: "rebar", label: "Rebar", entries };
  }

  if (STRUCTURED_KINDS.includes(kind)) {
    // KNOWN kind: structured fields are the sole editor. Keys outside the structured set
    // are not shown but are preserved verbatim by the slice-write (mutateConfig), so no
    // raw JSON fallback is needed and none is offered.
    entries.push(...structuredEntries(element, kind));
    return { id: "rebar", label: "Rebar", entries };
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
        // A batch step gets two more groups: its editable, add/remove criteria LIST, and the
        // in-panel authoring forms (create criterion/prompt + create overlay trigger).
        if (rebarKind(bo) === "batch") {
          groups.push(
            batchCriteriaGroup(element, this._modeling, this._bpmnFactory),
          );
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
