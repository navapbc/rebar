# Reusable machinery for developers

This reference documents the subsystem contracts that another rebar capability can reuse. It covers operation-certificate signing, generic HMAC primitives, the LLM workflow runtime, the prompt and contract model, and the output-schema seam. Related guides explain the [LLM framework](llm-framework.md), the [`SIGNATURE` event](event-schema.md), and [workflow authoring](workflow-authoring-v2.md).

The plan-review gate demonstrates the operation-certificate, LLM runtime, prompt, and output-schema surfaces. See [the plan-review guide](plan-review-gate.md) and `src/rebar/llm/plan_review/`.

> Audience: Human developers and LLM agents. `tests/unit/test_reuse_surface_doc.py` checks the documented callable signatures against the source. Use `inspect.signature(...)` for additional inspection.

## Public bridge operations — `rebar.*`

Use the noun-based facade for programmatic Jira bridge work:

```python
bridge_preview(*, only: list[str] | None = None, exclude: list[str] | None = None,
               repo_root=None) -> BridgeRun
bridge_run(profile: str | None = None, *, repo_root=None) -> BridgeRun
bridge_sync(*, only: list[str] | None = None, exclude: list[str] | None = None,
            max_changes: int | None = None, repo_root=None) -> BridgeRun
bridge_status(*, target_environment_id: str | None = None,
              max_age_seconds: int | None = None, repo_root=None) -> BridgeStatus
bridge_pause(reason: str, *, repo_root=None) -> BridgeControl
bridge_resume(*, repo_root=None) -> BridgeControl
bridge_check_access() -> BridgeAccessCheck
bridge_fsck(*, repo_root=None) -> BridgeFsck
```

`bridge_preview` selects the dry-run route and never applies changes;
`bridge_run` selects one scheduled compatibility profile, strictly delivers ticket events,
and returns captured stdout/stderr without printing; `bridge_sync` selects the live route and
may cap work with `max_changes`.
`only` and `exclude` are mutually exclusive. Status reads the durable witnesses,
pause/resume use the shared observed-OID CAS control ref, and access checking returns
the six-step provider result without invoking the CLI. Canonical invalid/operational
preview/sync outcomes raise `RebarError` with return code 2/1. `bridge_run` instead returns its
canonical 0/1/2 result so CLI and MCP adapters can render or transport the same structured value.

The compatibility facade `reconcile(mode="dry-run", *, repo_root=None) -> dict`
remains supported with its subprocess-visible return and exception contract. MCP
mirrors both sets of names; mutating bridge operations and mutating reconcile modes
require `REBAR_MCP_ALLOW_JIRA_SYNC`, and read-only mode blocks mutations. Setup is
interactive and intentionally has no library or MCP operation.

---

## 1. Signing surface in `rebar.signing`

Current `sign_manifest` writes an operation certificate for a ticket's manifest of verified steps. The record contains a DSSE envelope with an in-toto Statement. The envelope carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 key. The certificate principal identifies that environment. The plan-review and completion-verifier gates use this form. Generic HMAC primitives remain available for non-operation-certificate consumers.

### Operation-certificate key custody

```python
signing.ensure_opcert_key(tracker, *, create_if_missing=True, binding=None) -> str
signing.opcert_principal(tracker, *, binding=None) -> str
```

The default private key is `<tracker>/.opcert-key`, with a derived `.opcert-key.pub` sidecar. `REBAR_OPCERT_KEY_PATH` can select a provisioned private key. `REBAR_OPCERT_ENV_ID` supplies the environment principal when set, and the ticket store's `.env-id` supplies the default. A startup signer binding takes precedence when one is provided. The verification path does not create a missing private key.

### Manifests

```python
signing.parse_manifest(payload) -> list[str]
```

A manifest is a list of non-empty strings. Keep its content deterministic when repeated verification of the same state should produce the same predicate. The first line convention identifies the attestation kind, such as `"completion-verifier: PASS"` or `"plan-review: PASS"`. Current operation certificates bind the full manifest inside the signed in-toto predicate.

### Current sign and verify operations

```python
signing.sign_manifest(ticket_id, manifest, *, kind=None, repo_root=None, signer=None) -> dict
# Returns manifest, algorithm, envelope, material_fingerprint, merged_log_commit,
# principal, head_sha, signed_at, ticket_id, and kind when supplied.
# Appends a SIGNATURE event through the locked write path. Raises SigningError.

signing.verify_signature(ticket_id, *, kind=None, repo_root=None) -> dict
# Reduces the ticket and verifies one record without creating a key.
# kind=None selects the most recent compatibility mirror.
# An explicit kind selects that entry from the attestations map.

signing.verify_attestations(ticket_id, *, repo_root=None) -> dict
# Verifies every recorded kind and returns a sorted mapping from kind to verdict.

signing.verify_attestation_record(record, ticket_id, *, kind=None, key=None, repo_root=None) -> dict
# Dispatches by record shape to the operation-certificate or generic HMAC verifier.
```

An operation-certificate record uses `algorithm="sshsig"` and has no HMAC `signature` field. `verify_signature` performs same-environment verification. It requires the record principal to match the current environment and verifies the DSSE envelope through SSHSIG against that environment's Ed25519 public key. Verification of a certificate from another environment returns `foreign_key` on this path.

The verdict dictionary contains `verified`, `verdict`, `reason`, `manifest`, `step_count`, `algorithm`, `key_id`, `signed_at`, `head_sha`, and authenticated operation-certificate fields where available. Common verdicts include:

| Verdict | Meaning |
|---|---|
| `certified` | The selected certificate or generic HMAC record verifies under the applicable local key. |
| `mismatch` | The signature, signed subject, ticket binding, or kind binding does not verify. |
| `foreign_key` | The record identifies another environment or the local verification key is unavailable. |
| `invalid` | The operation-certificate envelope or payload is malformed. |
| `unavailable` | The configured signing scheme cannot run. |
| `unknown_kind` | No verification policy exists for the attestation kind. |
| `unknown_scheme` | The record uses a scheme that policy does not accept for the kind. |
| `unsigned` | No signature record exists in the selected slot. |

### Generic HMAC primitives

```python
signing.signing_key(tracker, *, create_if_missing=True) -> bytes
signing.key_fingerprint(key: bytes) -> str
signing.compute_signature(ticket_id, manifest, key) -> str   # hex HMAC over the canonical payload
signing.verify_record(record: dict | None, ticket_id: str, key: bytes) -> dict
```

These functions preserve the generic HMAC-SHA256 contract for consumers outside the two operation-certificate gates. `signing_key` resolves `REBAR_SIGNING_KEY` before `<tracker>/.signing-key`. `compute_signature` returns a hex HMAC over the versioned canonical payload containing the ticket ID and manifest. `verify_record` is a pure verifier when the caller already holds the record and key.

Do not use the generic HMAC path for `plan-review` or `completion-verifier`. A legacy HMAC record for either kind remains readable, but it returns `unknown_scheme` and cannot certify a current gated operation.

### Code and material freshness

```python
signing.head_sha(repo_root) -> str    # current HEAD sha, or "unknown" when unavailable
```

Current operation certificates bind the manifest, material fingerprint, and code commit inside the signed in-toto predicate. The record also retains `head_sha` for compatibility and unscoped freshness checks. Gate validity reads authenticated operation-certificate values before it compares ticket material, code state, reopen time, and related-ticket pins. Treat `"unknown"` as never matchable.

### The `SIGNATURE` event

`sign_manifest` persists a `SIGNATURE` event. The reducer stores the most recent record for each kind in `state['attestations']` and maintains `state['signature']` as the compatibility mirror of the most recent record. Replay order resolves concurrent writes in one slot. See [event-schema.md](event-schema.md).

### Worked example for a new gate

```python
from rebar import config, signing

manifest = [f"my-gate: PASS", f"ticket: {tid}", f"material: {fingerprint}"]
signing.sign_manifest(tid, manifest, repo_root=root)

res = signing.verify_signature(tid, repo_root=root)
ok = (
    res["verified"]
    and res["manifest"][0].startswith("my-gate:")
    and res.get("merged_log_commit")
    == signing.head_sha(config.repo_root(root))
    != "unknown"
)
```

The plan-review gate uses this path to store a DSSE envelope that carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 key and attributed to that environment. Its manifest also binds the material fingerprint used for material-edit invalidation.

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
    structured_retry_limit: int | None = None  # cap the output-retry allowance (0 ⇒ single-shot)

@runtime_checkable
class Runner(Protocol):
    name: str
    def run(self, req: RunRequest) -> dict: ...
    def preflight(self) -> None: ...    # offline readiness check; raises LLMConfigError

get_runner(config: LLMConfig, *, runtime=None, override: Runner | None = None) -> Runner
```

`mode` shapes the **output**: `findings` → the `review_result` pipeline;
`structured` → the agent's structured payload, validated against `output_schema`
(§4); `text` → `{text, runner, model, trace_id}`. `execution_mode` is *how the
runner drives the model*: `agentic` gives the full filesystem + rebar (+ MCP) tool
surface in a tool loop; `single_turn` is exactly **one** model call with **no**
tools (the structured-output path). Set `mode="structured"` + `output_schema=<name>`
together for a structured single-turn extraction.

**Structured is one bounded operation.** Every `mode="structured"` request routes through the
single `get_runner(...).run(...)` facade into one `_pai_structured` operation that issues
**exactly one outer `Agent.run_sync`** per output mode — no bespoke retry loop. Output repair is
the *in-Agent* bounded retry (`retries={"output": N}`): it adds a model request but not a second
`run_sync`, and its allowance N is single-sourced with the `UsageLimits` request budget by
`output_retry_allowance = min(OUTPUT_RETRIES, max(0, structured_retry_limit))`. So
`structured_retry_limit=0` means **single-shot** (zero output-repair retries — the overlap/judge
and contracts batch abstain fail-safe); the only sanctioned *second* outer run is the bug-895c
native→prompted downgrade. Full retry-layer/accounting contract:
[llm-framework.md](llm-framework.md) §"The structured retry layers and their accounting".

**Runners.** `PydanticAIRunner` (default; provider-agnostic — the provider is
chosen by the model string `anthropic:` / `openai-chat:` / `google-gla:`; needs the
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
**[ADR 0037](adr/0088-code-review-novelty-convergence.md)** and [review-kernel.md](review-kernel.md)
(§ Code-review novelty convergence).

## 5. The metrics registry — `rebar.metrics`

`rebar.metrics.registry` is a declarative registry that the `rebar metrics` command renders
once and never has to be modified as new signals accrue:

- `MetricSpec(id, lens, source, confidence, compute, accruing_since)` — one spec per metric;
  `compute` is a `Callable[[context], value | None]` (the context carries `repo_root`, `since`,
  `until`). Returning `None` yields an `Unavailable`.
- `REGISTRY` — the list of specs. It is hydrated by importing the `rebar.metrics` **package**
  (its `__init__` imports the reader modules `event_metrics` / `git_metrics` / `sidecar_metrics`,
  each registering its specs). Import the package, not just `rebar.metrics.registry`, to get a
  populated `REGISTRY`.
- `evaluate(spec, context) -> MetricValue | Unavailable` — runs a spec's `compute` and dispatches
  to a `MetricValue` (carrying `.value` + the spec's `source`/`confidence` labels) or an
  `Unavailable(reason, accruing_since)`.
- `is_authoritative(source) -> bool` — the segregation helper: True for the authoritative
  structural sources (`structural`/`git`/`sidecar`/`snapshot`), False for `backfill_classified`
  and any unknown source, so classified backfills never leak into an authoritative rollup.

The optional harness-specific adapters (`rebar.metrics.adapters.*` — GitHub-Actions, Claude
transcripts) are quarantined behind their backfill scripts and are **never** imported by the
core command or the registry, keeping the command harness-agnostic. See
[user-guide.md](user-guide.md) (§ Metrics) for the CLI view.

### Code-health analyzer seam

Analyzer-backed code-health metrics are isolated behind the
`rebar.metrics.analyzers` adapter modules and composed by `rebar.metrics.git_metrics`:
`scc_loc` provides LOC and module-size input, `lizard_complexity` provides complexity input,
and `jscpd_dup` provides duplication input. An adapter returns an `AnalyzerResult` for its
available signal or a structured `Unavailable`; registry composition then lets one unavailable
analyzer leave the other metrics usable. This is the extension seam for a new analyzer:
normalize its output to `AnalyzerResult` and preserve an honest `Unavailable` when the tool
cannot run.

The prerequisites are deliberately split:

- Install `nava-rebar[metrics]` for the optional Python dependency, **lizard**. The
  `[metrics]` extra contains only lizard.
- Install **scc** separately and make its executable available on `PATH` for LOC/module-size
  analysis.
- Install **jscpd** separately and make its executable available on `PATH` for duplication
  analysis.

Neither `scc` nor `jscpd` is a pip dependency of rebar. Their adapters resolve executables
from `PATH`; a missing or failing tool is reported as `Unavailable`, never as fabricated zero.

## Tracker-footprint measurement — `rebar._store.footprint`

Footprint accounting is reusable below the CLI without becoming a top-level `rebar.*`
facade or an ordinary metric:

```python
from rebar._store.footprint import FootprintError, measure_fresh_clone, measure_tracker

measure_tracker(tracker, *, remote: str, branch: str,
                mode: Literal["mounted", "fresh-clone"] = "mounted") -> dict[str, object]
measure_fresh_clone(repo_root) -> dict[str, object]
```

`measure_tracker` is a read-only filesystem/Git inspection of one already-materialized
tracker. It uses `StorePaths` to resolve Git's worktree-specific and common directories,
labels linked-worktree/alternates object databases as shared, and returns separate pack,
checkout, Git-directory, and whole-clone layers. The pack layer measures only the primary
common object database and carries a `complete` flag that is `false` when the checkout borrows
objects from an alternate object database, so a shared clone's near-empty primary pack is never
mistaken for the whole object store. Logical bytes count pathnames via `lstat`;
allocated bytes use `st_blocks * 512` with inode deduplication and become a structured
`unavailable` when the platform lacks that field.

`measure_fresh_clone` is the explicit network-capable wrapper. It resolves the configured
remote and branch, obtains the URL without returning or printing it, clones into a
`TemporaryDirectory` with local hardlink optimization disabled, delegates to
`measure_tracker`, and cleans up on success or failure. It raises a concise `FootprintError`
that names only the configured remote/branch on clone failures. Neither function initializes,
reconciles, writes, schedules, or applies a threshold. The JSON contract is
`schemas.TRACKER_FOOTPRINT`; it is intentionally omitted from generated `rebar.types` because
there is no top-level facade return.

## 6. Operation-scoped configuration

`rebar._operation_config`, re-exported from `rebar.config`, provides the supported composition seam implemented by RP-04 under ticket `vibrant-legal-hind` and established by [ADR 0098](adr/0098-operation-scoped-config-and-provider-composition.md). It composes one immutable, serializable, non-secret `OperationSnapshot` for an operation.

The composer delegates these responsibilities:

- Root selection uses `rebar._config_sources.repo_root`.
- Precedence and provenance use `rebar.config.resolve_with_sources` with the order defaults < user < project < env < cli.
- Canonical serialization and fingerprinting use `rebar._store.canonical`.

### Construction

- `compose_operation_snapshot(*, cli_overrides=None, repo_root=None) -> OperationSnapshot` is the central composer. A malformed selected configuration makes `resolve_with_sources` raise `ConfigError` before a behavior-bearing effect.
- `OperationSnapshot.build(*, envelope_version, repo_root, values, sources)` is the validating constructor. Every leaf in `values` must be a JSON primitive or a nested list or dictionary of primitives. A Pydantic `SecretStr`, `SecretBytes`, or runtime object raises `TypeError`.
- `OperationSnapshot.from_document(doc)` rebuilds a snapshot from `canonical_document()`. A mismatched `envelope_version` raises `rebar.config.ConfigError`.

### Serialization and projection

- `.canonical_document() -> dict` returns the hashed document as nested dictionaries.
- `.canonical_bytes() -> bytes` and `.fingerprint() -> str` delegate to `rebar._store.canonical`. The fingerprint is a 64-character SHA-256 value.
- `.project(*sections) -> OperationProjection` returns an immutable view restricted to the named sections. An unknown section raises `KeyError`.

`ENVELOPE_VERSION` is the schema version. The `values` and `sources` mappings use `types.MappingProxyType` at both levels, so a composed snapshot can be shared without defensive copies.

### Runtime bindings and authentication carriers

Runtime capabilities remain outside `OperationSnapshot` and its fingerprint. The implemented bindings are specific to their owners:

- `rebar_reconciler.runtime.ReconcilerRuntime` derives immutable `ReconcilerSettings` from an operation snapshot and builds only the selected Jira backend. `CloudStaticAuth` and `DataCenterStaticAuth` exclude the selected adapter credential from representations, comparisons, and hashes. The provider-specific factory reveals the credential only while constructing the client.
- `rebar.llm.auth.LLMRuntime` carries optional provider-native authentication. `AnthropicAuth` holds either an API key or OAuth token. `BedrockAuth` holds a caller-owned boto3 session. `OpenAIAuth` holds a key or rotating key callable. `ProviderSession` consumes only the carrier for the selected provider while retaining Rebar's client construction, retry, timeout, and teardown policies.
- `rebar.review_bot.startup.StartupBinding` pairs `LLMRuntime` with read-only non-secret startup policy. A running review bot retains this binding until restart.
- `rebar.opcert_service.keyprov.OpcertSigner` holds the process-owned operation certificate key copy and principal. `rebar._opcert_binding.bound_signer` passes it through a context-local binding so concurrent gate workers do not patch process-global signing variables.

ADR 0098 names `GitRuntime` and `BridgeRuntime` as design roles. The implementation does not expose classes with those names. Git operations continue through the store and repository seams. Jira reconciliation uses `ReconcilerRuntime` instead of a general `BridgeRuntime`.

### Configuration ownership enforcement

`rebar._config_sources` owns raw configuration input and `rebar._config_resolvers` owns the approved below-seam resolvers. `scripts/check_config_ownership.py` derives credential names and backend keys, then rejects prohibited ambient reads below the composition, credential, and backend boundaries. The gate runs through `make lint`. `scripts/config_ownership_exceptions.py` contains no legacy exceptions.

### Diagnostic shadow

`emit_shadow_snapshot(*, cli_overrides=None, repo_root=None, surface=...)` remains wired across CLI, MCP, public library, shared command, and direct reconciler entry points. It composes a snapshot and logs only the envelope version, source kinds, and a truncated fingerprint. It never logs values, secrets, or paths, and it does not control execution. The diagnostic emitter catches every `Exception`, including `ConfigError`, so the existing operation continues. Direct calls to `compose_operation_snapshot()` remain fail-fast.

`shadow_enabled()` reads `REBAR_OPERATION_SNAPSHOT_SHADOW`. The switch is enabled by default and remains active until a future behavior-bearing cutover retires the diagnostic path. See [config.md](config.md#rebar_operation_snapshot_shadow) for accepted values. Ticket `opal-daffy-mutt` records the correction that retained this switch after RP-04 closed.
