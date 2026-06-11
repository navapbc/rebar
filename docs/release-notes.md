# Release notes

Agent-visible contract changes, newest first. rebar shares one `origin/tickets`
across many clients, so contract changes are called out here when they could be
observed by an agent or a different rebar version.

## Auto-push policy — `REBAR_PUSH`

New env var (default `always`, unchanged behaviour): `always` pushes synchronously
on every write, `async` pushes in a detached background job (so per-write network
latency doesn't serialize a batch claim), `off` keeps commits local. All modes
keep convergence semantics — `fsck` still reports `PUSH_PENDING`, non-fast-forward
still fetches+merges+retries. Read at the `_push_tickets_branch` chokepoint, so
CLI/library/MCP honour it uniformly. (ticket `hip-rod-graze`)

## Contract freeze (2026-06-09 breaking-change window)

Story `fatty-cipher-range` froze three agent-facing contracts while the post-
announcement window made breaking changes cheap. All are documented in `docs/`
and pinned by `tests/interfaces/`.

### Exit codes (`docs/exit-codes.md`)
- Canonical exit-code contract documented for all 41 dispatcher arms (0 success,
  1 runtime error, 2 usage error, 10 optimistic-concurrency mismatch).
- **Behavior change:** an unrecognized `--option` on the structured read commands
  `show` and `list` now exits **2** (was 1), matching `deps`/`ready`/`search`.

### Error envelope (`docs/output-schemas.md`)
- **Schema change:** `error_envelope` (`common.schema.json`) gains an optional
  `exit_code` integer. No other shape change; no migration shim (zero external
  consumers).
- **Behavior change:** under `--output json`, command **failures** now emit a
  schema-valid `error_envelope` on stdout (so agents never parse stderr prose).
  Text-mode stdout is unchanged. Covered: `show`, `deps`, `get-verify-commands`,
  `next-batch`, `create`, `claim`, `transition`, `reopen`, `delete`. Exempt
  (documented): the per-ticket gates (verdict, not error), the tolerant reads,
  and `clarity-check` (own always-JSON contract).

### Event-schema versioning (`docs/event-schema.md`)
- The event log now declares `SCHEMA_VERSION` (`ticket_reducer/_version.py`).
- **Forward-compat fix:** unknown `event_type` values are preserved-and-ignored —
  replay skips them without error, and **compaction no longer folds/deletes an
  unknown-type event file** (an older clone's compaction could previously destroy
  a newer clone's data). Also stops main compaction clobbering `*-PRECONDITIONS*`
  files.

### MCP output schemas (`docs/output-schemas.md`)
- Every MCP tool advertising an `outputSchema` is now validated against its
  canonical JSON Schema (mechanically enumerated from `list_tools()`).
- `list_epics` and `bridge_fsck` now advertise an `outputSchema` (added
  `ListEpicsOut`/`BridgeFsckOut`). `transition_ticket`/`reopen_ticket` remain
  intentionally model-less (their `from` key is a Python reserved word).
