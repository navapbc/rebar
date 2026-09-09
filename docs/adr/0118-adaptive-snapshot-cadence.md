# ADR 0118 — Adaptive snapshot cadence by byte/replay cost

**Status:** Accepted (task `nutlike-biophilic-sheep` / `da70-18a5-898d-47ea`)
**Date:** 2026-09-08
**Relation:** EXTENDS ADR 0035 (`0035-rc2b-snapshot-horizon-safe-replay.md`), which is NOT superseded.

## Context

ADR 0035 made ticket compaction safe under concurrent clones by combining a conservative fold
horizon with rebuild-on-stray. That decision owns the safety invariants: hot-edge events stay live,
snapshots are timestamped in the gap before the live tail, `source_event_uuids` records what was
folded, folded sources and prior snapshots are retained as `*.retired`, and `fsck
--repair-snapshots` can rebuild from the full log including retired raw sources.

Research ticket `melancholy-couped-turkey` (`67de-1b36-0d66-49a6`) found that the current cadence
extends those safe mechanics with an unsafe growth shape. `compact_plan.needs_folding` folds when
the count of foldable active events exceeds `compact.threshold` (default 10). Each fold writes a
complete reduced ticket state into a new `SNAPSHOT`, then retires the prior snapshot append-only.
For a ticket whose state keeps growing, retaining a complete snapshot every ~10 events makes retired
snapshot bytes grow Θ(N²). The benchmark recorded 5,357,939,635 retired-snapshot bytes at 8,000
comment events under the fixed-count control.

The same research compared two linear-growth candidates against the identical workload. A pure
geometric cadence bounded retained snapshots but let the replay tail grow to 2,880 events at 8,000
events, so it bounded storage by ignoring read cost. The byte/replay-cost candidate folded when
pending foldable source bytes reached `alpha * active_snapshot_bytes`, preserving the first-snapshot
arm and the count floor. At alpha 1.0 it reduced retired-snapshot bytes to 15,581,244 at 8,000
events, about a 344x reduction from fixed-count, while bounding the bytes replayed on top of the
snapshot to roughly one active-snapshot load.

The research also concluded that all candidates change only the WHEN trigger. They do not change
snapshot placement, event ordering, `source_event_uuids`, the retire/restore rule, crash recovery,
or rebuild-on-stray. The remaining unproven safety claim is empirical: two independent clones may
fold at different byte-cost points, and that divergence must be proven to reconverge through the
existing merge-as-union plus ADR-0035 rebuild-on-stray contracts before this project opts in.

## Decision

Adopt an adaptive snapshot cadence based on byte/replay cost, behind a new configuration key
`compact.snapshot_alpha`.

The global default is OFF. A default-off value preserves exact fixed-count behavior, so projects that
do not opt in continue using ADR 0035's current `compact.threshold` cadence. The implementation must
add the new key with an in-tree mechanism-ratchet marker at the definition site:

```python
# mechanism-ok: config_key compact.snapshot_alpha — da70-18a5-898d-47ea
```

When `compact.snapshot_alpha` is enabled with a positive alpha, compaction must fold only after the
ADR-0035 horizon cut and when all of these are true:

1. there is at least one foldable source event;
2. the existing first-snapshot behavior and small-count floor are preserved, reusing
   `compact.threshold` so tiny tickets still compact and micro-churn does not write snapshots for
   every event;
3. pending foldable source bytes are greater than or equal to
   `alpha * active_snapshot_bytes`.

`pending_source_bytes` is the on-disk byte total of active foldable sources since the current active
snapshot. `active_snapshot_bytes` is the byte size of the current snapshot file. Both are derived
from existing files; this decision does not add a new event schema, snapshot schema, or sidecar.

Per-project opt-in is allowed through `rebar.toml`. This repository may enable the key only after an
independent two-clone reconvergence proof and alpha calibration have landed. The proof must use an
independent clone pair, not only a single-clone benchmark, and must show that divergent byte-cost
fold points converge after fetch/merge/replay/fsck. The calibration must justify the chosen alpha
against this project's live ticket-size distribution.

No existing oversized retired snapshots are rewritten by this decision. The cadence bounds future
snapshot growth; historical reclamation remains separate work.

## Consequences

- Retained full-state snapshot bytes move from Θ(N²) under fixed-count cadence to Θ(N) under the
  byte-cost cadence. The expected retained-snapshot amplification is approximately `1 / alpha` of
  the final live snapshot size.
- Replay cost becomes the explicit tradeoff. Positive alpha bounds the live-tail bytes replayed on a
  read to about `alpha * active_snapshot_bytes`, so total read work remains within a constant factor
  of loading the snapshot itself.
- ADR 0035's safety properties are preserved because the fold horizon, gap timestamp, append-only
  retired sources, crash recovery, mixed-version rollback, and rebuild-on-stray mechanics are not
  changed.
- The rollout is reversible. Clearing `compact.snapshot_alpha` or setting it back to the default-off
  value restores fixed-count cadence without data migration. Snapshots written under the adaptive
  cadence are ordinary ADR-0035 snapshots, so older binaries can read them and newer binaries can
  continue from fixed-count histories.
- This repository's opt-in is explicitly blocked until the implementation has passed the
  independent-clone reconvergence proof and calibration. Enabling the key in `rebar.toml` is a
  separate child ticket that depends on that proof.

## Implementation plan

1. Implement `compact.snapshot_alpha` default-off: config field, coercion, environment handling if
   consistent with existing compact keys, `needs_folding` inputs, and unit tests that prove default
   behavior is unchanged.
2. Add the independent two-clone reconvergence and calibration proof. The proof must exercise two
   clones with divergent adaptive fold points, merge-as-union, replay, and fsck/rebuild behavior;
   it must also record a recommended alpha from live-store size measurements.
3. Enable `compact.snapshot_alpha` for this project in `rebar.toml` only after the proof lands.

## Rejected alternatives

- **Raise `compact.threshold`.** This improves only the constant factor; retained snapshots still
  grow Θ(N² / threshold) for growing tickets.
- **Pure geometric count cadence.** It bounds storage but ignores event payload size and permits an
  unbounded absolute replay tail. Byte-cost cadence directly models the read/write tradeoff.
- **Delete prior snapshots instead of retaining them.** That would reduce storage but break ADR
  0035's append-only rebuild, crash-recovery, and merge-as-union safety model.
- **Enable globally immediately.** Rejected because the two-clone divergence proof is not yet in
  tree. Default-off keeps the implementation safe to ship before this repository opts in.

## Rollback

Rollback is configuration-only: remove `compact.snapshot_alpha` from `rebar.toml` or set it to the
default-off value, then run compaction normally. No migration or snapshot rewrite is required
because adaptive snapshots use the existing ADR-0035 snapshot envelope. If calibration chooses an
aggressive alpha that produces too much replay work, lower alpha or disable the key while retaining
all existing ticket history.

## References

- ADR 0035 — Snapshot-horizon-safe replay: conservative fold horizon + rebuild-on-stray.
- Research ticket `melancholy-couped-turkey` (`67de-1b36-0d66-49a6`) — deterministic benchmark,
  contract evaluation, rejected alternatives, and residual assumptions.
- Task `nutlike-biophilic-sheep` (`da70-18a5-898d-47ea`) — operator approval for the ADR-first,
  default-off, proof-gated rollout.
