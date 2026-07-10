# Release notes

> **User-facing changelog: [CHANGELOG.md](../CHANGELOG.md).** This file tracks
> agent-visible *contract* changes (event/schema/API); the user-facing changelog
> of features and fixes lives in `CHANGELOG.md`.

Agent-visible contract changes, newest first. rebar shares one `origin/tickets`
across many clients, so contract changes are called out here when they could be
observed by an agent or a different rebar version.

## BREAKING (pre-1.0) — remaining uncatalogued deprecations removed

A sibling breaking pass to DE7 removed the five remaining **scheduled** (removable)
deprecation shims — every entry left in `rebar._deprecations.REGISTRY` is now a
**permanent** ergonomic rename with no removal planned. Each old name below **now
does nothing** (env alias ignored; config value/key rejected as unknown; CLI
subcommand/flag unrecognized; MCP tool absent) — switch to the canonical
replacement:

| Removed surface | Kind | Use instead |
|---|---|---|
| env `REBAR_LLM_MAX_ITERS` | env var | env `REBAR_LLM_MAX_STEPS` |
| cfg `reconciler.lock_backend = "file"` | config value | (drop it — the `refs/reconciler/*` ref lock is the only backend; the `lock_backend` key itself is gone) |
| CLI `rebar list-epics` | CLI subcommand | `rebar list --type=epic --status=open,in_progress --unblocked [--min-children=N]` + `rebar list --type=bug --priority=0` |
| CLI `--no-sync` (read flag) | CLI flag | `--no-pull` |
| MCP `list_epics` tool | MCP tool | the `list_tickets` tool (`ticket_type="epic", status="open,in_progress", blocking_state="unblocked", …"`) |

Notes: the `list_epics` output schema (`schemas/list_epics.schema.json`) and its
`ListEpics` public TypedDict were removed with the surfaces. The permanent
ergonomic env renames (`REBAR_NO_SYNC`, `COMPACT_THRESHOLD`, `SCRATCH_BASE_DIR`,
`REBAR_ACLI_TIMEOUT`, `RECONCILER_ABSENT_GET_BUDGET`, `REBAR_ID_GUARD_MODE`) are
unaffected and still honored. (ticket `unclear-verymad-sablefish`)

## BREAKING (pre-1.0) — deprecated back-compat aliases removed (DE7)

Eight scheduled deprecation shims were removed at the pre-1.0 breaking-change
window. Each **old name now does nothing** (env aliases are silently ignored; the
config alias / flat reader are treated as unknown; the CLI flag and library
kwarg/function are gone) — switch to the canonical replacement:

| Removed surface | Kind | Use instead |
|---|---|---|
| env `REBAR_PUSH` | env var | env `REBAR_SYNC_PUSH` |
| env `TICKETS_TRACKER_DIR` | env var | env `REBAR_TRACKER_DIR` |
| env `REBAR_MCP_ALLOW_RECONCILE_LIVE` | env var | env `REBAR_MCP_ALLOW_JIRA_SYNC` |
| cfg `verify.require_verdict_for_close` | config key | cfg `verify.require_signature_for_close` |
| flat `.rebar/config.conf` reader | config file | `rebar.toml` or a `[tool.rebar]` table in `pyproject.toml` |
| lib `edit_ticket(tags=…)` | library kwarg | `edit_ticket(set_tags=…)` (or `add_tags=` / `remove_tags=`) |
| lib `rebar.list_epics()` | library function | `rebar.list_tickets(ticket_type="epic", status="open,in_progress", blocking_state="unblocked", …)` (+ `ticket_type="bug", priority=0` for the P0 bugs) |
| CLI `--verdict-hash` (transition) | CLI flag | `rebar sign <id> <manifest>` (the certified-signature close gate) |

Notes: at the time of DE7 the CLI `list-epics` command and the MCP `list_epics`
tool were kept (composing `list_tickets` internally) and only the
`rebar.list_epics()` *library* function was removed; the follow-up pass above
(`unclear-verymad-sablefish`) has since removed those two surfaces as well. The
permanent ergonomic env renames (`REBAR_NO_SYNC`,
`COMPACT_THRESHOLD`, `SCRATCH_BASE_DIR`, `REBAR_ACLI_TIMEOUT`,
`RECONCILER_ABSENT_GET_BUDGET`, `REBAR_ID_GUARD_MODE`) are unaffected and still
honored. (ticket `imposing-petite-xenopus`)

## 0.7.1 — MCP Registry auto-published; first fully-automated release

The `mcp_registry` job (GitHub Actions OIDC) now auto-publishes `server.json` to
the MCP Registry on a tag, so all three distribution channels + the GitHub Release
are hands-off from one `vX.Y.Z` tag push — no interactive `mcp-publisher login`.
This is the first release cut through the complete automation (PyPI + GitHub Release
+ MCP Registry), and the first real end-to-end run of the auto GitHub Release
(0.7.0's was a manual one-off due to a job bug fixed since). (ticket `dazed-cherry-knelt`)

## 0.7.0 — GitHub Releases now auto-created on tag

The release workflow (`.github/workflows/release.yml`) now creates the **GitHub
Release** automatically on a `vX.Y.Z` tag push (auto-generated notes, marked
Latest, sdist + wheel attached), so `github.com/navapbc/rebar/releases` no longer
lags PyPI/Homebrew/MCP. No maintainer action needed; see `docs/releasing.md`. This
is the first release cut *through* that automation. (ticket `wormy-sod-gorge`)

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
