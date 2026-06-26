# ADR 0003: Express the LLM gates as engine workflows (workflow-as-gate)

- **Status:** Accepted
- **Context:** Epic *Migrate the LLM gates onto the workflow engine* (`peak-fir-hilt`);
  decomposition story B3 *completion-verifier as an engine workflow* (`core-farce-seed`).

## Context

rebar's two LLM gates run OFF the unified workflow engine: the plan-review gate is a bespoke
742-LOC orchestrator (`plan_review/orchestrator.py`, a `ThreadPoolExecutor` fan-out) and the
completion verifier is a direct single-agent call (`completion.py`). Epic `peak-fir-hilt`
converges both onto the engine (declarative IR + thin interpreter + provider-agnostic runtime),
validated by offline planned-trace parity. B3 migrates the *simpler* gate first — the walking
skeleton that establishes the reusable pattern B2 then follows for plan-review.

The non-obvious constraint surfaced while implementing B3: `completion.verify_completion`'s
child-closure precheck is a **short-circuit**, not a linear stage. When a parent has an
unclosed/uncertified child it returns a deterministic FAIL verdict and **skips the LLM
entirely** (`completion.py:223-225`, no billable call). A naive linear `uses → prompt → uses`
workflow would still run the `prompt` after a precheck failure — changing both behaviour and
cost. And referencing an `if:`-**skipped** step's outputs *raises* (`executor._resolve_one`:
"step did not produce output"), so a bare `if:` on the prompt plus a downstream step that reads
the (possibly-skipped) verify output cannot work.

## Decision

Express an LLM gate as an engine workflow with this shape (the **workflow-as-gate** pattern):

1. A deterministic **precheck** `uses` op wraps the gate's existing deterministic helpers
   (no logic change) and emits a positive boolean (`run_verify`) plus — on failure — the
   already-reconciled deterministic verdict.
2. A **`branch` on `run_verify`** models the short-circuit. The branch is preferred over a bare
   `if:` because each arm references only steps that run in it (no skipped-output references):
   - **then** (precheck passed): the agentic **`prompt`** verify step (ONE call; the engine
     auto-injects `ticket_id`/`ticket_context`/`repo_path`) → a deterministic **reconcile**
     `uses` op applying the SAME normalize → resolve-citations → reconcile → validate pipeline
     as the bespoke tail, so the result is behaviourally equivalent (structural parity).
   - **else** (precheck failed): a **passthrough** `uses` op emits the deterministic verdict
     verbatim — the LLM is never reached (behaviour + cost preserved).
3. The workflow **does not sign.** The deterministic helpers produce/return a verdict; signing
   lives in the close-gate path, not in `completion.py`. Cutover + signing is the epic's later
   cutover story (B5).

The gate ops live in `src/rebar/llm/workflow/gate_ops.py` as thin adapters over
`rebar.llm.completion`, so equivalence with the bespoke call is structural (shared code), and a
golden test pins it. The deterministic verdict for a precheck failure is read from the executed
branch arm's recorded output (the branch step itself records `{taken}`); B5 adds the gate-side
helper that extracts the terminal arm's `completion_verdict` from a run result.

## Consequences

- **Reused by B2.** plan-review (find → verify → decide → coach) follows the same pattern — a
  deterministic precheck (DET P1–P9), overlay-resolved criteria, a `batch` finder stage, an
  aggregate verify, then deterministic decide/coach — so this ADR is the shared contract.
- **Short-circuits stay short-circuits.** Any deterministic gate guard that must avoid a billable
  call is modelled as a `branch`, never a bare `if:` feeding a later reader of the skipped step.
- **Parity is testable offline.** With a canned `AgentStepRunner` (no tokens) and monkeypatched
  reads, the whole gate shape — including the no-LLM-on-precheck-failure guarantee — is exercised
  cheaply in CI; planned-trace parity against the bespoke path is B4.
