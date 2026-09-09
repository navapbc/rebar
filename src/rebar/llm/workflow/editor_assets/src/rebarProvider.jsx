/**
 * Typed Rebar property editor for semantic BPMN elements. Each control mutates one slice
 * of the shared rebar:Config blob, so unknown keys survive round trips. There is no raw
 * JSON editor; invalid field values show errors without replacing prior data.
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

// Parse empty or malformed rebar:Config as {} for safe structured reads; live validation
// reports malformed source instead of exposing raw JSON editing.
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
  // Show server-resolved prompt text read-only. The IR keeps an id; prompt editing remains
  // a git-file concern.
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
  // Render scripted input/output contracts read-only. Missing or opaque contracts show
  // explicit empty/unchecked states rather than a blank that implies success.
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
  // Edit only the branch `when` slice here; arm and connection authoring is deferred.
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

// ── Structured fields ───────────────────────────────────────────────────────────
// Each entry mutates one config slice. Coercion accepts strings, true/false, and finite
// numbers; invalid typed text reports an error and skips the write.
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

// Numeric config stays in a validated text field so bad input remains visible and cannot
// silently replace the prior value.
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

// ServiceTasks can switch between agent and batch. Batch seeds cfg.batch; agent removes it;
// slice writes preserve all other config.
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

// ── Batch fields ────────────────────────────────────────────────────────────────
// Batch parameters use cfg.batch slice writes. Its required prompt selects the finder
// applied to each batch.
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

// Edit cfg.batch.model_ladder as ordered add/remove rows, preserving its array slice.
function readLadder(element) {
  const v = (parseConfig(element.businessObject).batch || {}).model_ladder;
  return Array.isArray(v) ? v : [];
}

function writeLadder(element, modeling, bpmnFactory, list) {
  mutateConfig(element, modeling, bpmnFactory, (cfg) => {
    const batch = cfg.batch && typeof cfg.batch === "object" ? cfg.batch : {};
    // Preserve trimmed empty rows while they are being edited; lint rejects them on save.
    // Remove the key only after the last row is deleted.
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

// ── Library-backed criterion authoring ─────────────────────────────────────────
// Prompt and trigger dropdowns use injected library/IR catalogs. The panel can create
// entries and updates its local overlay-trigger catalog as they are added.

const SENTINEL_CREATE = "__rebar_create__"; // "➕ Create new criterion/prompt…" in the prompt select
const SENTINEL_NEW_TRIGGER = "__rebar_new_trigger__"; // "➕ New trigger…" in the when select
const WHEN_ALWAYS = "__rebar_always__"; // "(always include)" → clears `when`

// Module-local transient form state never enters rebar:Config. Subscribers force panel
// rerenders because non-model events do not rerun getGroups.
const authoring = {
  open: false,
  kind: "criterion",
  id: "",
  body: "",
  // Plan-review criterion routing is posted only for criterion authoring. Defaults mirror
  // the packaged floor so the minimal form produces a valid activated overlay.
  routingExec: "1-TURN", // 1-TURN | 2-STEP | AGENT | DET
  routingGate: "plan_review", // which review the criterion belongs to: plan_review | code_review
  routingScope: "container,leaf", // applies_at.scope (comma-separated) — plan_review only
  routingAppliesTo: "", // applies_to globs (comma-separated) — code_review only
  routingBlockThreshold: "0.95", // block_threshold, number in [0,1]
  routingPosture: "advisory", // default_posture: advisory | blocking
  routingFailMode: "open", // DET only: open | closed
  routingDetector: "", // DET only: a detector id, or `<prefix>*` for an id_prefix class
  targetIndex: null,
  // Assign a created prompt id either to a batch criterion or to the selected step name.
  targetKind: "criterion",
  // Assign a new trigger expression either to a criterion `when` or a step-level `if`.
  triggerTargetKind: "criterion",
  // Remember the form's BPMN element so selection changes cannot redirect its write.
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
  authoring.routingGate = "plan_review";
  authoring.routingScope = "container,leaf";
  authoring.routingAppliesTo = "";
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

// Select cfg.if from workflow triggers. “Always include” clears it; creating a trigger
// targets this step's predicate.
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

// Select an agent prompt from the library through the element name. Preserve unknown ids as
// custom options, and open the authoring form for new prompts.
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

// Batch criteria are collapsible prompt/when rows; add/remove actions mutate the
// cfg.batch.criteria slice with provider-captured modeling services.
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

// ── Authoring forms ─────────────────────────────────────────────────────────────
// Store-driven fields render only for the active form. An always-visible status keeps the
// group present and reports the latest result.
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

// Mirror criterion_prompt_id: map project.<name> to the filesystem-safe
// plan-review-project-<name>.md form. Keep this synchronized with Python.
function criterionPromptId(cid) {
  return "plan-review-" + String(cid || "").replace(/\./g, "-");
}

function AuthoringIdEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const debounce = useService("debounceInput");
  if (!a.open) return null;
  const isCriterion = a.kind === "criterion";
  const cid = (a.id || "").trim();
  // Surface WHAT will be created so the author isn't guessing: for a net-new PROJECT criterion the
  // id is `project.<name>` (dotted namespace); its rubric lands at the sanitized prompt file.
  let description;
  if (isCriterion && cid) {
    description = cid.startsWith("project.")
      ? `Creates project criterion “${cid}” — rubric → .rebar/prompts/${criterionPromptId(cid)}.md`
      : `Re-tunes built-in “${cid}”. (For a NEW project criterion, use project.<name>.)`;
  } else if (isCriterion) {
    description = "For a NEW project criterion use project.<name> (name = letters/digits/dashes).";
  }
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label={isCriterion ? "criterion id (project.<name> for a new one)" : "new id (letters/digits/dashes)"}
      description={description}
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

// ── Criterion routing form ──────────────────────────────────────────────────────
// Visible only for criterion authoring; each field feeds /library/create routing.
// fail_mode and detector apply only to DET entries.
function isCriterionAuthoring(a) {
  return a.open && a.kind === "criterion";
}

// Build routing only for genuine criterion activation. A batch criterion's referenced
// prompt is not an overlay id, so its picker returns null instead of forcing project.*
// validation and stranding the step reference.
function buildRouting(a) {
  if (a.kind !== "criterion" || a.targetKind === "criterion") return null;
  const gate = a.routingGate || "plan_review";
  const n = parseFloat(a.routingBlockThreshold);
  const routing = {
    gate,
    exec: a.routingExec || "1-TURN",
    block_threshold: Number.isFinite(n) ? n : 0.95,
    default_posture: a.routingPosture || "advisory",
  };
  if (gate === "code_review") {
    // A code-review project LLM criterion targets changed files via applies_to globs; an
    // empty list is left in place so the backend refuses it fail-loud (RP-06 S6 AC3).
    routing.applies_to = (a.routingAppliesTo || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  } else {
    const scope = (a.routingScope || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    routing.applies_at = scope.length ? { scope } : {};
  }
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

function AuthoringRoutingScopeEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const debounce = useService("debounceInput");
  // applies_at.scope is a PLAN-REVIEW concept; hide it when authoring a code-review criterion
  // (which targets changed files via applies_to globs instead).
  if (!isCriterionAuthoring(a) || a.routingGate === "code_review") return null;
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label="applies_at scope (comma-separated: container,leaf)"
      getValue={() => a.routingScope}
      setValue={(v) => {
        a.routingScope = v || "";
      }}
      debounce={debounce}
    />
  );
}

function AuthoringRoutingGateEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  if (!isCriterionAuthoring(a)) return null;
  return (
    <SelectEntry
      id={id}
      element={element}
      label="gate (which review this criterion runs in)"
      getValue={() => a.routingGate}
      setValue={(v) => {
        a.routingGate = v || "plan_review";
        notifyAuthoring();
      }}
      getOptions={() => [
        { value: "plan_review", label: "plan_review (ticket plans)" },
        { value: "code_review", label: "code_review (changed files)" },
      ]}
    />
  );
}

function AuthoringRoutingAppliesToEntry(props) {
  const { element, id } = props;
  const a = useAuthoring();
  const debounce = useService("debounceInput");
  // applies_to globs are REQUIRED for a code-review project LLM criterion; use ["**"] for a
  // repository-wide one. Only shown (and only sent) for the code_review gate.
  if (!isCriterionAuthoring(a) || a.routingGate !== "code_review") return null;
  return (
    <TextFieldEntry
      id={id}
      element={element}
      label='applies_to globs (comma-separated; use ** for repository-wide)'
      getValue={() => a.routingAppliesTo}
      setValue={(v) => {
        a.routingAppliesTo = v || "";
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
      a.routingScope = "container,leaf";
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
  // Close forms opened for another element so module-global state cannot target this
  // element's remembered criterion index.
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
        id: "rebar-author-routing-gate",
        component: AuthoringRoutingGateEntry,
        isEdited: isSelectEntryEdited,
      },
      {
        id: "rebar-author-routing-scope",
        component: AuthoringRoutingScopeEntry,
        isEdited: isTextFieldEntryEdited,
      },
      {
        id: "rebar-author-routing-applies-to",
        component: AuthoringRoutingAppliesToEntry,
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

// ─── Read-only Effective Policy provenance ─────────────────────────────────────
// Render the compiled projection separately from authored controls, with no write path.
// Unavailable payloads show the compiler's located remedy.
function formatEffectivePolicy(state) {
  if (!state || state.loading) return "Loading effective policy…";
  if (state.error) return "⚠ effective policy unavailable\n" + state.error;
  const view = state.view || {};
  if (view.available === false) {
    return (
      "⚠ effective policy unavailable\n" +
      (view.unavailable_reason || "(no reason reported)")
    );
  }
  const crits = Array.isArray(view.criteria) ? view.criteria : [];
  const lines = ["digest: " + (view.digest || "(none)"), ""];
  if (!crits.length) lines.push("(no criteria)");
  for (const c of crits) {
    lines.push(
      `${c.id}  [${c.gate}]`,
      `  tier=${c.tier}  posture=${c.posture}  enabled=${c.enabled}`,
      `  applicability: ${c.applicability}`,
      `  source: ${c.source}`,
      `  (${c.reason})`,
      "",
    );
  }
  return lines.join("\n");
}

function EffectivePolicyEntry(props) {
  const { element, id } = props;
  const debounce = useService("debounceInput");
  const [state, setState] = useState({ loading: true });
  useEffect(() => {
    let live = true;
    fetch("/effective-policy", {
      headers: { "X-Rebar-Token": window.REBAR_TOKEN },
    })
      .then((r) => {
        // fetch does NOT reject on an HTTP error status, so an auth/other failure returns a
        // {"errors":[...]} body that must NOT be shown as if it were effective policy — surface
        // it as an explicit error instead.
        if (!r.ok) {
          return r
            .json()
            .catch(() => ({}))
            .then((b) => {
              throw new Error((b.errors || ["HTTP " + r.status]).join("; "));
            });
        }
        return r.json();
      })
      .then((view) => {
        if (live) setState({ view });
      })
      .catch((e) => {
        if (live) setState({ error: e.message });
      });
    return () => {
      live = false;
    };
  }, []);
  return (
    <TextAreaEntry
      id={id}
      element={element}
      label="Effective Policy (read-only)"
      rows={14}
      getValue={() => formatEffectivePolicy(state)}
      setValue={() => {}}
      debounce={debounce}
      disabled
    />
  );
}

// The read-only Effective Policy group — provenance-only, distinct from the authored workflow.
function effectivePolicyGroup() {
  return {
    id: "rebar-effective-policy",
    label: "Effective Policy (read-only)",
    entries: [
      { id: "rebar-effective-policy-view", component: EffectivePolicyEntry },
    ],
  };
}

function rebarGroup(element) {
  const bo = element.businessObject;
  const kind = rebarKind(bo);
  const entries = [{ id: "rebar-kind", component: KindEntry }];

  if (kind === "branch") {
    // Branch editing covers only structured `when`; other keys survive the slice write
    // while arm and connection authoring remains deferred.
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
        // Keep read-only compiled policy provenance separate from authored workflow controls.
        groups.push(effectivePolicyGroup());
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
        // Render authoring forms for batch criteria, agent prompt/if pickers, and scripted
        // if pickers—the entry points that can create their referenced records.
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
