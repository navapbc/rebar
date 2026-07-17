# The workflow engine — intended use

rebar's **workflow engine** is a thin, synchronous interpreter over a git-native YAML
IR (`.rebar/workflows/*.yaml`) whose run-state lives on rebar's event log. It is the
single substrate for rebar's **LLM gates** — the plan-review gate (open→in_progress)
and the completion-verification gate (→closed) are both workflows on this engine.

This doc is the canonical anchor for *when and how* to use the engine. It is written
for **two audiences**, kept deliberately separate:

- **Using rebar (client / operator)** — you mostly *run* workflows and *hit* gates;
  you rarely author them. Read [Part 1](#part-1--using-workflows-and-gates).
- **Developing rebar (workflow author)** — you author/edit workflows, prompts,
  review criteria, and their evals. Read [Part 2](#part-2--authoring-workflows-rebar-dev)
  onward.

It links to, rather than duplicates, the deeper references:
[llm-framework.md](llm-framework.md) (the agent-operations framework),
[workflow-authoring-v2.md](workflow-authoring-v2.md) (the contract-bearing prompt/step
model), [workflow-editor.md](workflow-editor.md) (the visual editor),
[plan-review-gate.md](plan-review-gate.md) (the gate itself), and
[output-schemas.md](output-schemas.md) (the result contracts).

---

## Part 1 — Using workflows and gates

You interact with the engine mostly through the **two gates**, which run automatically
during the normal ticket lifecycle (see the project guide / `AGENTS.md`):

- **Plan-review gate** — a ticket needs a passing plan review *before it can be
  claimed* (`open → in_progress`). Run it with `rebar review-plan <id>` (MCP
  `review_plan`). A BLOCK verdict must be remediated and re-run; even a PASS asks you
  to triage advisory findings.
- **Completion-verification gate** — closing a work ticket runs the completion
  verifier; a FAIL (or an unavailable LLM) blocks the close, and a PASS that is also
  `certifiable` is signed onto the ticket as the attestation that its criteria are met
  (a PASS with `certifiable: false` — a closed-but-uncertified descendant — still closes
  but is left unsigned).

You normally **do not author workflows** to use rebar. When you want to inspect or run
one directly:

| Action | CLI | MCP |
|--------|-----|-----|
| Run a workflow against a ticket | `rebar workflow run <file> …` | `run_workflow` |
| Read a run's status / result | `rebar workflow status <run_id>` / `result <run_id>` | `get_workflow_status` / `get_workflow_result` |
| Render a workflow as a diagram | `rebar workflow show <file>` | `render_workflow` |

Runs are replayable from the event log, so status/result are derived, not cached.

---

## Part 2 — Authoring workflows (rebar dev)

### When to author a workflow vs a bespoke op

Author a **workflow** when the task is a *declarative pipeline* of steps — especially
when it mixes deterministic work with LLM calls, benefits from being visually
reviewable/editable, or is a **gate** whose shape should be auditable and replayable.
Reach for a **bespoke Python op** (in `rebar.llm`) only for logic that is not a
step pipeline (tight library calls, one-shot helpers). The engine is the default
substrate for LLM functionality; the plan-review, completion, and code-review gates all
moved onto it precisely so the review logic lives in one place.

### The YAML DSL (v3)

A workflow is `schema_version`, `name`/`description`, typed `inputs`, and a list of
`steps`. Each step has an `id`, optional `needs` (DAG edges), and one of a few kinds:

- **`uses: <op>`** — a *scripted* (deterministic) step (e.g. `overlay_triggers`,
  `gate`, `plan_review_precheck`). No model call.
- **`prompt: <prompt-id>`** — an *agentic* step: runs the named reviewer prompt
  through the agent runtime, emitting a structured result (`mode: findings` etc.).
- **`batch: {prompt, criteria[], usd_budget, model_ladder}`** — a budgeted fan-out
  that runs a finder over an authored list of `criteria` (each a prompt-library entry),
  some conditionally included via `when:`.
- **`branch: {when, then[], else[]}`** — a first-class conditional whose chosen arm is
  journaled at run-start for replay safety (an `if:`-skipped step's outputs can't be
  referenced).

Values flow via `${{ inputs.x }}` / `${{ steps.<id>.outputs.<k> }}`. The canonical
skeleton lives at `src/rebar/llm/workflow/examples/review_skeleton.yaml`; the three real
gates are `src/rebar/llm/workflow/gates/{plan-review,completion-verification,code-review}.yaml`.

```yaml
schema_version: "3"
name: review_skeleton
inputs:
  plan: { type: string, required: true }
steps:
  - id: triggers            # scripted: precompute overlay triggers
    uses: overlay_triggers
    with: { text: ${{ inputs.plan }}, keyword_triggers: { security: [secret, token] } }
  - id: finders             # batch: budgeted Pass-1 finder over included criteria
    needs: [triggers]
    batch:
      prompt: code-quality
      criteria:
        - { prompt: ticket-quality }
        - { prompt: security, when: ${{ steps.triggers.outputs.security }} }
      usd_budget: 2.0
      model_ladder: [claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-8]
  - id: verify              # agentic: one aggregate Pass-2 verify
    prompt: completion-verifier
    needs: [finders]
    with: { findings: ${{ steps.finders.outputs.findings }} }
  - id: decide              # scripted: deterministic Pass-3 gate (no tokens)
    uses: gate
    needs: [verify]
    with: { findings: ${{ steps.verify.outputs.findings }}, policy: default }
```

### Author / validate / render / run / edit

```sh
rebar workflow new <name>        # scaffold a valid skeleton
rebar workflow validate <file>   # lint against the v3 schema + ref checks
rebar workflow show <file>       # render a Mermaid graph
rebar workflow run <file> …      # execute (sync); replay status/result by run_id
rebar workflow edit <file>       # open the ephemeral bpmn-js visual editor
```

For the contract-bearing prompt/step model (prompt front-matter, the prompt↔op↔registry
conventions, `execution_mode`, the derived prompt index + CI drift gate) see
[workflow-authoring-v2.md](workflow-authoring-v2.md); for the visual editor see
[workflow-editor.md](workflow-editor.md).

### The three-pass (4-pass) review gating pattern

rebar's review gates follow a fixed shape so LLM non-determinism never creates false
confidence:

1. **Pass-1 — evidence/finders** *(agentic or single-turn)*: produce candidate
   findings, each grounded in evidence. Fanned out over criteria by a budgeted `batch`.
2. **Pass-2 — independent binary-verify**: one aggregate verifier independently grades
   each finding's validity (discrimination, anti-sycophancy) — it never just trusts
   Pass-1.
3. **Pass-3 — deterministic gate**: a *scripted* step turns verified findings into a
   verdict by fixed policy (no model call), so the gate decision is reproducible.
4. **Pass-4 — affirmative coach** *(gates only)*: renders next-move coaching from
   locked templates.

The plan-review gate is the worked example — see [plan-review-gate.md](plan-review-gate.md).

### The agent-vs-single-turn bright line

A reviewer/criterion is **agent-tier** (a tool-using loop) *if and only if* reaching
its verdict needs to **probe the live environment** — grep/read the repo, run a
CLI/API — i.e. evidence not already in artifacts you can feed it. Two ways that
happens: **assertion-grounding** (verifying a claim about the codebase) and
**implication-grounding** (the verdict depends on what the code actually does — "could
the plan look fine but the code be broken, or vice-versa?"). Otherwise, feed the
artifacts you already hold (the ticket, its graph, a linked session log, the diff) to a
**single-turn (or 2-step) frontier call** — that is *not* an agent. Corollaries:
"needs a frontier model + large context" ≠ "needs an agent" (use a bigger model + more
context); code-grounding for assertions is centralized in the grounding criteria, not
re-done by every criterion; oversized sources use a *deterministic* pre-retrieval step,
never an autonomous loop. The agent tier is ~85× the cost and non-deterministic, so
default to single-turn and pay for the loop only when it must probe the environment.

---

## Part 3 — The prompt library + eval seam

### Prompts as files

Reviewer prompts are **git-native files** in `src/rebar/llm/reviewers/*.md` (front-matter
+ body), resolved by `get_prompt` with `.rebar/prompts/` overrides and an `index.json`
drift gate. They are versioned, reviewable, and visually editable like any workflow
asset — never transient experiment artifacts.

### Evals: the two-tier model

Prompt edits are regression-gated by evals, split the way the OSS community converges
on — a cheap deterministic gate on every PR, expensive LLM scoring off the PR path:

- **Offline discipline (every PR/push, zero cost, no model)** — the `eval-discipline`
  CI job validates every packaged spec (`src/rebar/llm/eval_specs/<id>.eval.yaml`):
  spec shape, registered deterministic scorers, a balanced dataset + gold set (STRICT
  for dataset-bearing specs), the pinned cross-family judge, `at_least(k)` over epochs,
  coverage, and the scorer/solver/`run_eval` unit tests. This is the only **required**
  check. Run locally with `rebar prompt eval <id>` (validates the spec offline).
- **Live scoring (manual + weekly, non-blocking)** — the `eval-live` CI job runs
  `run_eval` over the dataset-bearing specs: each case runs the reviewer's *real* op
  (via the eval solver, against a disposable fixture store), the deterministic scorers
  gate (`at_least(k)` over epochs), and JUnit is emitted. It is deliberately **off the
  PR critical path** (`workflow_dispatch` + a weekly `schedule`), **non-blocking**
  (`continue-on-error`), **concurrency-capped**, and **fork-safe** (same-repo only, so
  forks/Dependabot never spend tokens). Provider spend caps belong at the
  Anthropic/OpenAI dashboard — CI has no native dollar cap.

A deterministic scorer **gates**; an `llm-judge` scorer only **reports** (it never
gates by itself) and is admitted only when its Cohen's-κ alignment to a frozen
human-gold set clears threshold. To author a spec, copy an existing one and see
`src/rebar/llm/eval_specs/README.plan-review.md`; scorer names must be registered in
`src/rebar/llm/evals/eval_scorers.py`.

### Prompt-authoring guidance for review criteria (research-grounded)

Use the techniques that actually raise reliability; avoid the cargo-cult ones:

- **DO** write atomic yes/no checklist items; **reason first, then** emit the
  structured verdict (a leading `analysis` field — forcing the JSON/tool call first
  measurably degrades accuracy); use **independent verification** (Pass-2) and
  **calibrated abstention** ("insufficient evidence"); anchor "expertise" to a **named
  standard** (OWASP/WCAG/strong_migrations), not a persona.
- **DO** frame anti-false-positive rules **affirmatively** ("flag only when it would
  cause rework"), pink-elephant-safe — negation makes the prohibited behavior salient;
  add a **verbosity-bias guard** ("judge substance, not length").
- **AVOID** role/company personas ("Principal Engineer at …") — the evidence is they do
  **not** improve accuracy on evaluation tasks and detailed personas often hurt; avoid
  emotional prompts ("take a deep breath"), and avoid multi-agent debate / heavy
  self-refine (weak, ~3× cost for marginal gain).

---

## See also

- [llm-framework.md](llm-framework.md) — the `rebar.llm` agent-operations framework.
- [workflow-authoring-v2.md](workflow-authoring-v2.md) — contract-bearing prompts & steps.
- [workflow-editor.md](workflow-editor.md) — the bpmn-js visual editor.
- [plan-review-gate.md](plan-review-gate.md) — the plan-review gate.
- [output-schemas.md](output-schemas.md) — the structured result contracts.
- `src/rebar/llm/eval_specs/README.plan-review.md` — the standing eval suite.
