# Reusable machinery — developer API reference

This is the **reuse/extension reference** for the load-bearing subsystems a new
rebar capability is most likely to build *on*: the HMAC **signing** surface, the
**LLM workflow runtime** (the runner + the declarative workflow executor), the
**prompt/contract** model, and the **output-schema** seam. It complements the
higher-level docs — [llm-framework.md](llm-framework.md) (why the LLM framework is
shaped this way), [event-schema.md](event-schema.md) (the event-level view of
`SIGNATURE`), and [workflow-authoring-v2.md](workflow-authoring-v2.md) (authoring
workflows) — by giving the **exact signatures, return shapes, invariants, and
extension points** for the next agent.

Worked consumer of all four: the plan-review gate
([plan-review-gate.md](plan-review-gate.md), `src/rebar/llm/plan_review/`).

> Audience: human developers and LLM agents. Every signature below is verified
> against the current source by `tests/unit/test_reuse_surface_doc.py` (a CI
> anti-drift gate that introspects each documented callable) — re-verify with
> `inspect.signature(...)` if in doubt.

---

## 1. The signing surface — `rebar.signing`

HMAC-SHA256 attestation over a ticket's **manifest of verified steps**, computed
with an **environment-specific** key. Used by the completion close gate and the
plan-review claim gate; reuse it for any "this was verified" attestation. No new
key custody is ever needed — there is exactly one environment key.

### Key custody

```python
signing.signing_key(tracker, *, create_if_missing=True) -> bytes
signing.key_fingerprint(key: bytes) -> str        # domain-separated SHA-256 prefix; NEVER the key
```

Resolution order: `REBAR_SIGNING_KEY` env var → `<tracker>/.signing-key` (a
git-ignored, atomically-created UUID4, one per environment). On the **verify** path
always pass `create_if_missing=False` — a read must never mint a secret on disk (a
key-less environment then honestly reports `foreign_key`/`unsigned`).
`key_fingerprint` is what a signature records as `key_id`, so verification can tell
a *tampered manifest* apart from a signature made by a *different environment*
without exposing the key.

### Manifest + canonicalisation

```python
signing.parse_manifest(payload) -> list[str]      # validates a JSON array of non-empty strings
signing.compute_signature(ticket_id, manifest, key) -> str   # hex HMAC over the canonical payload
```

A **manifest** is a list of non-empty strings (the "verified steps"). The signed
payload is `(ticket_id, manifest)` canonicalised as sorted-key compact JSON
(`PAYLOAD_VERSION = 1`, `ALGORITHM = "HMAC-SHA256"`). Keep manifests
**deterministic** (no timestamps inside) so re-signing the same verified state is
reproducible. Convention: the first line names the attestation kind, e.g.
`"completion-verifier: PASS"` or `"plan-review: PASS"`, so a verifier can tell the
two gates' signatures apart on the same `state['signature']`.

### Sign + verify (library operations)

```python
signing.sign_manifest(ticket_id, manifest, *, kind=None, repo_root=None) -> dict
# → {manifest, algorithm, signature, key_id, head_sha, signed_at, ticket_id}
# Appends a SIGNATURE event through the single locked write path. Raises SigningError.
# `kind` (e.g. "plan-review"/"completion-verifier") is recorded UNSIGNED as a routing
# hint for the reducer's kind-keyed attestations map; it never enters the signed payload.

signing.verify_signature(ticket_id, *, kind=None, repo_root=None) -> dict
# READ-only (never mints a key). Reduces the ticket and verifies one signature record.
# `kind=None` verifies the most-recent `signature` mirror (back-compat); an explicit kind
# ("plan-review"/"completion-verifier") verifies that kind strictly from the attestations map.
# → the verdict dict (below) + ticket_id (+ `kind` when one was requested).

signing.verify_attestations(ticket_id, *, repo_root=None) -> dict
# READ-only. Verifies EVERY attestation kind on the ticket → {kind: verdict_dict} (sorted),
# {} when none. The per-kind companion to verify_signature.

signing.verify_record(record: dict | None, ticket_id: str, key: bytes) -> dict
# Pure, no I/O. The core verifier (use when you already hold the record + key).
```

The **verdict** dict: `{verified: bool, verdict, reason, manifest, step_count,
algorithm, key_id, signed_at, head_sha, ...}` where `verdict` ∈:

| verdict | meaning |
|---------|---------|
| `certified` | the manifest matches the HMAC under *this* environment's key |
| `mismatch` | a signature exists but does not verify (tampered manifest / bad sig) |
| `foreign_key` | signed by a *different* environment's key (or no local key) |
| `unsigned` | no signature on the ticket |

### Git-state (freshness) binding

```python
signing.head_sha(repo_root) -> str    # current HEAD sha, or 'unknown' when unresolvable
```

Every signature records `head_sha`. A gate enforces freshness by recomputing the
current HEAD and rejecting when `result['head_sha'] != head_sha` — treat
`'unknown'` as **never matchable** (else `'unknown' == 'unknown'` would void the
guard). This binds an attestation to the code at review time.

### The `SIGNATURE` event

`sign_manifest` persists a `SIGNATURE` event; the reducer folds it into
`state['signature']` **last-writer-wins** by replay (filename) order — concurrent
signs converge deterministically to the lexicographically-last `{ts}-{uuid}-SIGNATURE.json`
(re-sign to supersede). See [event-schema.md](event-schema.md).

### Worked example — a NEW gate reusing the surface

```python
from rebar import signing, config

# 1. SIGN on a passing verification (a deterministic manifest; bind any extra state
#    you want invalidated, e.g. a content fingerprint, IN the manifest so HMAC
#    protects it):
manifest = [f"my-gate: PASS", f"ticket: {tid}", f"material: {fingerprint}"]
signing.sign_manifest(tid, manifest, repo_root=root)

# 2. VERIFY fast at the gate (no LLM, no network — a pure HMAC verify):
res = signing.verify_signature(tid, repo_root=root)
ok = (
    res["verified"]
    and res["manifest"][0].startswith("my-gate:")
    and res.get("head_sha") == signing.head_sha(config.repo_root(root)) != "unknown"
)
```

The plan-review gate's `attest.py` is exactly this pattern, adding a material
fingerprint bound into the manifest for material-edit invalidation.

---

## 2. The LLM workflow runtime

Two ways to run an LLM operation, both behind the same `Runner` seam (so both are
exercisable offline with a `FakeRunner`):

### 2a. The runner seam — `rebar.llm.runner`

```python
@dataclass
class RunRequest:
    system_prompt: str
    instructions: str
    config: LLMConfig
    reviewers: list[str] = []
    target: dict = {}
    langfuse_prompt: object | None = None
    output_schema: str | None = None   # a registered contract/schema NAME (§3, §4)
    mode: str = "findings"             # "findings" | "structured" | "text"
    execution_mode: str = "agentic"    # "agentic" (tool loop) | "single_turn" (ONE call, no tools)
    extra_tools: list | None = None
    thinking: bool = False

@runtime_checkable
class Runner(Protocol):
    name: str
    def run(self, req: RunRequest) -> dict: ...
    def preflight(self) -> None: ...    # offline readiness check; raises LLMConfigError

get_runner(config: LLMConfig, *, override: Runner | None = None) -> Runner
```

`mode` shapes the **output**: `findings` → the `review_result` pipeline;
`structured` → the agent's structured payload, validated against `output_schema`
(§4); `text` → `{text, runner, model, trace_id}`. `execution_mode` is *how the
runner drives the model*: `agentic` gives the full filesystem + rebar (+ MCP) tool
surface in a tool loop; `single_turn` is exactly **one** model call with **no**
tools (the structured-output path). Set `mode="structured"` + `output_schema=<name>`
together for a structured single-turn extraction.

**Runners.** `PydanticAIRunner` (default; provider-agnostic — the provider is
chosen by the model string `anthropic:` / `openai:` / `google-gla:`; needs the
`[agents]` extra) and `FakeRunner` (the offline/test seam — no model, no network):

```python
FakeRunner(findings=None, summary=None, structured=None)
# mode="structured" → returns `structured` validated against output_schema + provenance.
# mode="findings"   → returns finalize_findings(findings, ...).
```

Always call `runner.preflight()` before `run()` so a missing extra / misconfig
surfaces as a clean `LLMConfigError` **before** any billable call (this is what
lets a gate degrade cleanly on missing infra).

**Pattern for a custom multi-call operation** (e.g. the plan-review's three
model-driven passes — find/verify/coach; Pass 3 "decide" is pure arithmetic, no call):
build one `RunRequest` per call with the right `system_prompt` / `instructions` /
`output_schema` / `execution_mode`, call `runner.run(req)`, and read the validated
dict. See `rebar.llm.plan_review.passes`.

### 2b. The declarative workflow executor — `rebar.llm.workflow`

For a *declarative* multi-step workflow (a `.rebar/workflows/*.yaml` IR), use the
executor instead of hand-driving the runner:

```python
from rebar.llm.workflow.executor import run_workflow, new_run_id, RunResult

run_workflow(
    doc: Mapping,                       # the parsed workflow IR (steps, inputs, …)
    inputs: Mapping | None = None,
    *, run_id: str | None = None,       # new_run_id() — globally-unique, sortable
    target_ticket: str | None = None,   # run-state events persist on this ticket
    repo_root: str | None = None,
    scripted_registry: Mapping[str, ScriptedStep] | None = None,  # deterministic (code) steps
    agent_runner: AgentStepRunner | None = None,                  # inject a runner (tests)
    recorder: RunRecorder | None = None,
    secrets: Mapping[str, str] | None = None,
) -> RunResult
```

Step kinds include deterministic **scripted** steps (pure code, registered via
`scripted_registry`), **agent** steps (an LLM call via the runner — `mode`/
`execution_mode`/`output_schema` come from the step's prompt front-matter, §3),
plus control flow (conditional / loop / map). A run and its per-step records persist
as `WORKFLOW_RUN` / `WORKFLOW_STEP` events on `target_ticket`; read them back with
the `get_workflow_status` / `get_workflow_result` tools. `run_workflow(dry_run …)`
via the CLI uses a `FakeRunner` so a workflow can be validated end-to-end with no
tokens. CLI/MCP: `rebar workflow <new|validate|run|status|result>` / `run_workflow`.
See [workflow-authoring-v2.md](workflow-authoring-v2.md) for the IR + authoring.

**When to use which.** A single structured LLM call, or a few you orchestrate in
Python with your own control flow (loops, fan-out, deterministic aggregation) →
drive the **runner** directly (like the plan-review passes). A reusable, declarative,
author-editable pipeline → a **workflow**.

---

## 3. The prompt library — `rebar.llm.prompting.prompts`

The prompt library is the **single source of truth for prompt TEXT**: every prompt
is a git-canonical, front-matter-bearing `*.md` file. Prompts are **never inline
string constants in Python** — an operation resolves its prompt from the library so
the text is reviewable, project-overridable, content-hashed into traces, and never
silently divergent. This section is the full reference; the canonical reviewer
examples live in `src/rebar/llm/reviewers/` and the plan-review prompts there too.

### Where prompts live + how they resolve

| Layer | Path | Notes |
|-------|------|-------|
| **Packaged (built-in)** | `src/rebar/llm/reviewers/<file>.md` | Ships in the wheel. The id is the file stem with `_`→`-` (`ticket_quality.md` → `ticket-quality`). |
| **Project override** | `<repo>/.rebar/prompts/<id>.md` | **Wins** over the packaged prompt of the same id (project > built-in). The override seam for adopters. |
| **Variant overlay** | `<id>.<variant>.md` | Overlays a base via `variant_of` front-matter + a `<!--base-->` splice marker; cycle-guarded. |

```python
prompts.get_prompt(prompt_id, *, repo_root=None) -> Prompt
# Resolves override → packaged; parses front-matter. Prompt fields: id, text (body,
# front-matter stripped), category, execution_mode, inputs, outputs, dimension,
# applies_to, default, title, description. `is_reviewer` == (category == "review").

prompts.resolve_prompt(reviewer_or_prompt, variables, langfuse_cfg=None,
                       *, repo_root=None, variant=None) -> (compiled_text, meta)
# Renders {{var}} STRICTLY (an unsupplied used var raises — never a silent empty),
# applies any variant overlay, and returns the compiled system prompt + meta
# (content_sha256 + provenance, threaded into traces). Langfuse is NEVER read for text.
```

### The front-matter contract (closed key set)

`rebar.llm.prompting.prompts.FRONT_MATTER_KEYS` (canonical emit order):
`schema_version`, `title`, `description`, `inputs`, `outputs`, `execution_mode`,
`category`, `model`, `tags`, `dimension`, `applies_to`, `langfuse_prompt`, `default`.

- **`category`** — free text; `review` marks the prompt as a reviewer (the only
  category that populates the reviewer index). Other categories (e.g.
  `plan-review-criterion`, `plan-review-pass`) are ordinary prompts excluded from
  the index.
- **`execution_mode`** — `single_turn` (one model call, no tools) | `agentic` (the
  tool-using loop). Flows into `RunRequest.execution_mode`. Absent → `agentic`.
- **`inputs` / `outputs`** — schema-registry **names** (never inline schemas);
  `outputs` is the structured-output contract (§4).
- **`dimension` / `applies_to` / `default`** — reviewer selection metadata (the
  rule layer `select_reviewers` uses).
- **Unknown keys are WARN+PRESERVEd** by the writer, BUT a *shipped built-in* prompt
  must use only the closed set: `test_built_in_prompt_round_trips_canonically`
  asserts every packaged prompt is byte-identical to `write_front_matter(parse(file))`
  with **no warnings**. So built-ins carry only closed keys and are canonical.

### Derived index + CI gates

- **Reviewer index** — `reviewers/index.json` is DERIVED from the `category: review`
  prompts' front-matter (`regenerate_prompt_index()` / `python -m rebar.llm.prompting.prompts
  regenerate-index`). It is the offline-testable selection catalog; a CI **drift
  gate** regenerates-then-diffs it. Invariants: exactly one `default: true` reviewer,
  no `dimension` collision.
- **Canonical-form gate** — see above; keeps built-ins clean + round-trippable.
- **Parity gate** — `check_prompt_parity` diffs declared `variables` vs the
  `{{vars}}` actually used.

### Separating prompt TEXT from routing metadata (the reuse pattern)

The library deliberately holds only prompt TEXT + the closed contract. Richer,
domain-specific routing/selection metadata lives in a **derived index** beside it:

- Reviewers: prompt text in `reviewers/*.md` + selection rules in `index.json`.
- The **plan-review gate** mirrors this exactly: each criterion's RUBRIC is a library
  prompt (`reviewers/plan_review_<id>.md`, `category: plan-review-criterion`,
  resolved via `get_prompt` + `.rebar/prompts/` overrides), and its routing (`exec`,
  `applies_at`, `block_threshold`, `default_posture`, `checklist`) is the derived
  `src/rebar/llm/plan_review/criteria_routing.json`. The five pass prompts
  (`plan_review_finder` / `verifier` / `coach` / `isf_finder` / `container`) are
  `category: plan-review-pass` library prompts resolved via `resolve_prompt`. Use
  this split whenever your prompts need metadata that doesn't fit the closed key set,
  rather than inlining prompts or stuffing custom keys into a built-in's front-matter.

### Adding a prompt

1. Write `src/rebar/llm/reviewers/<name>.md` with canonical front-matter (only closed
   keys) + a body using `{{var}}` placeholders. Keep it byte-canonical (author via
   `write_front_matter`, or run the canonical-form test).
2. If it's a `category: review` reviewer, run `python -m rebar.llm.prompting.prompts
   regenerate-index` and commit the updated `index.json`.
3. Resolve it: `p = get_prompt("<id>", repo_root=...)`; `system, _ =
   resolve_prompt(p, {var: ...}, repo_root=...)`; pass `system` +
   `output_schema=<p.outputs>` into a `RunRequest` (§2a). Projects can override it at
   `.rebar/prompts/<id>.md`.

---

## 4. The output-schema / contract seam

Two halves let any operation declare its **own** structured-output shape by NAME:

### 4a. Response-model contracts — `rebar.llm.contracts`

```python
contracts.register_contract(name: str, builder: Callable[[], type]) -> None
contracts.response_model_for(output_schema: str | None) -> type   # the Pydantic model (or findings default)
```

`register_contract` stores a **zero-arg builder** that returns a Pydantic
`BaseModel` subclass (import pydantic *inside* the builder so registration stays
import-clean). The runner's structured path calls `response_model_for(output_schema)`
to bind the model for constrained/validated generation. Register at import time:

```python
def _my_model():
    from pydantic import BaseModel, Field
    class Out(BaseModel):
        items: list[str] = Field(default_factory=list)
    return Out

contracts.register_contract("my_output", _my_model)
# then: RunRequest(mode="structured", output_schema="my_output", ...)
```

(See `rebar.llm.plan_review.passes.register_contracts` for three registered
contracts — Pass-1 findings, Pass-2 verification, Pass-4 coach.)

### 4b. JSON-Schema validation — `rebar.llm.findings` + `rebar.schemas`

```python
findings.validate_structured(data: dict, output_schema: str | None) -> dict
# Best-effort: validates `data` against the PACKAGED JSON Schema named output_schema.
# No-ops when the name is unset / not a packaged schema.
# Raises FindingsError on a real validation failure.
# (jsonschema is a core runtime dependency, so the validator is always available.)
```

Packaged JSON Schemas live in `src/rebar/schemas/*.schema.json` and are named in
`rebar.schemas` (e.g. `COMPLETION_VERDICT`); `OUTPUT_SCHEMAS` maps a command name to
its schema for the CLI/library `--output json` contract; a schema-pin test keeps
each Pydantic contract (§4a) in lock-step with its JSON Schema. Because
`validate_structured` **no-ops on an unregistered name**, an *intermediate* pass
needs only a contract (§4a) — register a JSON Schema only when you want a
documented, validated, pinned output surface (e.g. a CLI `--output json` shape or an
MCP `outputSchema`). A model-produced result that should advertise **no**
outputSchema is documented as `NO_SCHEMA_EXEMPT` in
`tests/interfaces/facades/test_mcp_output_schema_coverage.py` (as `review_plan` and
`verify_completion` are).

Related helpers: `findings.finalize_outcome(outcome, mode=…, output_schema=…, …)`
(the runner's finalizer for all three modes), `findings.normalize_finding`,
`findings.resolve_citations`, `findings.build_result`.

---

## Invariants worth preserving

* **Import-clean:** `import rebar.llm` must pull no heavy stack — import pydantic /
  pydantic-ai / anthropic *inside* function bodies, never at module top.
* **Fail-open vs fail-closed:** evidence/coverage tools fail *open* (abstain, never
  a false accusation); enforcement gates fail *closed* when enabled and their
  trust machinery is unavailable, with a `--force` escape that is audit-logged.
* **Deterministic manifests:** no timestamps inside a signed manifest, so re-signing
  the same verified state is reproducible.
* **Last-writer-wins state events** (`SIGNATURE`, `FILE_IMPACT`, …) converge by
  replay order; **reducer-ignored sidecars** (`REVIEW_RESULT`) stay out of compiled
  state and the hot paths (add them to the write allow-list +
  `_NON_REPLAY_KNOWN_TYPES`, NOT `KNOWN_EVENT_TYPES`).

---

## The review kernel (the shared four-pass framework)

`rebar.llm.review_kernel` is the shared kernel every multi-pass review gate consumes:
**Pass-2** the finding-verifier + the single registered `verification` contract +
the verify orchestration (chunking, merge-by-global-index, the verifier-model
default); **Pass-3** the deterministic decision core (`pass3_decide` /
`pass3_over_findings`, per-criterion thresholds parameterized); **Pass-4** the
affirmative-coach mechanism + the pluggable move-registry schema (the applicability
filter + the subject validator + the deterministic render). The plan-review gate is
the worked reference consumer; the code-review gate (`b744`) builds on the same seam
without copying the passes. The consumer plug-points (criteria + routing, finder
prompts, the domain-context assembler, the verify-prompt preamble, the move-catalog),
the public entry points, the verifier-rules scaffold, and the enforcement rationale
(structure mechanically + behavior via evals; **no** prompt-text lint) are in
[review-kernel.md](review-kernel.md).

### Novelty convergence — shared kernel primitives vs the code-review region gate

The novelty rising floor is a further reuse case. The **shared kernel** owns the reusable
convergence primitives: `review_kernel.verify.novelty_model` / `NOVELTY_SUBANSWERS` /
`reshape_novelties` (the novelty scoring contract) and `review_kernel.decide.novelty` /
`rising_floor_drop(priority, novelty)` (the per-finding novelty math + the drop predicate). Both
review gates bind the SAME `novelty_model` — plan-review as `plan_review_novelty`, code-review as
`code_review_novelty` — and call `rising_floor_drop` unchanged.

What is **gate-specific** (NOT in the kernel, because it genuinely differs per gate) is the
orchestration around those primitives: plan-review's whole-artifact floor
(`plan_review/__init__.py::_maybe_apply_rising_floor`) vs code-review's **per-citation region
gate** (`code_review/region_gate.py` + `code_review/workflow_ops.py::apply_region_gated_floor`),
which ANDs `rising_floor_drop` with a content-addressed region check so a finding is dropped only
when its cited code region is unchanged. The novelty PROMPTS are gate-specific too
(`reviewers/plan_review_novelty.md` vs `reviewers/code_review_novelty.md`). See
**[ADR 0037](adr/0037-code-review-novelty-convergence.md)** and [review-kernel.md](review-kernel.md)
(§ Code-review novelty convergence).
