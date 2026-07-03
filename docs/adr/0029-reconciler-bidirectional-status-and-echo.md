# ADR 0029: Bidirectional status mapping & echo/loop suppression

- **Status:** Accepted
- **Context:** Epic *Level-triggered bridge convergence* (`3006-e198-13db-4e1f`).
  Documents the status-round-trip + echo invariants before the redesign (child `444d`
  archived→Done introduces a new outbound status write) touches them. Lessons
  **L2, L3, L21**.

## Context

Two-way status/label/comment sync ping-pongs unless (a) the status map round-trips
sanely and (b) the reconciler recognizes and drops its **own** writes coming back as
"remote changes."

- **L21 — lossy status map + annotation labels** (`robe-creek-zealot`,
  `outbound_fields.py:84-95`, `inbound_translate.py:116-145`): local statuses without a
  faithful live-Jira equivalent (`blocked`, `cancelled`) map to the nearest live state
  and are preserved losslessly via `rebar-status:` annotation labels. The **reverse
  map is canonical, not a naive inversion**: e.g. Jira `Done → closed`. Critically,
  **`archived` has NO entry on the reverse path today** — it is not produced outbound
  (it is in `excluded_statuses`), so nothing maps Jira `Done` back to local `archived`.
- **L2 — comment echo-breaker** (bug `85a1`): outbound comment echoes carry a
  `<!-- rebar:reconciler-echo -->` marker; the inbound differ filters them
  (`outbound_comments.py:38-43,307`; `inbound_differ.py:27-30,382,422`) so our own
  writes are not re-ingested as new Jira comments.
- **L3 — single-pass bidir suppression** (bug `3bf8`, PR #457,
  `inbound_differ.py:560-745`): within one pass, an inbound mutation that contradicts a
  same-pass **outbound** mutation (scalar field, label add-vs-remove, link add) is
  dropped. This requires outbound and inbound to be computed against the **same
  snapshot**, and an outbound-key→inbound-field map kept in sync.

## Decision

1. **The reverse status map is canonical** and must round-trip without oscillation. Any
   new outbound status write must have a defined, non-oscillating inbound counterpart.
2. **Introducing `archived → Done` outbound (child `444d`) REQUIRES closing the loop:**
   because inbound maps Jira `Done → closed` (not `archived`), a bare archived→Done push
   would let the next inbound pass flip local `archived → closed`. Before shipping
   archived→Done, EITHER add `archived` to the reverse status map (Jira-`Done`-on-a-
   locally-archived-ticket ⇒ stays archived) OR extend the single-pass suppression (L3)
   to cover it. The naive same-pass suppression does **not** cover it today, because
   `archived` is excluded from outbound, so there is no same-pass outbound status
   mutation to suppress against — this must be handled explicitly.
3. **Echo suppression is provenance-based (L2) + single-pass (L3), not merely
   idempotent.** The redesign keeps the echo marker and the same-snapshot
   outbound/inbound computation. Adoption (ADR 0027 §4c) additionally seeds the
   baseline so an adopted issue is not immediately pushed back.
4. **Assignee/identity round-trip stays EXACT-match** (L6): unmappable agent identities
   converge to *unassigned* rather than churning an unsatisfiable assign every pass.

## Consequences

- Child `444d`'s guardrail "add `archived` to the reverse map + single-pass
  suppression" is a hard prerequisite; the convergence suite gets an
  **archived-not-status-looped** regression cell (archive locally → one pass → local
  stays archived, Jira stays Done, no oscillation over N passes).
- The classifier computes outbound and inbound Decisions from the same observed
  snapshot so L3 suppression remains expressible.
- `issuetype` remains an approved non-synced field once bound (L19); status/echo
  changes here do not alter that exception.
