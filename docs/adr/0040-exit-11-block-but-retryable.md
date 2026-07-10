# ADR 0040 — Exit code 11: block-but-retryable for LLM gate degrades

- **Status:** Accepted (story `authorial-hated-blackbear`, epic jira-reb-687)
- **Date:** 2026-07-10

## Context

The rebar LLM gates (`review-plan`, `review-code`, `verify-completion`, and the
completion gate that fires on `rebar close`) can fail for two categorically
different reasons:

1. **Your work is wrong** — a real BLOCK (plan/code review) or FAIL (completion).
2. **The model provider hiccuped** — a rate-limit (429/529), a connection blip, a
   transient 5xx, an overload. Nothing about the plan/code is wrong; the call
   should simply be retried after a backoff.

Before this decision both collapsed onto the existing exit codes: a plan-review
degrade exited `2` (INDETERMINATE) and a completion-gate outage exited `1`
(fail-closed) — indistinguishable, at the process boundary, from "fix your work".
A driving agent (or CI wrapper) therefore could not auto-retry only the genuinely
transient case; it either retried real failures (wasteful, wrong) or treated
transient blips as hard stops (flaky, brittle).

The LLM-failure classifier (`rebar.llm.failure.classify_llm_failure`, story
`civilized-immediate-mamba`) already computes a closed `resolution_class` and a
pre-computed `retryable` bool per failure. This ADR surfaces that bit at the
process boundary.

## Decision

Add exit code **11 — "transient — retry"** for the **systemic, retryable** subset
of LLM-gate degrades, i.e. the classifier's `WAIT_AND_RETRY` / `RETRY_NOW`
disposition (`retryable == True`).

- **Shape A** (plan-review / code-review return a degraded verdict dict): the CLI
  reads `coverage.retryable`; true → `11`, else the gate's existing exit
  (INDETERMINATE → `2`, BLOCK → `1`, PASS → `0`).
- **Shape B** (completion raises `LLMUnavailableError`): the CLI / close gate reads
  the raised error's `.outcome.retryable`; true → `11`, else `1` (fail-closed).
- **Exit `2` is NOT redefined.** `review-plan` already exits `2` for INDETERMINATE
  (pinned by the RED baseline of story `gnomish-nosophobic-arawana`); that use is
  unchanged. Only the retryable subset peels off to `11`. A non-retryable
  INDETERMINATE still exits `2`.

The disposition is persisted on the verdict (`coverage.resolution_class` /
`coverage.retryable`, additive optional schema fields) and the class-specific
human message is printed to stderr so the driver knows what to do.

## Consumers + additive-safety

Exit 11 is **additive** — every existing consumer keeps working unchanged:

| Consumer | How it reads the gate | Effect of exit 11 |
|---|---|---|
| Driving agent / human (primary) | `docs/exit-codes.md` | Treats 11 as "retry after backoff"; a not-yet-updated driver falls back to any-non-zero-is-failure — still safe |
| CI (`.github/workflows/gerrit-verify.yaml`, `test.yml`) | runs `make` targets; **never invokes the gate CLIs** | 11 is invisible to the `Verified` vote; plain any-non-zero-is-failure |
| Gerrit review-bot (`src/rebar/review_bot/adapter.py`) | votes off the code-review **verdict dict** (PASS/BLOCK/INDETERMINATE), tolerates open `coverage` keys | unaffected by exit codes; the new `coverage` keys are ignored |

No consumer special-cases exit `2` such that peeling `11` off it changes behavior.
Deploy note: because any-non-zero-is-failure consumers already fail correctly, the
gap window before a driver learns 11 degrades to "generic failure", never a misfire.

## Rollback

Revert the CLI mapping; the retryable subset falls back to the existing
INDETERMINATE exit (`2` for review-plan, `1` for the close gate). The verdicts'
optional `coverage.resolution_class` / `retryable` / `diagnostic` fields are
ignored by readers (the `coverage` object is `additionalProperties: true`), so a
verdict written in the forward window and read after rollback simply drops them —
**no scrub, no migration.**
