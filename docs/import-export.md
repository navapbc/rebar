# Import / export (NDJSON interop projection)

`rebar export` / `rebar import` (and the library `rebar.export_tickets()` /
`rebar.import_tickets()`) move tickets in and out of the store as **NDJSON — one
full ticket object per line**. This is a **lossy interop projection, NOT a
backup**: for a lossless, full-audit-trail backup use
`git bundle create tickets.bundle tickets` (the event log is the source of truth).
Two consumers:

- **Reporting / data-mining (primary).** A flat, row-oriented, stream-parseable
  artifact other tools read directly (DuckDB / jq / pandas / BigQuery).
- **Clean rebar→rebar migration (secondary).** Export a store, import into a
  different repo, optionally stripping external-tracker (Jira) associations so the
  tickets re-map into a new project.

Foreign-tracker **sync** (GitHub/Jira parity) is out of scope here — that rides the
reconciler (binding store + bidirectional differs + applier), not this one-shot,
stateless, additive, lossy import.

## Format

Each line is a compiled ticket state plus a `schema_version` discriminator
(`schemas/export.schema.json`). Run metadata (`exported_at`, `source_env`, counts)
goes to **stderr**, so every stdout line is a clean ticket object.

## Export

```
rebar export [-o FILE] [--status=S[,S]] [--type=T[,T]] [--parent=ID] \
             [--strip-external|--no-jira] \
             [--include-session-logs] [--exclude-archived] [--include-deleted]
```

Scope defaults: all work types and statuses (incl. closed); **session_log
excluded** (`--include-session-logs` to add); **archived included** carrying
`archived: true` (`--exclude-archived` to drop); **deleted excluded**
(`--include-deleted` to add). `--strip-external` removes ALL external-tracker
linkage provider-neutrally (`bridge_alerts`, per-comment `jira_comment_id`, any
provider id) — the only seam a future GitHub exporter inherits.

Export **streams**: it iterates ticket-id directories and `reduce_ticket`s each one
(never `reduce_all_tickets`, which would materialize every compiled state at once),
so memory stays flat regardless of store size.

## Import

```
rebar import [FILE] [--dry-run]      # reads stdin if FILE omitted
```

Import composes ordinary events through the **normal locked write path** (no
raw-event injection): `CREATE` (+ `STATUS` to reach non-open states, +
`EDIT`-parent, `LINK`, `COMMENT`, file-impact / verify-commands). It is a
**provenance** import, not raw fidelity:

- Tickets get **fresh local ids + fresh HLC timestamps** (foreign timestamps are
  never injected — HLC monotonicity is preserved). The source identity is kept as
  `source_id` / `source_created_at` / `source_author` / `source_env` (and per
  comment `source_author` / `source_created_at`).
- **Two-pass**, ordered so each step's preconditions hold: create → parents (while
  everything is still open) → links (blocking-link promotion re-runs
  deterministically) → file-impact / verify-commands → comments → statuses last
  (children before parents, satisfying the open-children close guard). A dangling
  parent / link target (a source id not in the import set) is **skipped with a
  warning, never a hard failure**.
- **Idempotent by `source_id`.** A streaming scan of the target at start builds
  `{source_id → local_id}`; a record whose `source_id` already exists is skipped.
  Re-runs and resume-after-crash never duplicate. Existing tickets are **never
  updated** (updating is sync = the reconciler, out of scope).
- `--dry-run` reports create/skip counts without writing.

### Performance & the large-import limitation (accepted)

Import **defers push** for its duration (`REBAR_SYNC_PUSH=off`) and pushes **once
at the end**, so a bulk import pays one network round-trip rather than one per
event.

**KNOWN LIMITATION:** there is currently no batch-commit primitive, so import does
**one git commit + one lock cycle per event** (`_store/event_append.py`,
`_store/lock.py`). A multi-thousand-event import therefore takes several minutes
and serializes the write lock for the duration. A batch-commit primitive is
deferred to a follow-up task.

For large imports:

- **Pre-compact the source** (`rebar compact-all`) before exporting, so each ticket
  replays from a SNAPSHOT and carries fewer events to re-compose.
- Import already runs with push deferred; if you are scripting the library path
  directly and want to be explicit, set `REBAR_SYNC_PUSH=off` for the import
  process and push once afterward.
