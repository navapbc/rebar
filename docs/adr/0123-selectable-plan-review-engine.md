# ADR 0123 — Selectable plan-review engine: the 4-pass pipeline or a single read-only agent

**Status:** Accepted
**Date:** 2026-11-09

Epic `pompous-vitriolic-macaw` (`9794-4336-5ed6-4561`). The engine switch, the agentic runtime
and tooling, and the agentic budget and timeout were confirmed by the operator on 2026-11-09
as written below; the severity-to-verdict mapping was decided earlier the same day.

## Context

`rebar review-plan` runs one engine: a deterministic Layer-1 floor (`plan_review/floors.py`,
`det_*.py`) and claimability fast-fail (`plan_review/claimability.py`), then four LLM passes
(find, verify, decide, coach) driven by the workflow `llm/workflow/gates/plan-review.yaml`. Its
verdict is BLOCK, PASS or INDETERMINATE with `blocking` and `advisory` finding lists
(`orchestrator.finalize_verdict`, `plan_review/sidecar.py`); a non-blocking PASS signs a
plan-review attestation the claim gate accepts (`plan_review/attest.py`, ADR 0049).

The epic adds a second engine — one Opus 5.5 agent with read-only access to the reviewed tree and
a new prompt — so the two can be run side by side and compared, and upgrades the configured
models to the 5.5 generation. The decisions below are the ones later children build on and that
signed attestations will carry, so they are recorded before those children are planned against
them.

## Decision

### 1. Engine switch (decided 2026-11-09)

A config key `[verify] plan_review_engine` selects the engine: `"pipeline"` (today's 4-pass
engine) or `"agentic"`. It sits beside the other plan-review keys
(`require_plan_review_for_claim`, `require_plan_review_for_close`) and is declared in
`_config_schema.py` and validated in `_config_sections.py`, so an unknown value fails at load
time rather than falling back silently.

- **Default:** `"pipeline"`. No project changes behaviour without opting in.
- **Precedence:** an explicit `engine` argument to `review_plan` (which the CLI `--engine` flag,
  the MCP tools and the comparison harness pass) over the config key over the built-in default.
  There is no environment variable: a per-call argument already covers one-off runs, and an
  environment layer would be a second, invisible source.
- **Lifetime:** the key is permanent and both engines are supported options (amended
  2026-11-12; see the amendment below). Its `mechanism-ok: config_key` marker names the standing
  admission ticket `cankered-cloistered-bittern` (`7447-47d8-051d-42ab`), which stays open while
  the key exists.

Selection happens inside the plan-review workflow, after the `precheck` step has run the
deterministic floor: the existing `review` step gains the condition `engine == 'pipeline'` and a
new `agentic_review` step runs on `engine == 'agentic'`. Claimability fast-fails earlier, in
`_run_plan_review`. So a floor block or a not-claimable ticket returns before any engine is
consulted, with zero LLM calls and an identical result under either value.

### 2. Agentic engine design (decided 2026-11-09)

- **Runtime.** Chosen over the Claude Agent SDK, which would add a subprocess dependency and
  lose the Bedrock-first fallback chain and `project.portability`. One call on rebar's existing runner (`RunRequest`, `execution_mode="agentic"`,
  `mode="structured"`) on the `frontier` model class. The Bedrock-first provider chain and its
  Anthropic fallback, retries, tracing and usage logging apply unchanged. Nothing depends on a CI
  provider or a subscription service, which keeps the engine inside `project.portability`.
- **Tools.** Exactly the runner's existing read-only surface: `read_file`, `list_directory` and
  `search_files` (`pai_tools.filesystem_tools`), the grounding tools, and `show_ticket` with
  `comment_ticket` withheld.
- **Read-only enforcement**, three independent layers: the tool surface contains no write, edit
  or execute tool (pinned by a test on the tool-name set); `fs_tools._safe_path` refuses
  traversal outside the read root; and the read root is the gate-chosen attested snapshot that
  `assert_gated` requires before any tool-using agent runs.
- **Budget and timeout.** `tool_step_limit=60` (after 60 tool steps the tools answer with a
  steering notice and the agent must produce its answer), `iteration_limit=80`, the model's
  maximum output tokens, and the per-request timeout `cfg.timeout_s`. All are existing
  `RunRequest` fields or config; the engine adds no knob. Effort is not an engine setting: the
  agent inherits the per-model effort table the model upgrade introduces (Opus 5.5 at `high`).
- **Prompt.** Kept in rebar's packaged prompt registry
  (`llm/reviewers/plan_review_agentic.md`). Its starting text is the operator's raw review
  prompt, copied and adapted: the plan material is the context rebar already assembles for the
  pipeline, the tree is "the repository your file tools read", the output is the structured
  result rather than a file, and the review criteria are appended from the registry
  `rebar explain` renders. rebar takes no dependency on the repository the text came from.

### 3. Severity-to-verdict mapping (decided 2026-11-09 by the operator)

The adapter sets each agentic finding's `decision`, the key `finalize_verdict`'s partition reads:

| severity | decision | effect |
|---|---|---|
| `blocker` | `block` | the plan is BLOCKed |
| `major`, `minor`, `trivial` | `advisory` | surfaced; the plan can PASS |

Severity alone decides; a criterion's registry posture does not. Zero blockers is PASS. A runner
error or output that fails schema validation is INDETERMINATE, through the same
`resolution_class` path as a failed pipeline pass, so it is never signed. The agentic engine has
no verify or decide pass, so its findings carry no priority or decision margin, as DET findings
carry none.

An agentic run that read no code, meaning it made no `read_file`, `search_files` or
`list_directory` call, also resolves INDETERMINATE and is never signed (amended 2026-11-13, bug
`bridal-gifted-vampirebat`). The agent call's run shape (`_usage.distinct_fetches`) lists those
calls; when it is empty, or a reported `_usage` lacks it, the review degrades through the same
path as a failed run, with `resolution_class` FIX_AGENT_DESIGN.

### 4. Finding schema mapping

| agentic field | pipeline finding field |
|---|---|
| `title` + `description` | `finding` |
| `evidence` | `evidence` |
| `suggested_fix` | `suggested_fix` |
| `criteria` (registry ids, possibly empty) | `criteria`, filtered to ids in `registry.by_id()` |
| `severity` | `severity` (kept) and `decision` (section 3) |
| — | `id`, minted by `orchestrator.mint_finding_id` |

With `decision` set and ids minted, `finalize_verdict`, the sidecar writer and every sidecar
reader consume agentic findings unchanged, and both engines' verdicts and sidecars validate
against the same `plan_review_verdict` and `review_result` schemas.

### 5. Engine provenance on the attestation

The signed plan-review attestation gains an `engine` field (`"pipeline"` or `"agentic"`) inside
the signed payload, so it cannot be changed without breaking the signature, and it survives
sidecar pruning. A missing field reads as `"pipeline"`: every attestation signed before this
change keeps validating for claim, close, `review-plan --status` and `sign-review`, and no
migration is written. Re-signing carries the field forward.

### 6. Invariants both engines keep

- The Layer-1 floor and claimability run first, unchanged; when either stops the review, no LLM
  call is made and the result is identical under both engines.
- Both engines produce the same verdict contract and are reachable from the same surfaces: CLI,
  library, and MCP synchronous and async start-and-poll tools, with the MCP de-dup key including
  the engine and gate admission (`GATE_CONGESTED`) unchanged.
- A non-blocking PASS earns the same certification under both.

### 7. Model upgrade

Opus 4.8, Sonnet 4.6 and Haiku 4.5 move to Opus 5.5, Sonnet 5.5 and Haiku 5.5 on the current
engine first (`beaten-natural-grouse`). The 5.5 request surface differs: forced `tool_choice`
returns 400 on Opus and Sonnet, thinking cannot be disabled on Opus, sampling parameters are
rejected, prefill is rejected, and Opus and Haiku default `effort` to `medium`. rebar sends no
forced tool, thinking toggle or prefill, so the upgrade withdraws `temperature` for every 5.5 id
and sets `effort` explicitly (`high` for Opus and Sonnet, matching the API default the 4.x models
ran at; `medium` for Haiku). The call-by-call audit and the before/after evals are recorded on
that ticket.

## Consequences

- Both engines are maintained as supported options, and the key selects between them; the
  comparison harness and production runs inform which engine a project chooses.
- An attestation now says which engine produced it, so a project can audit or restrict engines
  later without re-signing history.
- The agentic engine's findings have no verify pass behind them, so its false-positive rate is a
  property of the prompt and model alone; the comparison harness measures it against the
  pipeline before any project depends on it.

## Amendment (2026-11-12)

The operator decided that `[verify] plan_review_engine` is a permanent setting and that both
engines stay supported; no engine is retired, and the task planned to retire one is obsolete.
The key's `mechanism-ok: config_key` marker now names the standing admission ticket
`cankered-cloistered-bittern` (`7447-47d8-051d-42ab`), which stays open while the key exists.
The default (`"pipeline"`), the precedence and the absence of an environment variable are
unchanged.
