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
- **Confirmed hard-delete of a still-locally-present ticket is TOMBSTONED, never
  re-created (supersedes c244; bug `3b5f`).** This Consequence previously asserted that a
  proven 404 re-creates the Jira issue in the **same pass**, via
  `_apply_inbound_delete`'s `create_after_hard_delete` follow-on. That was inaccurate on
  two counts and is corrected here:
  - It never ran. The follow-on's producer chain began at
    `differ._compute_mutations_emit_absent_partner_probes`, which only emits for a
    `local_state` entry carrying a bound `jira_key`; its sole production caller passes a
    FETCHED Jira snapshot, whose entries never carry one. So the `(inbound, probe)` →
    `(inbound, delete)` → `create_after_hard_delete` chain had no live producer — the
    `epic-3e36` gap it claimed to close had silently re-opened for a different reason.
  - Re-creation is no longer wanted. Operator ruling (`3b5f`): once an issue is deleted in
    Jira it must not be resurrected — the deletion is an intent, and re-creating the issue
    overrides it. Prior art agrees (Unito refuses to re-create deleted work items;
    Kubernetes reconcilers treat a missing object with `IgnoreNotFound`).

  The current behaviour: a confirmed 404 counted to grace **retires** the binding
  (`RETIRE_AFTER_GRACE`), which unbinds the local ticket. The unbound-create arm in
  `outbound_differ` then consults the retired-side tombstone
  (`BindingStore.retired_key_for_local`) and **suppresses** the create it would otherwise
  emit, writing a `outbound-create-suppressed` bridge alert that names the local id, the
  retired key, and `BindingStore.unretire(<jira_key>)` as the documented route back. A
  NEVER-bound local ticket has no tombstone and still creates normally. The whole
  resurrection implementation (`_apply_inbound_delete`, its `("inbound", "delete")`
  registration, the `_apply_inbound_probe` marker leaf, and the
  `create_after_hard_delete` consumption in `applier.py`) was removed rather than left
  orphaned.
- **Absence-vs-deletion discrimination lives on the OUTBOUND side**, consistent with L16's
  "retirement stays outbound-owned": `outbound_differ._safe_get_issue` (the bounded direct
  GET, 404 → `_DELETED`) and `binding_walk`'s binding-driven loop, which maps the GET onto
  the four-way `ObservedJira` state and advances grace only on `CONFIRMED_404`. The
  inbound absence-probe port (`SupportsAbsenceProbe.probe_remote`, `inbound_probe.py`)
  survives as a **dormant** capability with no consumer: removing it would drop the DC
  adapter's only import of the shared `classify_probe_response`, leaving that classifier
  Cloud-only and weakening epic `e369`'s AC5.
