# Design note: the plan-review batch-runner seam (B1 ↔ B2)

**Status:** Draft for review (epic `peak-fir-hilt`; stories B1 `knotty-hat-chase`, B2 `unfed-posse-urn`).
**Decision needed before implementing B1** — its interface is shared with B2.

## Problem

Epic A added a generic `BatchRunner` seam (`rebar.llm.workflow.runners`):

```python
class BatchRunner:
    def run(self, req: BatchRunRequest, agent_runner: AgentStepRunner) -> BatchRunResult
```

with a thin, generic `BatchRunRequest` (built by the interpreter's `_run_batch`):

```
finder, criteria=({prompt, when?, with?}, …), usd_budget, model_ladder,
workflow(=the doc), target_ticket, repo_root, run_id, step_id
```

B1 must supply a **production** `BatchRunner` that drives plan-review's existing adaptive
batching (`sizing.py`: `pass1_with_ladder` / `shed_to_budget` / checkpointing). But that
machinery needs far more than the seam carries — it needs a `PlanContext` (ticket state +
children + centrality + plan text + largest-window), the per-criterion **registry
descriptors** (rubric + tier + container/overlay flags), the single-turn/agent/container
tier split, facet chunking, and a `rebar.llm.Runner` (not the workflow's `AgentStepRunner`).
Today the glue that assembles all that lives **inside `orchestrator.py`** (`_run_passes`,
lines 552–641; PlanContext build, lines 78–102), not as a reusable function.

So the two open questions: **(Q1)** does `BatchRunRequest` get extended to carry plan-review
context, or does the runner reconstruct it from `target_ticket`? **(Q2)** how do we keep the
runner a *thin adapter* (B1 AC3: "no duplicated algorithm") when the loop lives in the
orchestrator?

## Recommended design

### D1 — Keep `BatchRunRequest` GENERIC; the runner reconstructs `PlanContext`.

Do **not** couple epic A's seam to plan-review. The `ProductionBatchRunner` reconstructs
`PlanContext` from `req.target_ticket` + `req.repo_root` using the same builder the
orchestrator uses (see D2). Rationale:

- Keeps the seam reusable (epic A's `DefaultBatchRunner` and any future batch use stay valid).
- Keeps B2's workflow authoring declarative and simple (the `batch` step lists only
  finder + criteria prompt-ids + overlays + ladder; no context plumbing).
- **Replay-safe:** epic A's batch invariant is that the runner journals an *opaque* plan and
  replay reads the committed marker (the runner is **not** re-run on replay, per
  `interpreter._run_batch`). So reconstruction happens only on the first run — there is no
  replay-determinism cost to re-deriving context.

Cost: the runner re-fetches ticket state + re-derives centrality that B2's DET-precheck step
also computes. These are cheap local reads; not worth coupling the seam to avoid.

### D2 — Extract the Pass-1 loop from `orchestrator.py` into a shared function; both call it.

This is the key refactor that satisfies AC3 (thin adapter, no duplication). Extract two units
from `orchestrator.py` into a reusable home (proposed: `sizing.py`, already the "fit the review
into a budget/window" cluster, or a sibling `pass1.py`):

- `build_plan_context(ticket_id, *, repo_root) -> PlanContext` — the inline assembly at
  `orchestrator.py:78–102` (show_ticket + children + `centrality`).
- `run_pass1(ctx, cfg, runner, single, agent, coverage) -> findings` — the body of
  `_run_passes` (`orchestrator.py:552–641`): container split → `chunk_by_facet` → `shed_to_budget`
  → per-chunk `load_checkpoint`/`pass1_with_ladder`/`save_checkpoint` → `_run_container`.

`orchestrator.py` is refactored to call these (behaviour-preserving; the existing plan-review
test suite + the live claim gate cover it). The `ProductionBatchRunner` then calls the *same*
two functions. **No algorithm is duplicated** — the runner is genuinely thin glue.

> This refactor touches the **live plan-review claim gate**. It must be behaviour-preserving
> and land green; it is the one piece of B1 with real blast radius (B3 was purely additive).

### D3 — The `ProductionBatchRunner` holds its own `rebar.llm.Runner`; the seam's `agent_runner` is intentionally unused.

Plan-review's finder (`passes.pass1_chunk`) is a specific `rebar.llm.Runner` operation, not a
generic workflow agent step, so the seam's `AgentStepRunner` is the wrong abstraction to drive
it. The runner constructs `get_runner(LLMConfig.from_env(repo_root))`, **injectable** via a
constructor param for offline/parity tests (B4 passes a fake `rebar.llm.Runner`). The `run()`
signature still accepts `agent_runner` (seam contract) but documents that the production runner
does not use it.

### D4 — The runner computes the budget cap from `PlanContext` unless `usd_budget` overrides.

`plan_budget_cap(ctx)` is centrality-scaled — a property of the ticket, not the workflow. Let the
runner compute it (keeping budget logic in `sizing.py`), and treat a workflow-supplied
`batch.usd_budget` as an explicit override. Keeps B2's workflow declarative.

### D5 — Criterion resolution: prompt-id IS the registry id.

Epic A established that the criteria library mirrors the prompt library, so each `batch.criteria`
entry's `prompt` is also its registry criterion id. The runner resolves descriptors via
`registry.by_id()` and splits single/agent/container from the descriptor's tier/flags — exactly
as the orchestrator does.

## What B2's `batch` step looks like under this design

```yaml
- id: finders
  needs: [precheck, triggers]
  batch:
    prompt: plan-review-finder          # the finder system prompt id (req.finder)
    criteria:                           # registry-assembled at render time / via a `uses` op
      - { prompt: F1 }
      - { prompt: E4 }
      - { prompt: security, when: ${{ steps.triggers.outputs.security }} }   # overlay
      # …
    model_ladder: [claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-8]
    # usd_budget omitted → runner computes plan_budget_cap(ctx); set to override
```

The interpreter resolves each `when` (inclusion) before the runner; the runner receives only the
included criteria, reconstructs context, and drives the extracted `run_pass1`. The journaled
`coverage` dict (budget/shed/ladder-events/checkpoints) becomes the opaque `batch_plan`.

## Parity (B4) under this design

Both the bespoke `orchestrator._run_passes` path and the workflow `ProductionBatchRunner` call
the **same** extracted `run_pass1`, so the planned trace (criterion prompt-ids + intended model +
call-mode + the deterministic ladder/shed steps) is structurally identical — parity is by
construction, asserted offline with a fake `rebar.llm.Runner`.

## Open questions for review

1. **D1**: confirm we keep `BatchRunRequest` generic (runner reconstructs) rather than extending
   it with a plan-review context blob. (Recommended: keep generic.)
2. **D2 home**: extract `build_plan_context` + `run_pass1` into `sizing.py`, or a new
   `plan_review/pass1.py`? (`sizing.py` is the existing cluster but would grow; a sibling keeps
   files small per the module-size policy.)
3. **D4**: runner-computed budget vs. workflow-authored `usd_budget` as the default. (Recommended:
   runner-computed, workflow overrides.)
4. Confirm the orchestrator refactor (D2) is acceptable to land on its own behaviour-preserving
   PR *before* the runner, so the risky live-gate change is isolated and reviewed independently
   of the new runner + B2 wiring.

---

# Post-brainstorm decisions (joe-brainstorm, grounded in prior art + original-intent logs + adversarial critique)

**Chosen: Option A — the batch is an engine-opaque, sealed adaptive unit** (two-level
observability), confirmed against the original `woody-meat-seize`/epic-A intent ("adaptive
batching is a recorded decision-tree, not replayable IR") and the industry mainline
(claim-check + decide-once-journal-the-result; Temporal/SFN/Airflow/Argo).

- **D1–D2, D4–D5 stand.** D2 extraction home: a new sibling **`plan_review/pass1.py`** (keeps
  `sizing.py` cohesive + under the module-size cap), holding `build_plan_context` + the Pass-1
  loop; `orchestrator.py` is refactored to call it. **Land that extraction as its own
  behaviour-preserving PR first** (it touches the live claim gate), then the runner.
- **D3 reframed (resolves the BLOCKER the critique found).** The planned-trace capturer
  `PlannedTraceRunner` is an `AgentStepRunner`, but plan-review's Pass-1 drives a
  `rebar.llm.Runner` directly — in BOTH the bespoke and workflow paths. So the production runner
  **owns an injectable `rebar.llm.Runner`**, and the planned trace is captured **at that seam**
  (a tracing/Fake `rebar.llm.Runner`), which both paths share via the extracted `pass1.py`. The
  engine's dry-run path injects the Fake `rebar.llm.Runner` into the production runner (parallel to
  `FakeAgentRunner`). Consequence (consistent with "opaque batch"): the batch's internal finder
  calls are not visible to the engine's generic `PlannedTraceRunner`/dry-run — they're traced one
  level down.

## Critique findings — resolutions
- **Resume / context-divergence / checkpoint coherence:** the live orchestrator ALREADY
  reconstructs `PlanContext` + checkpoints by a `material_fingerprint` that excludes deps. A
  faithful migration *preserves* this; it is not a new risk. Capturing a content-hash in the plan
  (prior art's reproducible-resume fix) is a noted FUTURE enhancement, out of scope for B1.
- **Concurrency:** the Pass-1 thread pool lives INSIDE the sealed runner — exactly where the
  original design put adaptive work. The interpreter stays single-threaded; the Burr invariant
  holds. Constraint: do NOT `map` over batch steps (nested pools).
- **`target_ticket` required:** lint/guard that a production-runner batch step runs against a
  target ticket (the claim gate always has one).
- **Parity scope (the critique's MAJOR 4):** "same function" only proves Pass-1. The full planned
  trace is assembled from **two offline-fakeable capture points** — the batch's `rebar.llm.Runner`
  (finders) and the engine's `AgentStepRunner` (the Pass-2 verify step) — and parity (B4) is
  asserted at the BOUNDARIES (criteria inclusion/order, intended model, call-mode, det steps),
  pre-escalation, not as "I wired the same function."

## Editor scope (now its own stories)
In-editor editing covers **criteria AND prompts** (not just criteria), selection-driven not
free-text. Split into:
- **B-DM (`dark-tempo-rug`)** — the underlying DATA MODEL: a library enumerate + create/update +
  index-regen API for prompts + criteria (the backend the editor sits on). **Do this first** (per
  the epic sequencing); it is independent of the runner.
- **B-UX (`lanky-turret-worry`)** — the visual editor: criterion/prompt **dropdowns** from the
  library; overlay-`when` dropdown from the workflow's `overlay_triggers` outputs; inline
  create-new-trigger (step config); **full in-editor authoring** of new criteria AND prompts (write
  the `.md` via B-DM + regen the index, keeping the drift gate green). Blocked by B-DM.

## Sequencing
Underlying data model FIRST: **B1** (the runner — extraction PR, then the thin runner) and **B-DM**
(library write/enumerate model) — both data-model, both before **B-UX**. Then B2 → B4 → B5 → B6.
