# ADR 0026: Reconciler three-way-merge baseline (direction arbitration)

- **Status:** Accepted
- **Context:** Epic *Level-triggered bridge convergence* (`3006-e198-13db-4e1f`);
  documents the invariant introduced by commit `16870dbec` before the convergence
  redesign (children `444d`/`5854`/`13eb`/`8de5`) touches it. Lesson **L1**.

## Context

Bidirectional field sync needs a **common ancestor** to tell a *local* edit from a
*remote* edit — otherwise a two-way sync degenerates into "whichever side we scan
last wins," silently reverting the other side's changes.

The reconciler learned this the hard way (commit `16870dbec`, *"inbound field sync —
mirror Jira-side edits instead of reverting them"*): before it, the outbound path was
unconditional local-wins, so **a field a teammate changed in Jira was reverted to the
local value on the next pass. Verified live: assigning a ticket in Jira was reverted
to unassigned.** (This is exactly the class `REB-532`, "assignee clobber repro,"
exists to reproduce.)

The fix introduced a three-way merge: `prev_snapshot` — the previous pass's Jira
snapshot — is the common ancestor. `outbound_fields._local_matches_prev`
(`outbound_fields.py:262-278`) asks *"does local still equal what Jira had at the last
sync?"* for the five inbound-mirrored scalar fields
(`title`/`description`/`priority`/`status`/`assignee`, `outbound_fields.py:259`):

- **local == baseline** → local is unchanged since sync → any divergence from *current*
  Jira is a **Jira-side edit** → the outbound push is **suppressed** so the inbound
  differ mirrors it into local.
- **local != baseline** → a genuine **local edit** → local-wins outbound push.

The baseline is consumed at `outbound_differ.py:643` / `outbound_fields.py:303-360`,
loaded per pass at `reconcile.py:722-742`, and advanced at pass end
(`reconcile.py:1401-1407`). A corrupt/conflict-marked baseline **aborts the whole
pass** fail-closed (`reconcile.py:775-833`): the pass must *never* proceed from an
unknown Jira baseline. The inbound differ carries no baseline of its own, so it cannot
recover this direction signal — the baseline is the *only* source of it.

## Decision

1. **The reconciler MUST retain a three-way-merge baseline** (a per-pair "last-synced
   value") for the inbound-mirrored fields. Direction arbitration is: `local ==
   baseline ⇒ mirror Jira inbound (suppress outbound)`; `local != baseline ⇒
   local-wins outbound`.
2. **An absent/empty baseline degrades to local-wins** (`outbound_fields.py:269-272`).
   This is *safe but lossy* — it loses Jira-edit mirroring, it does not corrupt data —
   and is the only acceptable fallback (e.g. first sight of a pair).
3. **A corrupt baseline fails the pass CLOSED.** Reconciling from an unknown/partial
   baseline is forbidden.
4. **The baseline may be re-keyed per binding** (a last-synced field cache on the
   binding entry) instead of a whole-Jira `prev_snapshot`. This is *encouraged*: it
   makes the level-triggered/binding-driven loop possible AND unlocks **per-field**
   3-way merge (only the fields that actually changed on each side move), reducing
   false clobbers when both sides edit *different* fields. **Binding-*presence* alone
   is NOT a substitute for the baseline VALUE** — you cannot arbitrate direction
   without the ancestor value.

## Consequences

- The convergence redesign must **carry the baseline forward**; removing
  `prev_snapshot` without a replacement last-synced store re-opens `16870dbec` and is
  explicitly rejected (the "corrected P4").
- The convergence property suite must include a **direction-preservation** property: a
  teammate's Jira-side edit to any of the five fields survives a reconcile pass (is
  mirrored, never reverted). This is a first-class regression cell, not an example.
- Whatever store holds the baseline inherits the ADR 0004 producer↔consumer contract
  discipline and the fail-closed-on-corruption rule.
