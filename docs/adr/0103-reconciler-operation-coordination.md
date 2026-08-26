# ADR 0103 — Reconciler logical-operation coordination (single retry budget + observe-before-replay)

- **Status:** Accepted (finalized by RP-03 S5 T3 `a735-052e-8eca-4aeb`; supersedes the
  provisional status once the observe-before-replay step was validated end-to-end against
  portable verified-fake Cloud and Data Center passes in
  `tests/unit/rebar_reconciler/mutate/test_reconciler_coordinator.py`)
- **Date:** 2026-08-24 (finalized 2026-08-26)

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

## Failure taxonomy — eleven dispositions projected onto five buckets

One logical operation resolves to exactly one `operation_outcome.Disposition`, and the
reconciler projects that eleven-value vocabulary onto the five provider-neutral pass buckets
(`failure_policy.OUTCOME_BUCKETS` = `applied, recovered, deferred, failed, skipped`) so the
pass tally is exact and byte-stable:

| Disposition | Bucket | Meaning |
|---|---|---|
| `applied`, `already_satisfied` | `applied` | the desired state now holds (a write, or a no-op that was already satisfied) |
| `recovered` | `recovered` | an ambiguous commit was *observed* to already hold the desired state — folded into applied successes yet counted separately |
| `retryable_deferred`, `dependency_deferred`, `scope_deferred` | `deferred` | eligible next pass — budget remains, a dependency is unmet, or a fuse scope is open |
| `commit_unknown`, `permanent_failure`, `exhausted_transient`, `safety_aborted` | `failed` | not applied and driving a degraded exit (an ambiguous non-replayable commit, a terminal provider error, budget exhaustion, or an aborted safety precondition) |
| `skipped` | `skipped` | intentionally not attempted this pass |

`recovered` and the three `*_deferred` values are the outcomes that make the bounded retry
*safe*: recovered is the observe-before-replay success, and the deferred values keep genuinely
retryable work eligible without ever re-invoking a non-idempotent write. `commit_unknown` maps
to `ReplaySafety.forbidden` and is therefore `failed`, never `deferred` — an ambiguous commit
is never blindly replayed. Deferral, failure, skip, and `commit_unknown` are all disjoint from
applied, so the buckets sum to the mutation count (the exact-tally invariant validated by the
coordinator suite and `test_live_mode_failure_tally`).

## Fuse and containment — scope isolation, open, and reset

A run of same-scope exhaustion opens a **pass fuse** (`pass_fuse.PassFuse`) at the narrowest
`operation_outcome.FailureScope` that explains the failures — `ticket`, `endpoint`, `tenant`,
`provider`, or `global` — never wider. Containment is by scope key:

- **Only fuse-eligible work is deferred.** `failure_policy.FUSE_ELIGIBLE_DISPOSITIONS` is
  `{exhausted_transient, retryable_deferred}`; once a scope is open, remaining *matching,
  retryable* work is `deferred` carrying the exact `fuse_decision` (scope, reason, and a
  concrete `retry_not_before` derived from the cooldown). A genuine `permanent_failure` under
  an open scope keeps its own `failed` bucket and is **never masked** as deferred, so a real
  failure still drives the degraded exit.
- **Independent scopes are never conflated.** An open `endpoint`/`provider` fuse leaves an
  unrelated provider's or endpoint's operations fully `applied`; the fuse keys on the located
  binding, so cross-scope work is untouched.
- **Reset on proven health.** A same-scope SUCCESS arriving after the fuse opened re-closes
  that scope and is reported `applied` (not deferred) — observed health beats a stale open
  fuse, so the fuse cannot wedge a scope that has recovered within the pass.

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
not stored under a new schema.

**Rollback never deletes remote work.** Backing the coordinator route out is a code/routing
reversion followed by remote **re-observation** — the next pass re-reads current provider
state and reconciles toward the desired state. A rollback (and the create path it governs)
issues **no** `delete_issue` against the remote: an issue already created in Jira is recovered
by observation, never destroyed to "undo" a partially-applied pass. This no-remote-delete
invariant is proven behaviourally by the route census (`test_coordinator_route_census.py`, the
`delete_issue`-call-count assertions) and the create/rollback scenarios of the coordinator
suite. The observe-before-replay step was validated end-to-end against portable
verified-fake Cloud and Data Center passes, so the earlier provisional caveat is resolved.

## S1 lifecycle boundary — S3 owns production cutover

RP-03 S1 lands the outbound-summary seam as a *dormant* capability: `handle_update`
routes an exact-`{"summary"}` update through a constructor-injected `summary_executor`,
maps its `OperationOutcome` back to the manifest, and tags a terminal disposition with a
single redacted per-mutation error and any `retry_not_before`. In S1 that executor
DEFAULTS TO `None`, so production keeps taking the legacy generic path unchanged — S1
carries no config or environment gate and performs no production cutover.

**S3 is the SOLE owner of the production cutover** and of everything the terminal tags
feed: S3 (not S1, not S2) flips the seam live, owns the **fuse** and durable-deferral
consumer that reads the `retry_not_before`/disposition tags this stage only records, and
owns the **retirement** of the compatibility bridge — S3 is where the legacy
`dispatch_one.update_one` summary path is finally **retired** once the executor route is
proven. Until that S3 retirement the two paths coexist; S1 deliberately persists no
durable deferral and retires nothing.
