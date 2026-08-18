# Release notes

> **User-facing changelog: [CHANGELOG.md](../CHANGELOG.md).** This file tracks
> agent-visible *contract* changes (event/schema/API); the user-facing changelog
> of features and fixes lives in `CHANGELOG.md`.

Agent-visible contract changes, newest first. rebar shares one `origin/tickets`
across many clients, so contract changes are called out here when they could be
observed by an agent or a different rebar version.

## Library force bypass carries its audit reason on `force`

The public library lifecycle operations now share one force contract:
`force: str | None`, where `None` alone means the operation is not forced and a supplied
string is the audit reason. An explicitly empty string is still a supplied force and is
recorded as `(no reason given)`, matching a bare CLI `--force`. This applies to both
`rebar.claim()` and `rebar.transition()`.

For migration, `rebar.transition(force=True)` and
`rebar.transition(force_close="<reason>")` still work temporarily, emit the registered
deprecation warning, and map to the unified value. If a caller supplies both the canonical
string `force` and deprecated `force_close`, the canonical value wins. `reason` no longer
doubles as a force note; it is only the justification for a reason-required administrative
close.

This does not grant automated import a new bypass: an NDJSON replay close remains an ordinary
close and respects the target repository's completion-verification policy. The durable reduced
state key `force_close_reason` is unchanged for event-schema compatibility.

## BREAKING (pre-1.0) — the `resolved_statuses` config keys removed

Operator-approved early removal (**pre-1.0 pass #3**), 2026-08-12, same window and same
operator-ruling lever as the entries below. Task `f020` deleted the inbound absence-probe
port that read them; task `549c-032f-6cb0-4258` deprecated the now-inert keys in v0.11.0
and held the removal for sign-off, which has now been given.

| Removed surface | Kind | Use instead |
|---|---|---|
| cfg `[tool.rebar.jira].resolved_statuses` | config key | nothing — delete the line |
| cfg `[tool.rebar.reconciler].resolved_statuses` | config key | nothing — delete the line |
| env `REBAR_JIRA_RESOLVED_STATUSES` | env var | nothing — unset it |
| env `REBAR_RECONCILER_RESOLVED_STATUSES` | env var | nothing — unset it |

**There is no replacement setting.** Resolved/unresolved discrimination is owned by the
outbound path (ADR 0028); these keys configured the *inbound* absence probe, which no
longer exists. An operator simply removes the line from `pyproject.toml`.

**Tombstoned `warn`, so nothing breaks on upgrade.** Unlike `REBAR_LLM_MODEL` below,
these inputs are **inert** — nothing read them, and the behaviour they configured is
gone — so a still-set key does not abort the command. It logs that the key was removed,
the key is dropped, and loading continues. That is the same treatment as the other
retired inert config keys (`code_health.enabled`, `code_health.analyzers`,
`reconciler.lock_backend`, `reconciler.lock_max_retries`). (ticket `f408-64ad-ee41-46b6`)

## BREAKING (pre-1.0) — bare `REBAR_LLM_MODEL` env var removed

Operator-approved early removal (**pre-1.0 pass #3**), 2026-08-12, same window and same
operator-ruling lever as the two entries below. ADR 0057 made the per-class
`[tool.rebar.llm.model_classes]` slots the model-selection interface and deprecated the
bare scalar in v0.11.0; the env var is now **gone**.

| Removed surface | Kind | Use instead |
|---|---|---|
| env `REBAR_LLM_MODEL` | env var | the `[tool.rebar.llm.model_classes]` slots, `REBAR_LLM_<CLASS>_MODEL`, or `[tool.rebar.llm].model` |

**This one IS tombstoned, and fails LOUD.** Unlike the review surfaces below (CLI/library/MCP
kinds the tombstone registry does not cover), `REBAR_LLM_MODEL` is an `env` input an operator
may still have exported. A still-set value raises a targeted migration error with a non-zero
exit — `behavior="error"`, matching `REBAR_LLM_MAX_ITERS` — rather than being ignored, because
silently dropping it would quietly change which model every operation runs. The check lives in
`LLMConfig.from_env`, so it fires only when the LLM stack actually loads.

**The config key survives.** `[tool.rebar.llm].model` is NOT removed — it remains the
top-level model knob, now resolving **CLI > `[tool.rebar.llm].model` > `DEFAULT_MODEL`** with
no env channel. The per-class `REBAR_LLM_<CLASS>_MODEL` variables and the per-step workflow
`model:` override are untouched. The class-slot precedence loses only its bare-scalar rung and
is now CLI > per-class env > config table > built-in default. (ticket `6cc4-56f1-6cc6-4c7f`)

## BREAKING (pre-1.0) — single-pass review public surfaces removed

Operator-approved early removal (**pre-1.0 pass #3**), 2026-08-12, using the same
operator-ruling lever as DE7 and the ticket-5899 sibling pass. Story 316a deprecated
the single-pass review operation in v0.11.0 in favour of the plan-review gate; its
three **public entry points are now gone**. Each old surface **now does nothing**
(CLI subcommand unrecognized; library attribute absent; MCP tool absent) — switch to
the replacement:

| Removed surface | Kind | Use instead |
|---|---|---|
| CLI `rebar review` | CLI subcommand | `rebar review-plan` |
| lib `rebar.llm.review_ticket()` | library function | `rebar.llm.review_plan()` |
| MCP `review_ticket` tool | MCP tool | the `review_plan` tool |

**The engine STAYS.** `rebar.llm.operations._review_ticket_impl` is untouched — the
workflow-parity harness and the eval solver still call it — as does
`schemas/review_result.schema.json`, which `review-code` still produces. The
synthetic `OUTPUT_SCHEMAS` key that pinned `review_result` moved from
`"review_ticket"` to `"review_code"`, its surviving producer, and the
`"review": plan_review_verdict` key went with the CLI verb.

**No tombstone row**, matching precedent: the tombstone registry covers only
`env`/`cfg`/`file` inputs, so a removed CLI verb, library function, or MCP tool
simply stops existing — exactly as `list-epics`, `--no-sync`, `rebar.list_epics()`
and the `list_epics` MCP tool did. The three `rebar._deprecations` rows are deleted
and the pass is recorded in that module's historical-record note.

**Downstream warning:** an external importer of `rebar.llm.review_ticket` will break
at import. This was announce-then-remove — the deprecation shipped in v0.11.0 and
signalled on every call — and the early removal is an operator ruling.
(ticket `97d4-3098-2fb6-4658`)

## BREAKING (pre-1.0) — dead `.reconciler-*` gitattributes strip arm removed

Operator-approved early removal (**pre-1.0 pass #3**), 2026-08-12. `rebar init`'s
`gitattributes` ensure-unit no longer strips the retired `.reconciler-* merge=ours`
line from an already-committed `.gitattributes`. That one-time migration arm (epic
dust-troth-naval / C4) existed because the reconciler pass-lock/phase-gate moved off
the tickets tree onto `refs/reconciler/*`; this store already swept it (commit
`826d5ae379`), so the arm was permanently unreachable here.

**Contract impact:** the unit is now create-only — it still writes `.gitattributes`
(carrying `.bridge_state/* merge=ours`) on a tracker that has none, and reports
`ok`/".gitattributes converged" for any tracker that has one. A clone last swept
**before 2026-07-05** that still carries the retired line will no longer be converged
by `init`; strip the line by hand if you have such a clone. No deprecation-registry
row and no tombstone are involved — this was internal store convergence, never a
user-facing input. (ticket `e654-58e2-0d38-48c6`)

## Scheduled bridge execution is runner-neutral

GitHub Actions, Jenkins, and GitLab now invoke the same
installed `rebar bridge run` command. Provider configuration retains checkout,
credentials, signing material, schedules, notifications, and time limits; profile routing,
pause recognition, exit translation, and strict ticket delivery live in the packaged Python core.
All providers therefore use the same five `MODE` values and 0/1/2 automation result contract.
The pip wheel, Homebrew formula virtualenv, and MCP Registry/uvx installation all carry that
same core; a checkout-relative script is no longer required.

This is an automation-wrapper change, not a legacy CLI contraction. Noun-based
`rebar bridge preview` and `rebar bridge sync` remain primary, while direct
`rebar reconcile --mode ...` and `python -m rebar_reconciler` callers retain their published
defaults, messages, mutation behavior, and benign 3/4/75 sentinels. Only the shared provider
adapter translates those benign sentinels to provider success.

## Destructive repairs now own a durable reconciler pause

Live `rebar fsck --repair` and `rebar doctor --repair` no longer depend on GitHub
Actions workflow disable/enable calls. Before any repair mutation, both commands now
CAS-create a uniquely owned pause on `refs/reconciler/gate`, then fail closed unless
`refs/reconciler/lock` is provably free. The same contract therefore applies to local,
GitHub Actions, Jenkins, GitLab, and bare-repository environments. A configured Git
`user.email` is required, and an existing pause always belongs to another operation and
is never replaced or cleared by repair.

Cleanup re-reads the pause document and OID as one snapshot and deletes only the exact
pause created by that invocation. Missing, changed, or concurrently replaced state—and
any transport, authentication, timeout, or CAS uncertainty—leaves the durable pause in
place for operator recovery. Inspect its owner and reason with `rebar bridge status`;
after confirming that no repair is still running and that the pause is safe to remove,
clear it explicitly with `rebar bridge resume`.

## Bridge operations are now first-class library and MCP APIs

The public library and MCP server now expose typed `bridge_preview`, `bridge_run`, `bridge_sync`,
`bridge_status`, `bridge_pause`, `bridge_resume`, and `bridge_check_access` operations.
The new inputs describe the operation directly and never accept the legacy mode vocabulary;
scheduled execution selects `profile=...` in Python/MCP or `--profile` in the CLI.
Preview remains strictly non-mutating; run and sync are explicitly mutating. MCP gates run,
sync, pause, and resume through both the read-only policy and `mcp.allow_jira_sync`, while status,
preview, fsck, and access checking remain available without that sync authorization.

This is additive. `rebar.reconcile(mode=...)`, MCP `reconcile(mode=...)`, and
`bridge_fsck` retain their names, defaults, schemas, return values, and error behavior.
The interactive setup wizard remains a CLI-only operator flow.

## `bridge fsck` audits real offline state

`rebar bridge fsck` now returns exactly `unknown_event_types`, `binding_drift`, and
`store_integrity`. The former `orphaned`, `duplicates`, and `stale` result keys are removed:
they depended on legacy `SYNC` events that rebar never emitted and were permanently empty.
This is the output-contract break authorized for the bridge-vocabulary migration.

Unknown event detection now reads `refs/heads/tickets` directly, using a cheap Git grep followed
by top-level JSON verification for only residual candidates. That unknown-event arm does not
require a tickets checkout, and nested/comment text cannot become a finding. Git/ref/read failures
fail closed as an operational exit 2 rather than reporting a clean store. Against a materialized
tracker, `store_integrity` validates both directions of `bindings.json`; malformed JSON is an
operational exit 2, while an index inconsistency exits 1. Unknown types and the existing
informational binding-drift cells remain non-gating.

The compatibility `rebar bridge-fsck` entrypoint produces the identical new result and exit
semantics as canonical `rebar bridge fsck`. Public library/MCP symbol names remain
`bridge_fsck`; only their returned schema changes. The live `BRIDGE_ALERT` event/reducer path,
the bridge-alert JSONL channel, and compatibility `rebar bridge-status` are unchanged.

## Bridge maintenance commands are nested under `bridge`

The primary operator spellings replace the old vocabulary as follows:

- `rebar bridge-fsck` -> `rebar bridge fsck` for the mapping audit;
- `rebar bridge-probe` -> `rebar bridge check-access` for the live Jira capability round-trip;
- `rebar jira-onboard` -> `rebar bridge setup` for the onboarding wizard.

`check-access` remains a distinct child, not an fsck option. The public library and MCP
`bridge_fsck` symbol names are unchanged.

This is an expand-contract migration: `rebar bridge-fsck`, `rebar bridge-probe`, and
`rebar jira-onboard` remain compatibility entrypoints. Each alias and canonical command routes
through the same implementation, so existing automation retains its parser, output, state
effects, and exit policy while new scripts can adopt the nested vocabulary.

## `bridge preview` / `bridge sync` are primary

`rebar bridge preview` shows proposed Jira changes, while `rebar bridge sync`
applies them. Preview is lock-free and emits a deterministic field-level manifest.
Canonical sync retains a comparable manifest for capped and uncapped runs;
`bridge sync --max-changes N` also records its complete deferred remainder. Canonical
selection (`--only` / `--except`) narrows examination. `rebar bridge pause REASON`
temporarily stops scheduled synchronization, and `rebar bridge resume` clears it.

This expand-contract migration retains `rebar reconcile`, direct engine `--mode`,
and `--filter-local-ids`. Their historical defaults do not collapse: argument-less
`rebar reconcile` stays dry-run, argument-less direct engine invocation stays live,
and the legacy filter remains a post-computation write filter. Legacy uncapped LIVE keeps
its tally/no-manifest behavior. Reconcile-check remains its distinct lock-free diagnostic
and is not an alias for preview. See
[ADR 0092](adr/0092-bridge-primary-vocabulary-compatibility-adapters.md).

The production workflow maps its retained profiles exactly: `dry-run` → `bridge preview`,
`bootstrap-strict` → `bridge sync --max-changes 10`, `bootstrap-throttle` →
`bridge sync --max-changes 100`, and `live` → `bridge sync`; `reconcile-check` alone
continues through `rebar reconcile --mode reconcile-check` for its diagnostic contract.

Canonical `bridge preview` / `bridge sync` (including the direct-engine canonical verbs) now
expose only 0 success/benign, 1 operational failure, and 2 invalid invocation/configuration.
Converged, paused, another-pass-in-flight, reschedule, and the historical phase-gate outcome
are benign canonical exit 0 states with a stable one-line state marker. The engine still
classifies and executes a single pass; route adapters translate only the final status/message,
so repository and ref effects are identical.

This is a rolling migration, not removal of compatibility. `rebar reconcile --mode ...`,
direct-engine `--mode`, argument-less direct-engine invocation, and the existing library/MCP
reconcile adapters retain their historical defaults, messages, and 3 (pass in flight), 4
(phase gate), and 75 (reschedule) sentinels. Existing systemd units, Jenkins jobs, scripts,
checked-out workflows, and older environments therefore continue unchanged while new
automation moves to canonical 0/1/2. The production workflow no longer carries a 3/75
whitelist; its paused-marker commit-skip remains unchanged.

## Durable reconciler status and last-pass witness

`rebar bridge status` is the canonical status surface. It reads the authoritative
`refs/reconciler/last-pass` record together with the pause ref and live lock, applying the same
verdict precedence in text and JSON. `--max-age` explicitly enables staleness; without it a
matching successful pass remains healthy regardless of age. Healthy, paused, and running return
zero; foreign, failed, stale, and never-run return nonzero. Target identity resolves from
`--target`, then `REBAR_ENV_ID`, then the local tracker `.env-id`.

The hidden `rebar bridge-status` compatibility spelling routes through the identical parser and
core, but stays out of top-level help. `purge-bridge` remains removed with no replacement.

Mutating reconciler processes publish the schema-v1 last-pass ref before stopping their heartbeat
or releasing the lock. The only local status artifact is the rolling
`.tickets-tracker/.bridge_state/last-pass.json`; consumers ignore it unless pass and environment
match the ref. The canary now defaults to canonical status, with `github-api` retained as a
one-release rollback/bootstrap source selected by
`REBAR_CANARY_HEARTBEAT_SOURCE=github-api`. See
[ADR 0094](adr/0094-reconciler-last-pass-two-witness-status.md).

## The `[eval]` extra is removed (breaking, no alias)

`nava-rebar[eval]` no longer exists. Its only member was `inspect-ai`, which rebar never
imported — there were zero `import inspect_ai` sites in `src/`, and the eval module's own
implementation comment records the decision not to route through Inspect AI ("our 'model
call' is a whole tool-using agentic op ... wrapping it would add a dependency and an
impedance mismatch for no gain"). The module docstring that still advertised Inspect AI
routing, and the dead `INSPECT_MIN_VERSION` constant, are gone with it.

What agents and automation should change:

- **Installs.** Replace `nava-rebar[eval]` with `nava-rebar[agents]`. `pip` treats an
  unknown extra as a warning, not an error, so a stale `[eval]` install silently gets the
  base package — it will not fail loudly. `uv sync --extra eval` *will* fail; update it.
- **Capability probes.** `rebar._optional.EXTRAS` no longer has an `eval` key, so
  `extra_installed("eval")` / `require_extra("eval")` now report it as unknown.
  `rebar llm setup` no longer reports an `eval` row (text) or an `extras.eval` key (JSON),
  and `rebar.llm.config.available_backends()` no longer returns `eval_extra`.
- **Live prompt evals** were always gated on `[agents]` + credentials, not on `[eval]`.
  The CLI hints that said otherwise now say `agents`.

Resolution side effects, all in the consumer's favour: the `eval`-vs-`dev` and
`eval`-vs-`bedrock` `[tool.uv] conflicts` entries are removed (`dev` and `bedrock` are
co-installable again), as is the `click>=8.3.3` `override-dependencies` entry — the lock
reaches a non-vulnerable click without forcing an untested combination.

## `transition --force-close` renamed to `transition --force` (breaking, no alias)

`rebar transition` now has ONE escape hatch, `--force[=<reason>]`, spelled exactly as
`rebar claim --force[=<reason>]`. It bypasses whichever gate the transition would hit —
the start-work (plan-review) gate on `open -> in_progress`, the completion-verification /
signature gate on `-> closed`. A bare `--force` on a close records `(no reason given)`.

The former `--force-close=<reason>` is **removed with no deprecation alias** and is now
explicitly rejected with a non-zero exit and an error naming `--force`. Agents and scripts
that still emit `--force-close` will fail loudly rather than silently closing through the
gate. Update stored agent instructions and automation accordingly.

That 0.12.0 release changed only the CLI: at that release the library kwarg
`rebar.transition(..., force_close=...)` was unchanged, and no force bypass was exposed over
MCP. The newer library-normalization note above supersedes the first statement for current
callers.

## Project policy cutover — plan-review material pins and close reviews

The tracked `rebar.toml` for this project now enables
`verify.enforce_plan_material_pins` and `verify.require_plan_review_for_close`.
Agents working in this repository must refresh a review after changing material
that it pins, and must have a current execution-phase review before an ordinary
close. Both remain `false` defaults in the reusable configuration schema, so
downstream projects retain their existing opt-in posture. Legacy attestations with
no material pins remain compatible, and the change introduces no backfill, stored
relation migration, list-output change, or new pin-management command. (ticket
`145e-52a9-26e3-4209`)

## ONE-WAY DOOR — legacy signature-mirror rollback lever removed (task 7ed9)

The CONTRACT-phase rollback toggle `compact.emit_legacy_signature_mirror` (env
`REBAR_COMPACT_EMIT_LEGACY_SIGNATURE_MIRROR`) has been **removed**. Its default
already meant "never persist the legacy `state['signature']` mirror into new
snapshots", so this changes **no runtime behavior** — new snapshots carry only the
kind-keyed `attestations` map, exactly as before. The key is now an unknown key
(warned + ignored), and `CompactConfig` no longer carries the attribute.

**Downstream warning — this is a ONE-WAY DOOR.** 352b let an operator re-emit the
mirror by flipping this config key and recompacting. That escape hatch is gone. An
**external project still running pre-attestations rebar** (which reads
`state['signature']` directly and has no `attestations` map) that encounters a
mirror-less snapshot whose latest signature was compacted below the horizon can **no
longer recover via configuration** — the only remaining recovery is a **code
downgrade** to a binary that still writes the mirror. This is **safe for fleets that
auto-update to `origin/main`** (no pre-attestations binary remains live), which is
this deployment. The in-memory re-derivation is untouched: a migrated reader still
re-projects `state['signature']` from the attestations on every replay, so signature
verification keeps working on a compacted, mirror-less ticket. (ticket `7ed9`; see
`docs/migrations.md` "Legacy signature-mirror retirement".)

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
| cfg `verify.require_verdict_for_close` | config key | cfg `verify.require_completion_verification_for_close` |
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
