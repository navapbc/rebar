# ADR 0103 — Reconciler logical-operation coordination (single retry budget + observe-before-replay)

- **Status:** Proposed (provisional — ticket `7bc2-5203-d5f4-4a4a`, epic RP-03)
- **Date:** 2026-08-24

## Context

The reconciler's outbound mutate path issues **non-idempotent** ticket writes to Jira
Data Center: a create/update/transition has no client-supplied idempotency key, so a
retried write after an *ambiguous* commit (the HTTP call timed out or the connection
dropped after the server may already have applied it) risks a duplicate side effect.
Retry logic historically lived in several places at once — the ACLI subprocess loop
(ADR 0084), the LLM transport layer (ADR 0087), and ad-hoc call-site loops — with no
single owner of the *physical-invocation* budget for a logical mutate operation, and no
provider-neutral vocabulary for the outcome of one logical operation across those
physical invocations.

Two failures follow from that diffusion:

1. **No bounded, single-owned retry budget.** Nothing capped the *total* retries and
   *cumulative* sleep for one logical operation, so a batch could sleep unboundedly or
   re-invoke a non-idempotent write more than intended.
2. **Blind replay after ambiguity.** After a commit-unknown result there was no
   *observe-before-replay* step: the code either replayed blindly (risking a duplicate)
   or gave up (losing a recoverable operation).

## Decision

Introduce a provider-neutral **logical-operation contract** and consolidate retry
ownership in the reconciler.

- **One retry budget, in the reconciler.** `retry_budget.RetryBudget` is the *sole* owner
  of the physical-invocation budget for a logical mutate operation: at most **three**
  invocations (the initial call plus its retries) and at most **15 seconds** of cumulative
  sleep. The budget is fed by an injected clock (`now()` / `sleep_ms()`) and a zero-arg
  jitter callable, so every decision is deterministic under test and performs zero
  wall-clock sleep. This is a **retry-ownership delta vs ADR 0087**: transport-layer retry
  stays for read-only LLM gate calls, but the reconciler's non-idempotent *ticket writes*
  are governed here, not by an SDK/transport retry that cannot see the logical-operation
  budget.
- **Additive jitter, retained from ADR 0084.** The delay floor is
  `int((2 ** (retry_index + 1) + jitter) * 1000)` — retry index 0 → `[2s, 3s)`, index 1 →
  `[4s, 5s)` — exactly the ADR 0084 / `acli_subprocess._rate_limit_backoff` additive-jitter
  schedule. A provider-supplied delay (a `Retry-After`-derived value) is an **authoritative
  lower bound** that is never shortened; when it exceeds the floor it wins and the outcome
  records `DelaySource.provider`.
- **Full/decorrelated jitter is REJECTED.** With only two bounded sleeps under a 15s cap,
  decorrelated or full jitter buys no herd-avoidance benefit that additive jitter does not
  already provide, while making the schedule non-obvious and harder to reason about at the
  boundaries. Additive jitter is retained deliberately.
- **Observe-before-replay after an ambiguous commit.** On a commit-unknown result the
  reconciler first *observes* current provider state, then consults a decision table
  (`decide_replay`): an already-*desired* observation is `recovered` (no replay); an
  `old_conclusive` observation replays only if budget remains (`retryable_deferred`) and
  otherwise is `exhausted_transient`; a `failed`/`inconclusive` observation is
  `commit_unknown` and **non-replaying**. `replay_safety_for` maps `commit_unknown` to
  `ReplaySafety.forbidden` — the contract-critical guard against duplicating a
  non-idempotent write.
- **Provider-neutral outcome values.** `operation_outcome.OperationOutcome` is a frozen,
  provider-neutral value with a stable `logical_id` across physical invocations, bounded
  and **allowlisted** diagnostics (routed through the ADR 0041 diagnostic-sanitization
  seam, capped in count and message length so no raw body, header, or credential leaks),
  and canonical bytes produced *only* through the `rebar._store.canonical` seam so
  equivalent outcomes serialize byte-identically.
- **Deltas apply to the selected route only.** A computed delta is applied to the route the
  operation selected, never fanned out across bindings — a **desired-state delta vs ADRs
  0004, 0026, and 0029**: the snapshot contract (0004), three-way-merge baseline (0026),
  and bidirectional status/echo suppression (0029) still arbitrate *what* the desired state
  is; this ADR governs *how* one logical operation is invoked, retried, and observed toward
  that already-arbitrated desired state, and scopes its effect to the selected route.
- **Lifecycle owners.** The reconciler owns the retry budget and the observe-before-replay
  decision; the value module owns outcome identity, diagnostics bounding, and canonical
  serialization; the store seam owns canonical bytes. No provider adapter owns retry policy.

## Consequences

- Non-idempotent ticket writes are retried under a single bounded budget with a safe
  observe-before-replay step, eliminating both unbounded sleep and blind duplicate writes.
- The outcome vocabulary is provider-neutral and byte-stable, so it can be recorded,
  compared, and reconciled without provider coupling.
- Transport/SDK retry (ADR 0087) and the ACLI subprocess backoff (ADR 0084) remain for their
  own domains; this ADR does not remove them, it narrows the reconciler mutate path's retry
  ownership to the budget defined here.

## Rollback

The contract is a leaf value module plus a stateless budget/decision module with no
migration and no persisted schema change. Rollback is reverting the two modules and this
ADR (and its index row); no data conversion is required, because outcomes are computed,
not stored under a new schema. This ADR is **provisional** and may be superseded once the
observe step is validated against live Data Center non-idempotency behaviour.
