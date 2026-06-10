# rebar event schema

Every ticket is a directory under the `tickets` orphan branch worktree
(`.tickets-tracker/<ticket_id>/`); every mutation is one append-only JSON **event
file** in it. State is computed by replaying the events (the reducer,
`src/rebar/_engine/ticket_reducer/`). Nothing is ever stored as compiled state
except the local, rebuildable `.cache.json` (gitignored — see docs/concurrency.md).

> This document is the **event** (write) schema. The **output** (read) contract —
> the replay-derived JSON shapes that `show`/`list`/`deps`/… emit, each backed by
> a JSON Schema under `src/rebar/schemas/` — is documented separately in
> [output-schemas.md](output-schemas.md). Outputs are compiled state, not events.

## Filename contract (I2)

```
${timestamp_ns}-${uuid}-${TYPE}.json
```

- `${timestamp_ns}` — high-resolution (`time.time_ns()`) clock prefix; determines
  replay order (lexical == chronological for equal-width ns integers).
- `${uuid}` — a fresh UUID4 per event; makes every filename globally unique, so
  two clients writing concurrently never collide and git merges the two files as
  a union (`ticket-lib.sh:85`, `ticket_txn.py`).
- `${TYPE}` — the event kind (below).

Dotfiles (`.cache.json`, `.tombstone.json`, `.env-id`, …) are NOT events and are
excluded from replay (the reducer globs `*.json` and skips names starting with `.`).

**New event kinds MUST use this scheme** and append-only semantics (I1).

## Event types

Replay dispatch: `ticket_reducer/_processors.py` (`process_*`).

| TYPE | Written by | Effect on replayed state |
|------|-----------|--------------------------|
| `CREATE` | `ticket-create.sh` | Seeds `ticket_type`, `title`, `parent_id`, `priority`, `assignee`, `description`, `tags`. Exactly one per ticket (fsck checks presence). |
| `STATUS` | `ticket_txn.py` (transition/claim) | Sets `status`; carries `current_status` (the optimistic-concurrency expectation) and `parent_status_uuid` (the prior STATUS uuid) for fork resolution. |
| `EDIT` | `ticket-edit.sh`, `ticket_txn.py` (claim) | Merges `data.fields` (title/priority/assignee/description/tags/parent) into state (last-writer-by-replay-order). |
| `COMMENT` | `ticket-comment.sh` | Appends `{body, author, timestamp}` to `comments`. |
| `LINK` / `UNLINK` | `ticket-graph.py` / `ticket-link.sh` | Add / cancel a relation. Relations: `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`, `discovered_from` (`ticket_graph/_links.py:CANONICAL_RELATIONS`). `relates_to` is reciprocal; the rest are directional. Only `blocks`/`depends_on` can create cycles. **Hierarchy promotion:** for `blocks`/`depends_on` only, the recorded endpoints are promoted up the parent hierarchy so the dependency is between comparable levels (epic↔epic, story↔story, task/bug↔task/bug), emitting a `REDIRECT: A→B promoted to …` note; the other (non-blocking) relations are recorded exactly as given. `UNLINK` is pair-scoped (no relation arg) and cancels the most-recent link for an ordered `<source> <target>` pair, one per event — and must target the *promoted (ancestor)* endpoint to cancel a promoted blocking link. |
| `FILE_IMPACT` | `set-file-impact` | Records the `{path, reason}` array `next-batch` uses for conflict-aware scheduling. |
| `VERIFY_COMMANDS` / `PRECONDITIONS` | `set-verify-commands` / preconditions util | Record DD-level verify commands / precondition metadata. |
| `ARCHIVED` | `archive` / lifecycle | Marks the ticket archived (excluded from the default list). |
| `SNAPSHOT` | compaction (`ticket-compact.sh`) | Folds a run of prior events into one compiled-state event under the write lock; the folded files are renamed `*.retired` (I1's only exception). `data.source_event_uuids` lists what it folded (fsck cross-checks this). |
| `BRIDGE_ALERT` / `REVERT` / `SYNC` | reconciler / revert | Jira-bridge alerting, event reversal, and bridge sync bookkeeping. |

## Replay & fork determinism

- Events replay in `${timestamp_ns}` filename order; the reducer is pure
  (deterministic given the file set).
- **STATUS forks** (two STATUS events sharing a `parent_status_uuid` — e.g. two
  clients transitioning the same ticket concurrently) are resolved **skew-
  independently by the lexically-lower event UUID** (`_processors.py` `process_status`),
  so every clone converges to the same winner regardless of clock skew or replay
  order (invariant I8). Other event kinds (COMMENT/EDIT) are best-effort by
  timestamp.

## Compaction (I9)

Compaction runs under the per-clone write lock, writes a `SNAPSHOT` that folds
the events it retires, and renames the folded files to `*.retired`. A remote
clone appending a new (unique-named) event merges as a union; the SNAPSHOT must
already fold any event its result depends on. Never retire an event a
not-yet-folded state could still need.

See `docs/concurrency.md` for the I1–I9 invariants and the merge-as-union
sync/reconvergence algorithm, and `docs/architecture.md` for the components.
