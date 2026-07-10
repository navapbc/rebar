# ADR 0039 — Write-time batched commits, scoped to bulk import (a durability carve-out)

**Status:** Accepted (epic cold-stall-chalk — write-time op batching)
**Date:** 2026-07-10

## Context

rebar's store commits **one git object graph per event**: every mutation takes the
write lock, appends one event file, and makes one `git commit`. This is the
deliberate durability trade recorded in `docs/scale-envelope.md` — "every write is
immediately persisted and shareable" — and that document previously asserted there
was **intentionally no batched-commit fast path** because it "would weaken the
per-write durability guarantee."

Bulk import (`rebar._io.import_ndjson`) inherits that cost at scale: importing N
tickets emits thousands of commits (one per CREATE/EDIT/LINK/COMMENT/STATUS event),
which dominates import latency and sets the long-term growth *rate* of the `tickets`
branch. Object *reclaim* was already addressed by re-enabling stock gc (epic
`minor-haste-ogle`); this epic addresses the *growth rate at write time*.

The question this ADR settles: **can we add a batched-commit path without violating
the durability decision that scale-envelope.md records?**

## Decision 1 — Add `batch_stage_and_commit`, an all-or-nothing batch primitive

`rebar._store.event_append.batch_stage_and_commit` takes the write lock **once**,
atomically renames M event files into place, stages them, and makes **one**
`git commit` — collapsing N events into a single commit. It is all-or-nothing: a
failure mid-batch unstages/unlinks the whole batch (generalizing the single-event
path's unstage-on-failure recovery to the full path list), so a crash leaves either
the whole batch's commit or none. The single-event `stage_and_commit` path is
**unchanged**. The lock is not re-entrant, so the primitive acquires it once and does
all work inside — it does not loop the single-event path.

## Decision 2 — The batched path is **import-only**; interactive writes are never batched

The durability guarantee protects **interactive** writes — each `create` / `edit` /
`transition` / `claim` / `link` / `comment` is independently committed, pushed, and
visible to other clones immediately. Those keep committing **one event per commit**
and retain that guarantee unchanged. Batching is enabled **only** for bulk import,
enforced structurally by a context-scoped sink at the single `_seam.append_event`
boundary (not kwarg-threading through call layers), with a unit test asserting that a
normal `create`/`comment` still makes exactly one commit.

**Why import is exempt from the per-write guarantee.** Import is already a
fundamentally different transaction: it **already defers push** (runs with
`REBAR_SYNC_PUSH=off` and pushes once at the end) and is **idempotent-by-`source_id`
and crash-resumable** (a re-run re-scans already-imported `source_id`s and skips
them). So it never relied on the "immediately shareable" half of the guarantee.
Batching import therefore trades commit **granularity**, not durability: a crash
mid-import leaves either a whole batch's commit or none, and the re-run resumes
cleanly.

## Decision 3 — Invariant safety (I2 / I5) is preserved by construction

Batching a commit is **indistinguishable from N single commits to every reader**,
because identity keys off the **per-event UUID, not commit boundaries**:

- **I2 (event files):** every event remains its own `{timestamp}-{uuid}-{TYPE}.json`
  file; batching changes only how many commits wrap those files.
- **I5 (single locked writer):** the batch takes the same unified write lock, once.
- Replay, UUID dedup, cross-clone union-merge convergence, and SNAPSHOT compaction
  all operate on event UUIDs / filenames, so collapsing commits is invisible to them.

Which passes of the importer are batched follows independence: Pass 1 (CREATE) and
the independent wire-up passes are batched; **Pass 2b (links)** and **Pass 2e
(statuses)** stay per-event because they read state written earlier in the same pass
(link reciprocity / `_is_active_link`; children-before-parents close ordering).

## Decision 4 — The Jira reconciler is deliberately **excluded**

This epic is about the **local** ticket store. The Jira reconciler
(`rebar._engine.rebar_reconciler`) is a separate system — a *client* of the store —
and is explicitly **out of scope**: we do not route it through, nor extract a shared
primitive from, this primitive. Its own inbound write mechanics are a Jira-sync
internal. See `docs/architecture.md` **"Two writers, one store"** (added under this
epic) for the disambiguation, which exists precisely because agents scoping "batch
the store's writes" mistook the reconciler for the local write path.

## Consequences

- Bulk import makes far fewer commits (benchmarked before/after), bounding the
  `tickets` branch growth rate, with no change to interactive-write durability.
- `docs/scale-envelope.md` is corrected: the "no batched-commit fast path" assertion
  is replaced with the import-only carve-out and its rationale.
- The durability guarantee for interactive writes is now an **explicit, documented
  invariant** rather than an implicit consequence of one-commit-per-event — any future
  proposal to batch interactive writes must revisit this ADR.
