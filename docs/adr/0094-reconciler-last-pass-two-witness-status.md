# ADR 0094 — Reconciler last-pass two-witness status

**Status:** Accepted
**Date:** 2026-08-09
**Relates to:** [ADR 0031](0031-reconciler-ref-lock.md), [ADR 0092](0092-bridge-primary-vocabulary-compatibility-adapters.md)

## Context

The reconciler previously emitted several unrelated local artifacts: accumulating health files,
baseline files, and per-pass snapshot records. None was authoritative across ephemeral runners,
and the heartbeat canary inferred health from GitHub Actions run history rather than the process
that held the reconciler lease. A completed workflow was not proof that a pass published its
outcome, while a wall-clock heartbeat could not distinguish a slow live holder from a crashed one.

ADR 0031 established the prior art: small reconciler coordination facts belong on dedicated
`refs/reconciler/*` refs, use observed-OID compare-and-swap, and judge lease progress from OID and
fence rather than cross-host timestamps. Status needs the same cross-clone authority without
turning the completion record into another lock.

## Decision

Use two separate witnesses with distinct jobs:

1. `refs/reconciler/last-pass` is the authoritative rolling completion witness. It points to a
   schema-v1 JSON blob containing `pass_id`, `environment_id`, `outcome`, nullable
   `failure_kind`, UTC `completed_at`, and `lock_fence` provenance. Publication observes the
   current ref OID and uses the ADR-0031 CAS classifier. A moved ref is refetched and retried at
   most three times, sleeping 0.1 then 0.2 seconds; transport, authentication, permission, and
   other non-CAS failures are immediate hard failures.
2. `refs/reconciler/lock` remains the independent live-progress witness from ADR 0031. Status
   never treats the fence copied into the last-pass record as proof that a process is still
   running. The canary observes the live lock twice, one holder lease apart: advancing OID or
   fence proves RUNNING, while an unchanged lease proves CRASHED.

The mutating process writes its success or process-surviving hard failure before stopping the
heartbeat and before releasing the pass lock. If publication fails, the process exits nonzero but
still releases the lock. This ordering makes a successful process exit imply a durable outcome.

One optional local file, `.tickets-tracker/.bridge_state/last-pass.json`, carries rolling richer
detail. It is replaced atomically instead of accumulated. A consumer may use it only when both
its `pass_id` and `environment_id` match the authoritative ref, so a stale tickets worktree can
never override the ref.

`rebar bridge status` reads one snapshot of the completion ref, pause ref, and live lock. JSON and
text apply the same precedence: PAUSED, RUNNING, FOREIGN, FAILED, STALE, HEALTHY. NEVER_RUN applies
only when neither completion nor lock exists. Staleness is opt-in through `--max-age`; there is no
implicit threshold. Target resolution is explicit `--target`, then `REBAR_ENV_ID`, then
`local:<.tickets-tracker/.env-id>`. A missing, empty, or unreadable local id is an error rather
than an invented `reconciler` identity.

The hidden `rebar bridge-status` spelling routes through the same parser and core for compatibility.
`purge-bridge` remains retired.

The canary defaults to canonical status and explicitly targets `reconciler`. Setting
`REBAR_CANARY_HEARTBEAT_SOURCE=github-api` selects the one-release rollback source; GitHub run
history is also the bootstrap witness when a producer deployed before this change has not yet
written `last-pass`. The canary rejects leases above 480 seconds before waiting, preserving
headroom inside its fixed ten-minute job timeout.

## Rejected alternatives

- **Compare heartbeat timestamps across runners.** Rejected for the same clock-skew reason as ADR
  0031. `heartbeat_ns` and recorded completion time are diagnostic/age inputs, not live-lease proof.
- **Keep accumulating per-pass health and baseline files.** Rejected because an ephemeral or stale
  worktree is not authoritative, unbounded history obscures the current state, and three producers
  can disagree. Git history already retains prior ref objects where operational archaeology needs
  them; the status contract needs only the rolling head.
- **Put rich detail in the authoritative ref.** Rejected because high-churn or evolving detail
  enlarges the cross-clone protocol and its compatibility burden. The ref stays small and stable.
- **Use the recorded `lock_fence` as RUNNING evidence.** Rejected because it is provenance frozen
  at completion. Only a second observation of the live lock can prove progress or a stalled holder.

## Consequences and rollback

Operators get a fast, non-ticket-mutating status command whose unhealthy states are nonzero and
whose healthy, paused, and running states are zero. The reconciler owns one durable publication
point; legacy health/baseline/pass-record producers are removed.

Rollback is operationally explicit: set the canary source to `github-api`, then revert the status
consumer and producer together. Old binaries ignore `refs/reconciler/last-pass`; the ref and local
detail file are safe to leave in place. Do not roll back only the producer after consumers have
stopped using the GitHub bootstrap fallback, or a fresh environment will remain NEVER_RUN.
