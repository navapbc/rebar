# ADR 0028: Bound-but-absent ≠ deleted (membership is not value; confirm before destroy)

- **Status:** Accepted
- **Context:** Epic *Level-triggered bridge convergence* (`3006-e198-13db-4e1f`).
  Documents the fetch-window + absence invariants before the redesign (children
  `13eb` GC, `444d` terminal transition) touches them. Lessons **L13, L16, L17**.

## Context

The reconciler fetches a *working set* of Jira issues, not all of Jira. A key that a
binding points at can be **absent from that working set for reasons other than
deletion**, and conflating "absent from my query" with "deleted" is catastrophic —
it mass-retires bindings and/or re-emits every field of every out-of-window issue on
every pass.

Three lessons encode this:

- **L17 — split-JQL + Done window** (bug `f6cc`, `fetcher.py:23-94`): a single JQL hit
  the ~1000-issue ACLI ceiling, so the fetch is split into `status != Done` (active) +
  `status = Done ORDER BY updated DESC` capped at `_DONE_RECENT_CAP`. **Done issues
  older than that cap are alive in Jira but deliberately OUTSIDE the snapshot.** Their
  absence is expected, not deletion.
- **L13 — membership is not value** (bug `1e08`, `outbound_differ.py:73-135,450-644`):
  a bound key absent from the snapshot must NOT be diffed against `{}` (that re-emits
  every field every pass). Its liveness is resolved by a **bounded direct GET** (budget
  `K`, rotation by last-GET pass): `200` = alive overlay, `404` = `_DELETED`, else
  `_TRANSPORT_ERROR` = **defer**.
- **L16 — absent-alive fields shared to inbound** (bug `0702`,
  `outbound_differ.py:623-634`): a `200` overlay for an out-of-window key is shared to
  the inbound differ so it can mirror Jira→local without a second GET; **`404` and
  transport errors are deliberately excluded** so a gone issue is never inbound-mirrored
  (retirement stays outbound-owned).

## Decision

1. **Snapshot-absence is NOT a signal of deletion.** No destructive or terminal action
   (binding retirement, terminal transition, "diff against `{}`") may be driven by a
   key's absence from the fetched snapshot.
2. **Deletion is proven only by a bounded direct GET returning 404**, counted to grace
   (ADR 0027 L14). A `_TRANSPORT_ERROR` defers; a `200` is an alive-overlay.
3. **The direct-GET budget is bounded and rotated** (`K` per pass, oldest-GET first) so
   confirmation cost is amortized, never O(all-out-of-window-issues) in one pass.
4. **The level-triggered binding-driven loop MUST preserve this discrimination.**
   Iterating the binding store and reconciling from "currently-observed" state is
   correct ONLY if "not currently observed" routes to the L13 GET-probe/defer path, not
   to a "gone" verdict. A naive `observed = in-snapshot` that treats out-of-snapshot as
   deleted would mass-retire the entire Done backlog on the first pass (the exact
   failure the circuit breaker also backstops).

## Consequences

- Class `13eb` GC and class `444d` terminal transition are gated on a confirmed 404,
  never absence — this is stated in both tickets' guardrails and enforced by the
  convergence suite's **Done-beyond-cap-not-GC'd** and **transient-fetch-gap-not-GC'd**
  regression cells.
- The classifier's `observe_jira(key)` returns a four-way state
  (`present | confirmed-404 | absent-in-window | transport-error`), never a boolean;
  the state matrix routes each distinctly.
- The circuit breaker (refuse a pass mutating/retiring > N% of bindings) is the
  defense-in-depth backstop if this discrimination is ever violated by a fetch/JQL
  regression.
- **Confirmed hard-delete of a still-locally-present ticket now re-creates it (c244).**
  A proven 404 (`ARCHIVED_OR_MOVED`) preserves the local content, and — because the local
  ticket is authoritative and still present — the reconciler re-creates the Jira issue in
  the **same pass**: `_apply_inbound_delete` emits a `create_after_hard_delete` follow-on,
  and `applier.apply()` reconstructs the CREATE fields from the local ticket
  (`_map_local_to_jira_fields`) and injects a standard outbound CREATE into the pass's
  batch, so it flows through `create_one` (JQL dedup makes a retry idempotent; REST-budget
  exhaustion defers to the next pass). This closes the former `epic-3e36` gap where the
  follow-on was built but never consumed. A subsequent `bind_confirm` to the fresh key
  drops the stale reverse-index entry for the old (deleted) key in the same save.
