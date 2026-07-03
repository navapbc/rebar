# ADR 0027: Reconciler binding lifecycle (pending → confirmed → retired; adoption gating)

- **Status:** Accepted
- **Context:** Epic *Level-triggered bridge convergence* (`3006-e198-13db-4e1f`).
  Documents the binding-store identity + retirement invariants before the redesign
  (children `5854` adoption, `13eb` GC) touches them. Lessons **L9, L10, L11, L14, L15**.

## Context

The binding store (`.tickets-tracker/.bridge_state/bindings.json`, `binding_store.py`)
maps `local_id ↔ jira_key` and is the authoritative set of synced relationships. Its
lifecycle and identity rules were each forced by a production incident:

- **L9 — the rebar-id label is the identity primitive.** Only three leaves may ever
  write a `rebar-id:`/`rebar-id-` label (outbound_create + inbound_create add,
  inbound_clean_label delete); every write is audited (`rebar_id_audit.py`, story
  `4496`). An unaudited identity write risks duplicate/ghost identities.
- **L10 — label-derived-binding suppression** (bug `4354`, `differ.py:377-424`): a
  *bound* issue present in `curr` but not `prev` must NOT be re-classified as unbound,
  or the pass mints a phantom `jira-dig-NNNN` local entity and writes a ghost
  `rebar-id:jira-dig-NNNN` label back to Jira.
- **L11 — dual-identity invariants** (story `7a75`, `invariants.py:228-301`): a missing
  back-pointer seeds a repair; a conflicting pointer, or a **double-bind** (two Jira
  issues claiming one `local_id`), **quarantines** the offenders, capped per pass.
- **L14/L15 — grace + reversibility** (bug `1e08`, `binding_store.py:292-364`): a
  confirmed binding is retired only after `RECONCILER_ABSENT_RETIRE_GRACE`
  **consecutive** direct-GET 404s; retirement is a **reversible** soft-delete to
  `bindings-retired.json`; a 200 GET resets the counter. A corrupt retired file
  fails **open** (empty set + alert), while `bindings.json` corruption fails **closed**.

The write-ahead protocol is `bind_pending → create → plant marker → bind_confirm →
save`, with `recover_pending_bindings` reconciling *pending* (never *confirmed*)
entries at startup.

## Decision

1. **Binding states are `pending | confirmed | retired`.** Confirmed is the steady
   state; retired is a reversible soft-delete, not a hard delete.
2. **Retirement of a confirmed binding requires a CONFIRMED 404 counted to grace**
   (ADR 0028 governs *why* absence ≠ 404). Never retire on snapshot-absence or a single
   miss. Retirement writes to `bindings-retired.json` and is re-recoverable if the
   marker/label reappears.
3. **Identity writes stay audited (L9).** No new path — including a unified
   `bridge-fsck` that now *reads* `bindings.json` — may write a `rebar-id` label
   outside the three audited leaves.
4. **Adoption of an unbound Jira issue MUST run the identity gates FIRST** (the
   `5854` policy is "adopt", but gated):
   a. consult `bindings-retired.json` (`is_retired`) so a just-retired issue is not
      resurrected into a delete/re-adopt loop;
   b. run the rebar-id audit (L9) and label-derived-binding suppression (L10) so an
      already-marked issue is treated as bound, not double-bound;
   c. seed the ADR 0026 baseline from the adopted fields so the first outbound diff is
      empty (echo suppression);
   d. key `create + bind` on the external id (idempotency) so overlapping passes can't
      double-create.
5. **The at-most-one-`local_id` and dual-identity invariants (L11) remain**, with their
   per-pass quarantine caps, as the backstop against identity corruption from any new
   binding-driven path.

## Consequences

- Class `5854` (adopt) is safe only with 4a–4d; the epic's convergence property must
  include an **adopt-skips-retired/labeled** cell and a **no double-bind** conservation
  property.
- Class `13eb` (GC) reuses the L14 grace machinery on the binding-store walk; the
  circuit breaker (ADR-adjacent) backstops a mass-retire.
- Retirement must never become a hard delete; recovery from `bindings-retired.json`
  must survive the redesign.
