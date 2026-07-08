# ADR 0035 — Snapshot-horizon-safe replay: conservative fold horizon + rebuild-on-stray (RC2b)

**Status:** Accepted (epic adept-hedge-stain / 36d1 — RC2b, the f193 store-corruption follow-ups)
**Date:** 2026-07-07

## Context

Compaction writes a `SNAPSHOT` event that folds the events before it and records their
`source_event_uuids`. Replay (`reducer/_processors.replay_events`) does a **positional skip**:
every event that sorts before the latest snapshot is skipped, its effect assumed captured in the
snapshot's `compiled_state`.

That assumption breaks under concurrency. When two clones write and one compacts, the compacting
clone folds only the events **it** has seen. A concurrent event authored on the other clone —
which the compactor never witnessed, so it is **absent from `source_event_uuids`** — can, after a
merge-as-union reconvergence, land in the ticket directory sorting **before** the snapshot. The
positional skip then drops it *regardless of `source_event_uuids`*: silent data loss (the "RC2"
class). The live store held ~11 such orphaned events.

`SNAPSHOT_INCONSISTENT`/`ORPHAN_EVENT` in `fsck` already *detect* this shape; nothing *repaired* it,
and nothing *prevented* it.

## Decision

Adopt **Option 1 (rebuild-on-stray) + Option 3 (conservative horizon)** together. Option 2
(per-field LWW-by-timestamp) is deferred — it is a larger semantic change and the two adopted
options close the data-loss hole.

### Option 3 — conservative fold horizon (prevention)

Compaction folds an event only once it is older than `compact.COMPACTION_HORIZON_NS`
(`hlc.physical_now() - event_ts >= horizon`; default `1_800_000_000_000` ns = 1800 s). Younger
"hot-edge" events stay live `*.json`. Crucially, when any live events remain the SNAPSHOT is
**timestamped in the gap between the newest folded event and the youngest live one**, so the live
events sort *after* the snapshot and replay **on top** of `compiled_state` instead of being
positionally skipped. A concurrent sub-horizon sibling that merges in later therefore also sorts
after the snapshot and is applied, not dropped. `horizon <= 0` folds everything (pre-RC2b
behaviour; the offline test suite defaults to 0 so fresh events still compact).

`COMPACTION_HORIZON_NS` is a `CompactConfig` field with a `_SECTIONS['compact']` coercer and the
clean env alias `REBAR_COMPACTION_HORIZON_NS` (not the doubly-prefixed auto-derived name).

### Option 1 — rebuild-on-stray (remediation)

When `fsck --repair-snapshots` finds a still-present pre-snapshot orphan
(`SNAPSHOT_INCONSISTENT`/`ORPHAN_EVENT`) it calls `compact.rebuild_snapshot_from_full_log`, which:

- recomputes state from the **full ordered log including `*.retired`** via
  `reduce_ticket(include_retired=True)` — a raw replay with SNAPSHOTs stripped and no positional
  short-circuit, so the orphan's effect is captured;
- writes a fresh SNAPSHOT whose `source_event_uuids` now include the orphan, retires the folded
  sources (append-only `*.retired`, invariant I1), and increments a rebuild counter + WARNs;
- is **crash-safe** via a `.snapshot-rebuild.bak` sentinel written before any mutation and removed
  only after a clean round-trip (a fresh reduce reproduces the rebuilt state). A `.bak` present at
  entry means a prior rebuild was interrupted → it rebuilds again (idempotent).

The rebuild **bypasses the reducer cache** (the `include_retired` file set is never keyed to the
active-only dir hash), so it can never return a stale `*.json`-only cached state.

## Consequences

- **No silent sub-horizon drop.** A concurrently-appended pre-horizon event either replays on top
  of a gap-timestamped snapshot (prevention) or is folded back by the fsck rebuild (remediation).
- **Determinism preserved.** Replay still sorts by the HLC filename prefix; `reduce_ticket` twice
  over the same file set (any `os.listdir` order) yields equal `compiled_state`.
- **A3 (entitled-unsweet-nutria)** drives the live store to fsck-zero with
  `fsck --repair-snapshots` on top of this machinery.
- The horizon trades a little compaction latency (recent events stay live ~30 min) for
  convergence safety. `reducer/_sort.py` is unchanged.
