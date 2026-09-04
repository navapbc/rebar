# Changelog

All notable, user-facing changes to rebar are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and rebar
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are generated from [Conventional Commits](https://www.conventionalcommits.org)
with `git-cliff` and then hand-curated. Agent-visible contract changes live in
[docs/release-notes.md](docs/release-notes.md).

## [0.13.1] - 2026-08-30

### Fixed

- **MCP Registry publish no longer fails on description length.** `server.json`'s top-level
  `description` exceeded the MCP Registry's 100-character limit, causing the 0.13.0 release's
  `mcp_registry` publish job to reject the manifest with HTTP 422. Shortened the description
  to fit, and added a `release_guards.py` guard (wired into `release.yml`'s `authorize` job)
  that now catches an over-length description before build.
- **Plan-review and other gate LLM calls no longer silently lose prompt caching on fallback.**
  A mixed Bedrock-primary / Anthropic-fallback model chain collapsed prompt caching to "none"
  whenever the fallback candidate didn't share the primary's cache-key style, even though the
  primary (which serves the overwhelming majority of calls) supports it fine. This had silently
  doubled the median cost of gate LLM calls since 2026-08-12. Caching now follows the primary
  candidate's capability, while genuinely cross-provider-incompatible settings still fail safe.

### Ops

- Migrated operator-seeded SSM `SecureString` secrets (Gerrit admin password, signing keys,
  API tokens, etc.) to Terraform write-only arguments so their values are no longer read into
  cleartext state on every plan/refresh; imported the live `mcp-client-pat-*` SSM parameters
  that a prior apply had left undeclared.
- Internal module-size cleanup: split oversized `llm/config.py` and `llm/plan_review/__init__.py`
  ahead of the 800-LOC hard cap, and bounded the ticket-enrichment self-heal's re-enqueue
  fan-out to stop a churn-driven event-volume surge on the tracker's `tickets` branch.

## [Unreleased]

### Changed

- **BREAKING (pre-1.0): legacy reconcile compatibility surfaces have been removed.**
  The top-level CLI `rebar reconcile` route, direct-engine `--mode reconcile-check`,
  Python `rebar.reconcile`, MCP `reconcile`, and the persisted `reconcile_diagnostics`
  status field are gone. Use `rebar bridge preview` for live Jira-vs-local proposed
  changes, `rebar bridge fsck` for offline binding/integrity audit, and `rebar bridge
  status` for operational state. The direct-engine legacy route restores
  `--filter-local-ids` as an engine-only post-filter for live-DC harness scoping that
  primary `--only` cannot express for not-yet-bound inbound Jira keys. The scheduled
  profile spelling `reconcile-check` remains accepted by bridge runners and maps to
  canonical preview.

- **BREAKING (pre-1.0): `rebar.create_placeholder()` has been removed from the Python API.**
  Use `rebar.ensure_identity_for(provider, external_id, display_name)` instead. The
  canonical function still resolves an existing mapping first and otherwise mints the
  same placeholder identity, so the behavior is preserved while the compatibility alias
  disappears.

- **BREAKING (pre-1.0): `review-code` no longer accepts caller-selected reviewers.**
  The `rebar review-code` CLI, the MCP `review_code` tool, and the
  `rebar.llm.review_code()` library surface all dropped the compatibility-only
  `reviewers` input because the four-pass code-review gate has long selected its
  own overlays/reviewers internally. This removes a misleading no-op knob without
  changing the returned `review_result`: the output still carries a `reviewers`
  field reporting the reviewers that actually ran. Migrate by removing
  `--reviewer ...` and `reviewers=[...]` from callers.

- **`severity` is now optional on `review_result` findings.** Code-review and plan-review
  findings no longer carry an impact-only `severity` label that could disagree with the
  review-kernel's own `priority`/blocking decision; they now surface `priority` and
  `decision` instead, and the Gerrit `LLM-Review` comment plus the CLI text output show
  blocking/advisory rather than a severity word. `completion_verdict` findings (from an
  unrelated completion-verification pipeline) are unaffected and always continue to carry
  `severity`. See `docs/release-notes.md` for the schema detail.

## [0.13.0] - 2026-08-28

### Changed

- **BREAKING (pre-1.0): `rebar export` NDJSON (`schema_version` 1 → 2) now emits nanosecond
  timestamps as JSON STRINGS.** The js-safe decimal-string wire form the entries below adopted for
  MCP and CLI `--output json` is extended to the last surface left out: the NDJSON export projection
  (`rebar export` and its library twin `rebar.export_tickets()`). A `time.time_ns()` timestamp
  outside `[-(2**53)+1, (2**53)-1]` (`created_at`, `updated_at`, `comments[].timestamp`,
  `signature.signed_at`, and the `source_created_at` provenance on imported records) is now emitted
  as its EXACT decimal string, so a float64 consumer of the export artifact (`jq`, `node`, a DuckDB
  or pandas JS-based loader) no longer silently rounds it — measured on a real ticket, `rebar export`
  went from **4 → 0** unsafe integers, and `node`'s `JSON.parse` of a stored `…255001` no longer
  returns `…255000`. `EXPORT_SCHEMA_VERSION` is bumped to **2** to mark the wire-form change.
  The round-trip twin is preserved: `rebar import` coerces these fields with `int()`, so
  `export | import` still reproduces the EXACT nanosecond digits (proven by a regression test that
  pins the digits, not merely the type). Only out-of-range integers change form; `priority` and
  other in-range integers stay JSON numbers. **Migration:** parse these fields with `int(x)` /
  `BigInt(x)` (the export schema already typed them `["integer", "string"]`, so a conformant
  consumer already accepted the string form); what breaks is naive raw-integer arithmetic on the
  parsed value, which now fails LOUDLY instead of returning a rounded number.
  Bug `guilty-pusslike-wyvern` (`a8db-dc3c-983a-40b0`). See
  [docs/release-notes.md](docs/release-notes.md).

- **BREAKING (pre-1.0): CLI `--output json` now emits nanosecond timestamps as JSON STRINGS too.**
  The MCP-only wire form shipped in the entry below is extended to the CLI, the surface where users
  are explicitly told to pipe rebar JSON into `jq`/`node` — and those are exactly the float64
  consumers RFC 8259 §6 does not protect. Measured on a real ticket: a stored `created_at` of
  `1787860170488898642` came back from `node`'s plain `JSON.parse` as `1787860170488898600`,
  silently 42 ns wrong. Only integers outside `[-(2**53)+1, (2**53)-1]` change form; `priority` and
  other in-range integers stay JSON numbers. Per surface, unsafe integers before → after:
  (1) `rebar show --output json` **4 → 0**; (2) `rebar list` **2 → 0** / `rebar ready` **3 → 0**
  (with `search`, `session-logs`, `deps`, `summary` and the `--output llm` projections on the same
  emitter); (3) `rebar sign` **1 → 0** and `rebar verify-signature` **1 → 0** (`signed_at`), plus
  `rebar review-plan --status`; (4) `rebar audit show` **4 → 0**. **Not changed:** `rebar export`
  (NDJSON) and `rebar.export_tickets()`, a separately versioned projection with a round-trip
  contract against `rebar import` *(superseded — see the `guilty-pusslike-wyvern` entry above: export
  now adopts the same string wire form under `schema_version` 2)*; and the Python library facade,
  which returns arbitrary-precision
  Python `int`s and has no JSON boundary to lose digits at. **Migration:** `int(x)` / `BigInt(x)`
  coercion is unaffected. What breaks is naive raw-integer arithmetic on the parsed value
  (`jq '.created_at + 0'`, `data.created_at - start` in Node) — and it now breaks LOUDLY instead of
  returning a rounded number. No field was retyped — the schemas already typed these fields
  `["integer", "string", …]` and already described the string wire form with no surface qualifier —
  but `sign_result.schema.json`'s top-level description, which asserted the old CLI-integer /
  MCP-string split, is corrected. rebar's own in-repo consumers are fixed with it: both
  `conflict_bug_filing._recent_marker_comment` (the reconciler's conflict-bug filer) and
  `scripts/alert_dedup.recent_marker_comment` (the shared dedup behind the canary and
  dependency-advisory alert lanes) type-checked the comment `timestamp` with
  `isinstance(ts, (int, float))`, which a string fails, silently breaking their 24h
  accumulation cap; both now coerce with `int()`.
  See [docs/release-notes.md](docs/release-notes.md).

- **BEHAVIOR CORRECTION (pre-1.0): `fsck --output json`'s `issue_count` now agrees with the exit
  code.** It was computed as `len(issues)`, counting every emitted `KIND:` line — including the
  report-only kinds (`push_pending`, `status_fork_resolved`, `tracker_dirty_tmp_event`, and any
  `warn` line) that never drive the exit code — so a JSON consumer gating on `issue_count > 0` could
  disagree with a shell consumer gating on the exit code against the same store. `issue_count` is
  now the **counted subset** (the findings that respect each check's `is_issue` flag) and matches
  the exit code. Each `issues[]` item gains an additive `counted` boolean; report-only findings are
  `counted: false` and excluded from `issue_count` but still present in `issues[]`, so nothing is
  lost and `len(issues)` recovers the old total. An uninitialized/absent tracker now reports a
  single counted `not_initialized` issue, distinguishing its JSON payload from a clean store's empty
  `issues[]` (previously byte-identical). Applied consistently across CLI `--output json`, library
  `fsck_report()`, and the MCP `fsck` tool. The `issue_count` type is unchanged (integer, required);
  only its meaning changed. **Consumers** that relied on `issue_count == len(issues)` must compute
  `len(issues)` themselves; `issue_count` now answers "how many counted problems" (== the exit-code
  verdict). Bug `sugarcane-scrummy-arctichare` (`29c3-b025-04d7-454e`).

- **The MCP discovery lists are now bounded and refuse loudly when oversized.** An unfiltered
  `list_tickets` against a mature store returned 94.5 MB in one response over 177 seconds; the
  server never errored, so clients died in client-specific ways (`Transport closed`, or a
  silently truncated result) with no way to tell "too big" from "server died". `list_tickets`,
  `ready_tickets` and `search` are now bounded at the same ~90 KB client budget the workflow
  reads already used, and an over-budget result is REFUSED with a structured
  `response_too_large` error naming the match count, the size, the budget and a remedy — never
  a partial list, because a short list is indistinguishable from a complete one. Additive: a
  previously unbounded call gains a structured failure it never had. (`search` is included
  because its docstring already promised "bounded discovery results".) The budget is measured
  on the wire payload rather than the reducer rows, which under-report it on three axes — model
  defaults re-added after the lean projection, JS-safe integer quoting, and FastMCP emitting the
  result TWICE (`structuredContent` plus one `content` block per row, together 2.08x the
  structured object alone). The remedy is per-tool: `ready_tickets` takes no filters, so its
  refusal points at `next_batch(epic_id)` rather than at arguments it would reject. See [docs/release-notes.md](docs/release-notes.md).
- **BREAKING (pre-1.0): the discovery lists drop the signature material by default.** The lean
  list row — CLI `rebar list`, MCP `list_tickets` and MCP `ready_tickets` — now omits
  `authorship_ledger`, `attestations`, `signature` and `keyring` as well as `description` and
  `comments`. Those four were 88% of a "lean" row's bytes on a mature store (15,548 bytes per row
  became 1,726; a full `rebar list --status closed` over 2,696 rows went from 43,661,976 to
  4,747,485 bytes, a 9.2x reduction). **Two surfaces narrow a published default.** MCP
  `ready_tickets` previously returned the FULL ticket shape with no opt-out and now gains
  `full=True`; CLI `rebar list --output json` narrows because it shares the same projection —
  its row goes from 39 keys to 35, losing exactly those four and nothing else. Pass `full=True`
  (MCP) or `--full` (CLI) for the previous shape, or read a single ticket with `show_ticket` /
  `rebar show`, which are unchanged. The **library** `rebar.list_tickets` is unaffected — its own
  `full` defaults `True`, so the narrowing comes from each caller's flag default, not the
  library's — and so are `rebar validate` and `next-batch`, which do not ask for a lean row. See [docs/release-notes.md](docs/release-notes.md).
- **BREAKING (pre-1.0): the MCP server now emits nanosecond timestamps as JSON STRINGS.** Ticket
  timestamps are `time.time_ns()` values — 19 digits — and were going out as bare JSON numbers.
  RFC 8259 §6 only guarantees interoperability inside `[-(2**53)+1, (2**53)-1]`, and every MCP
  client parses JSON numbers as IEEE-754 binary64, so `list_tickets` and `ready_tickets` failed
  from GitHub Copilot CLI with `TypeError: Do not know how to serialize a BigInt`, while clients
  using a plain `JSON.parse` silently truncated the value instead of erroring. On the **MCP surface
  only**, an integer outside the JS-safe range is now emitted as its exact decimal string;
  CLI `--output json` and the Python library still emit integers *(the CLI half was superseded by
  the entry above; the library still emits integers)*. Affected keys: `created_at`,
  `updated_at`, `last_reopened_at`, `source_created_at`, comment `timestamp` /
  `source_created_at`, and the **required** `signed_at` on `sign_result`,
  `verify_signature_result` and `plan_review_status` — retyping a required key is why this is
  flagged BREAKING. **Consumers must** parse these with an arbitrary-precision integer (`int(x)` in
  Python, `BigInt(x)` in JavaScript) and accept both forms; `Number(x)` rounds silently (the stored
  value `1787856371950409998` reads back as `1787856371950410000`). The instant is unchanged and
  the conversion is lossless — same digits, different JSON type. `updated_at` is also now declared
  in `ticket_state.schema.json` instead of riding as an undeclared additional property. See
  [docs/release-notes.md](docs/release-notes.md).

- **Library and MCP reads now ERROR on an ABSENT *or present-but-unusable* ticket store instead
  of reporting it as an empty one.** `rebar.list_tickets` / `search` / `ready` /
  `recent_session_logs` / `deps` / `next_batch` (and the MCP tools that delegate to them)
  previously returned `[]` whenever the tracker *directory* was missing OR merely existed without
  being a usable store — a directory with no `.git` (the production shape: marker files only), or
  a `.git` whose HEAD has not landed yet mid-clone — which is indistinguishable from a store that
  exists and holds no tickets; `show_ticket` reported the misleading `Ticket '<id>' not found`.
  They now raise `RebarError` carrying the new `store_uninitialized` error code whenever the
  tracker is not a usable store. Usability is decided by one shared predicate
  (`_store.store_usability.store_is_usable`) that both the reads and the library WRITE path
  (`event_prepare._ensure_initialized`) flow through, so they can no longer disagree: a tracker
  is usable if it has `.git` with a resolvable HEAD, OR it carries the on-disk store structure
  (a committed `.store-compat.json` record, or a well-formed ticket event directory). The
  structure clause keeps a `.git`-LESS *materialized* ticket snapshot — the pinned, read-only
  tree `materialize_tickets` produces and the code-review gate agents read — READABLE, while a
  genuinely broken directory raises. **Migration:** a caller that relied on the empty-list degrade
  should catch `RebarError` and branch on `rebar.error_code_for(exc) == "store_uninitialized"`; an
  INITIALIZED-but-empty store (including a zero-ticket snapshot carrying the committed record)
  still returns `[]` unchanged, so only the absent/broken-store case is affected. A deployed MCP
  server whose store held only marker files had been answering every tracker query "no tickets"
  for an extended period because of this.
- **New error code `store_uninitialized`** in `rebar.KNOWN_ERROR_CODES`, so the fault above is
  machine-readable over the MCP envelope rather than requiring message-text matching.


### Fixed

- **uv is pinned to an exact version, so CI can no longer silently change which uv it runs.** `astral-sh/setup-uv` was SHA-pinned at all 25 call sites, but **uv itself was
  not** — no `version:` input anywhere, no `[tool.uv] required-version`, no `uv.toml`. With no
  version resolvable from the checkout the action fell back to fetching a remote version
  manifest on every job, so every workflow carried a network dependency it did not need. When
  that fetch failed the job failed with `##[error]fetch failed`, a verdict with no relationship
  to the change under test — run 33214025855 died that way in 21 seconds. `release.yml` was
  among the call sites, so the resolved uv could also change between publishes with no
  repository change at all. Fixed by adding `[tool.uv] required-version = "==0.12.7"` to
  `pyproject.toml`: `setup-uv`'s zero-input workspace scan reads exactly `uv.toml` then
  `pyproject.toml`, so one line reaches all 25 call sites without editing any of them, and an
  **exact** specifier resolves through the action's `ExactVersionResolver` with no network call.
  `0.12.7` is what CI already resolved to via "falling back to latest", so the pin is
  behaviour-preserving rather than a silent upgrade or downgrade. The `==` form is load-bearing:
  a range such as `>=0.12.7` reads as pinned but still fetches the manifest. **Local impact:**
  uv reads `required-version` itself, so a contributor on a different uv now gets a fast,
  self-describing `Update uv by running uv self update 0.12.7` instead of silently diverging
  from CI — see `docs/local-dev-env.md`. A new `scripts/check_uv_pin.py`, wired into
  `make lint`, keeps the pin single-sourced: it fails the build if the pin is removed, loosened
  to a range, shadowed by a root `uv.toml`, or overridden by a per-call-site
  `version:`/`version-file:` input (which `setup-uv` resolves *ahead of* the workspace scan).
  Upgrading uv stays a deliberate one-line change. **Scope, stated precisely:** `setup-uv` has a
  second, independent manifest consumer — `downloadVersion` calls `getArtifact` unconditionally
  on a tool-cache miss to obtain the download URL — so one `raw.githubusercontent.com` fetch per
  job REMAINS, and this change does not by itself eliminate the `##[error]fetch failed` class.
  What it does eliminate is the version-resolution fetch and the non-determinism: verified on run
  33230196661, every job now logs `Found version for uv in .../pyproject.toml: 0.12.7` and
  `Successfully installed uv version 0.12.7` with no `Could not determine uv version` fallback.
  The residual fetch is tracked as bug `5caa-4b63-7ea2-4e71`, which needs a different mechanism
  (tool-cache seeding, or installing uv without the action).

- **A RELOCATED ticket store is now honoured by the reconciler, bridge, and snapshot paths.**
  `config.tracker_dir()` has always documented the store as relocatable — `REBAR_TRACKER_DIR`
  wins verbatim, and an absolute `tracker.dir` relocates the store (EV-3b) — but 13 shipped
  call sites composed `repo_root/.tickets-tracker` directly and ignored that configuration.
  The most visible symptom was `bridge_status` failing with *"cannot read local environment id
  from `<repo_root>/.tickets-tracker/.env-id`"* on a deployment whose store is elsewhere and
  demonstrably healthy. All 13 now resolve the store through the single seam.
  **No behaviour change for the default in-tree layout**, where the resolver returns exactly
  the path these sites used to compose.
  A new `make lint` gate (`scripts/check_tickets_boundary.py`) rejects new store-path
  composition so the drained sites cannot quietly regrow; sanction an intentional
  boundary-crossing path with `# tickets-boundary-ok: <reason>` (the reason is mandatory).

  **Operator note — a relocated deployment may hold an inert stale directory.** Before this
  fix, the MUTATING reconciler paths (`BRIDGE_ALERT` publication and the bridge sync mapping)
  wrote under `<repo_root>/.tickets-tracker` even when the store lived elsewhere. This release
  performs **no migration**: after the fix nothing reads that location, so any residue is inert
  rather than divergent, and deleting a directory rebar no longer interprets is left to you.
  If your store is relocated, check for a stale `<repo_root>/.tickets-tracker/` and remove it
  once `bridge_status` reports healthy.

### Added

- **Config-driven reconciler mapping seam (`[tool.rebar.mapping]`).** The reconciler's
  rebar↔Jira vocabulary translation (statuses, issue types, link types, priorities, hierarchy
  ranks, and create-time defaults) is now an optional config overlay on top of the built-in
  literals, resolved per Jira project KEY through a three-layer per-key merge. The mapping
  engine is provider-neutral (the reuse seam for future targets such as Linear / GitLab /
  GitHub Issues) and a read-only `rebar bridge suggest-mapping <PROJECT>` probe seeds a
  `[mapping]` block from a live Jira project. **Additive and backward-compatible: an absent
  `[tool.rebar.mapping]` section reproduces today's hardcoded behavior EXACTLY, there is no
  store migration, and rollback is simply deleting the section.** See
  [docs/mapping-seam.md](docs/mapping-seam.md) and
  [docs/config.md](docs/config.md#provider-neutral-reconciler-mapping-mapping).

### Changed

- **BREAKING (pre-1.0) — an unreadable config now ERRORS gate-bearing operations instead of
  fail-OPENing the opt-in `verify.*` gates.** When `rebar.toml` is present but cannot be read
  (malformed TOML, invalid values), claim/transition/close now fail loudly with a
  `ConfigError` naming the gate, the ticket, and the chained parse fault — previously the
  gates silently resolved to their configured defaults and the operation proceeded
  unverified. Applies to the CLI (clean `Error:` line, exit 1) and the library
  (`rebar.claim` / `rebar.transition` raise `ConfigError`, now re-exported as
  `rebar.ConfigError`). Operator ruling on ticket 39f8-ae7c: "Unreadable config should
  result in an error." Fix the config file, then retry the operation.

- **BREAKING (pre-1.0) — the MCP gate resolvers now ERROR on an unreadable config instead of
  silently resolving to a fallback.** `config.mcp_readonly()` no longer falls back to
  read-only and `config.mcp_gate()` no longer returns a caller-chosen default (its `fail`
  keyword is removed) when the config cannot be read — both raise `ConfigError` naming the
  gate, chained from the parse fault, per the same 39f8-ae7c operator ruling. Over MCP the
  fault reaches the client as a structured error envelope with the new `config_unreadable`
  code (added to `rebar.KNOWN_ERROR_CODES`), so a broken config is distinguishable from a
  deliberate read-only/off policy. The safe capability posture is unchanged in effect — a
  broken config still never enables writes, LLM calls, or Jira sync — but the fault now
  surfaces to be fixed instead of masquerading as configuration.

- **BREAKING (pre-1.0) — library `transition` now accepts only the reason-carrying force
  shape shared with `claim`.** Pass `force="<reason>"` to bypass whichever lifecycle gate the
  operation reaches; `None` alone means no bypass, and an explicitly empty string records
  `(no reason given)`. The former boolean `force=True` and close-only
  `force_close="<reason>"` library spellings have been removed and now raise `TypeError`
  naming `force="<explicit reason>"`. The free-text `reason` parameter remains only an
  administrative close reason. Automated NDJSON replay remains an ordinary close and does not
  gain authority to bypass a target repository's completion-verification gate.

- **BREAKING (pre-1.0) — hosted OpenAI no longer supports the explicit `openai-chat`
  fallback.** Hosted `openai-chat:gpt-…` and `model_provider = "openai-chat"` now resolve to
  `openai-responses:gpt-…` with no deprecation-warning path. Custom OpenAI-compatible
  `base_url`/slot `endpoint` configurations still use Chat Completions because support for
  `/v1/responses` on arbitrary compatible servers is unknown.

## [0.12.0] - 2026-08-15

### Removed

- **Closing a ticket no longer compacts it.** Compaction was the store's longest write-lock
  holder and it ran inline on every close: measured on the rebar store, one close held the ONE
  tickets write lock for 13m53s and three others the same hour held ~2.5 min each, starving
  every concurrent writer. Compaction is optional housekeeping — an unfolded event log is
  completely valid and the reducer replays it — so the trigger is removed and a close now ends
  after its STATUS write, signing, the force-close audit comment and scratch cleanup. Its lock
  holds are the short per-append acquisitions. `rebar compact <id>` still folds on demand and
  is unchanged; the standing trigger becomes an out-of-band sweep that runs in a dedicated
  clone of the `tickets` branch. The only user-visible effect is that ticket event logs stay
  unfolded for longer — larger on disk, identical in meaning. See `docs/concurrency.md` §I9b.

- **BREAKING — `purge-bridge` is removed with no compatibility alias.** Invoking it follows the
  standard unknown-subcommand path and exits non-zero. No persisted data format changes.

- **BREAKING (pre-1.0)** — the deprecated `jira.resolved_statuses` and
  `reconciler.resolved_statuses` config keys (and their auto-derived
  `REBAR_JIRA_RESOLVED_STATUSES` / `REBAR_RECONCILER_RESOLVED_STATUSES` env twins) are
  removed and tombstoned **warn-class**: a config still carrying one keeps loading, logs
  that the key was removed, and drops it. There is no replacement — resolved/unresolved
  discrimination is outbound-owned (ADR 0028), so just delete the line. Operator-approved
  early removal (pre-1.0 pass #3), 2026-08-12.

- **BREAKING (pre-1.0)** — the bare `REBAR_LLM_MODEL` environment variable is removed
  and tombstoned: setting it now fails loud with a migration error instead of being
  ignored. Use the `[tool.rebar.llm.model_classes]` slots, the per-class
  `REBAR_LLM_<CLASS>_MODEL` variables, or the top-level `[tool.rebar.llm].model` key —
  which is NOT removed and still resolves CLI > config > default. Operator-approved
  early removal (pre-1.0 pass #3), 2026-08-12.

- **BREAKING (pre-1.0)** — the single-pass review operation's three public entry
  points are removed: the CLI verb `rebar review`, the library function
  `rebar.llm.review_ticket()`, and the MCP `review_ticket` tool. v0.11.0 turned the
  CLI verb into a forwarding shim and deprecated the library/MCP surfaces; this
  deletes them. Use `rebar review-plan` / `rebar.llm.review_plan()` / the
  `review_plan` MCP tool. Operator-approved early removal (pre-1.0 pass #3),
  2026-08-12.

- **BREAKING (pre-1.0)** — `rebar init` no longer strips the retired
  `.reconciler-* merge=ours` line from an already-committed `.gitattributes`; the
  ensure-unit is create-only. A clone last swept before 2026-07-05 must strip the
  line by hand.

### Added

- **Canonical bridge maintenance vocabulary:** use `rebar bridge fsck`,
  `rebar bridge check-access`, and `rebar bridge setup` for mapping audits, the live Jira
  capability round-trip, and interactive onboarding. The historical `bridge-fsck`,
  `bridge-probe`, and `jira-onboard` spellings remain fully functional compatibility aliases
  routed through the same implementations, output, state changes, and exit policy.

- **Canonical reconciler status:** `rebar bridge status` reads the durable last-pass, pause, and
  live-lease refs and reports `PAUSED`, `RUNNING`, `FOREIGN`, `FAILED`, `STALE`, `HEALTHY`, or
  `NEVER_RUN`. Age-based staleness is opt-in with `--max-age`; `rebar bridge-status` is retained
  as a hidden compatibility alias. The heartbeat canary now uses this status by default.

### Changed

- **Documentation-only Gerrit changes use a focused Verify lane.** Changes limited to the
  documentation allowlist retain generated-reference drift, link, index, quickstart, policy,
  hygiene, and documentation-contract checks without occupying the full OS/Python, mutation,
  packaging, and integration runner graph. Any code, configuration, workflow, dependency, test,
  unknown, or malformed path still fails closed to the complete Verify suite.

- **Portable scheduled bridge runner:** GitHub Actions, Jenkins, and GitLab now delegate profile
  routing, pause handling, strict ticket delivery, and canonical provider exits to one
  installed `rebar bridge run` command backed by the public `rebar.bridge_run` API. The pip
  wheel, Homebrew formula virtualenv, and MCP Registry/uvx install the same Python core.
  Scheduled profiles use `--profile` in the CLI and `profile=` in library/MCP calls.
  Noun-based `rebar bridge preview` / `bridge sync` remain primary; direct legacy reconcile
  entrypoints retain their existing defaults and 3/4/75 compatibility sentinels.

- **`bridge fsck` now audits real offline bridge state.** Its JSON result contains
  `unknown_event_types`, `binding_drift`, and `store_integrity`; the permanently empty
  SYNC-derived `orphaned`, `duplicates`, and `stale` keys are removed. Binding-index
  inconsistencies exit 1, while Git/ref scan failures exit 2 instead of looking clean.
  `rebar bridge-fsck` remains an identical compatibility entrypoint.

- **`reconcile-bridge-canary.yml` is a client-copied template.** Existing client copies keep
  their current delivery loop until maintainers opt in by refreshing that workflow from this
  release; refresh it deliberately rather than assuming repository updates modify copied files.

- **BREAKING — the `[eval]` extra is removed, and with it the unused `inspect-ai`
  dependency.** rebar never imported `inspect_ai`: prompt evaluation
  (`rebar prompt eval`, spec validation, grader discipline, coverage, JUnit) is
  stdlib-only and needs no extra, and the *live* run goes through rebar's own solver
  on the pydantic-ai runtime — the `[agents]` extra. The `[eval]` extra therefore
  installed a dependency nothing used. `pip install 'nava-rebar[eval]'` now warns that
  the extra does not exist and installs the base package; **install
  `nava-rebar[agents]` instead** for live prompt evals. Dropping it also removes the
  two `[tool.uv] conflicts` entries (`eval` vs `dev`, `eval` vs `bedrock`) that existed
  to fork the resolution around inspect-ai's `boto3 < 1.40.62` diamond — so `dev` and
  `bedrock` are now co-installable — and the `override-dependencies = ["click>=8.3.3"]`
  entry that existed only to widen inspect-ai's stale `click < 8.2.2` cap. The lock now
  reaches click 8.4.2 on its own and `pip-audit` is clean without any override.

- **BREAKING — `rebar transition --force-close=<reason>` is now `rebar transition
  --force[=<reason>]`.** `transition` previously carried two different force flags — a
  valueless `--force` that bypassed the start-work (plan-review) gate and a separate
  `--force-close=<reason>` that bypassed the completion-verification / signature gate on a
  close — while `claim` spelled its single escape hatch `--force[=<reason>]`. They are now
  one flag, spelled the same as `claim`'s, that bypasses whichever gate the transition
  would hit: the start-work gate on `open -> in_progress`, the close gate on `-> closed`.
  A bare `--force` on a close records `(no reason given)`, matching `claim`.

  This is a **clean break with no deprecation alias**: `--force-close` now exits non-zero
  with an error naming `--force`, so a stale caller fails loudly instead of silently
  closing through the gate it meant to bypass. Update any script or automation that passes
  `--force-close`; there is no compatibility shim to fall back on. To back the change out
  entirely, revert the rename commit.

  At the time of the 0.12.0 CLI rename, `--force --reason="x"` on `open -> in_progress` still
  recorded `x` as the audit note and the library kwarg
  `rebar.transition(..., force_close=...)` kept its name. The Unreleased normalization above
  supersedes both library behaviors. The unresolved-children close guard remains
  unbypassable, and 0.12.0 exposed no force bypass over MCP.

## [0.11.0] - 2026-08-06

A feature release centered on first-class Amazon Bedrock support, a substantially
more complete Jira Data Center backend, and stronger review, completion, and
git-backed store reliability. This release also raises the Git floor to 2.38 and
retires the deprecated single-pass `rebar review` operation.

### Added

- **First-class Amazon Bedrock provider** with provider-registry resolution,
  per-model-class configuration and fallback chains, custom endpoints, prompt-cache
  accounting, cost reporting, signed provider provenance, and a live parity harness.
- **Jira Data Center backend maturity** across identities, rich text, comments,
  labels, priorities, assignees, status transitions, links, Epic Link/sub-task
  parentage, pagination, rate limits, and retry semantics, backed by Dockerized live
  contract and capability-map CI.
- **Project-defined review policy** for plan and code review, including criteria
  routing, engine-layer enforcement, measurement provenance, phase-boundary checks,
  acceptance-criteria satisfiability, and explicit no-file-impact declarations.
- `rebar edit --review` and `rebar claim --review` shortcuts for the
  edit/review/claim workflow.
- `rebar doctor` as the supported link/store diagnostic surface, including safe
  repair dry-runs and stale-channel repair.
- Search matching by ticket ID, alias, and Jira key.
- A `bug_trends` metrics lens covering five bug-health dimensions.
- Provider-parity and structured-output measurement harnesses for live LLM
  compatibility checks.

### Changed

- Plan review now fast-fails deterministic blocks, reviews only claimable work,
  batches overlap checks, reuses cacheable prompt prefixes, and handles review-time
  ticket edits and tool-call exhaustion more predictably.
- LLM work is routed through explicit `trivial`, `standard`, and `frontier` model
  classes; local development and the hosted review bot now use Bedrock by default.
- Jira Cloud and Data Center share a typed backend contract with explicit capability
  and exception boundaries instead of adapter-specific assumptions.
- The git-backed ticket store now uses normalized bridge baselines, a rotation
  sidecar, narrowed snapshots, and a fail-closed store epoch around the one-time
  tickets-history reclaim.
- Development installs are reproducible from the committed `uv.lock`.
- The minimum supported Git version is now **2.38**.
- The release smoke probe is Python-only; the transitional `probe-rebar.sh` shim was
  removed.
- The deprecated single-pass `rebar review` operation was retired in favor of
  `review-plan`, `verify-completion`, and `review-code`.

### Fixed

- Jira Data Center labeling, TLS validation, credential failures, `Retry-After`
  handling, stalled pagination, Epic Link clearing, sub-task reparenting, and
  project-move behavior now fail explicitly and converge correctly.
- Jira index lag no longer creates duplicate issues, and inbound/outbound parent,
  assignee, link-deletion, status, and comment synchronization is more symmetric.
- Ticket-store synchronization now tolerates local Git lock contention, uses explicit
  refspecs, bounds network Git calls, detects stalled fetches, and classifies
  SIGPIPE-affected push failures reliably.
- Completion and close gates now reject unchecked acceptance criteria, fail closed
  when epic bug screening fails, preserve actionable recovery diagnostics, and avoid
  signing unpinned local-source reviews.
- Completion verification now curbs repeated read-only evidence searches, and FAIL
  remediation names the exact `[operator-attested]` tag required for external evidence.
- Plan-review attestations survive intended registry transitions while still
  invalidating on material type, dependency, or configuration changes.
- LLM routing preserves the selected provider across escalation and recovery,
  respects per-class endpoints, clamps token budgets to the active model, and avoids
  empty verifier requests and incompatible structured-output grammars.
- Code-health analyzers report unavailable data honestly instead of returning
  synthetic zeroes.

### Security

- Resolved `PYSEC-2026-2132` through the locked Click override.
- Jira Cloud URLs must use HTTPS, matching the Data Center transport policy.
- Webhook and rerun tokens are no longer emitted in logs.
- Release and Bedrock CI permissions, action pins, and publishing provenance checks
  were tightened.

## [0.10.1] - 2026-07-24

A maintenance release: plan-review and completion-close gate hardening, a set of
deterministic code-health analyzers behind `rebar metrics`, and continued build-out
of the reconciler **Backend** port — no breaking changes.

### Added
- **Code-health analyzers** — deterministic `scc` LOC and `lizard` complexity
  analyzers plus a shared `jscpd` duplication analyzer, exposed through a new
  code-health configuration surface (the code-health `rebar metrics` lens now draws
  on these; size metrics moved onto the `scc` analyzer).
- The completion close gate can target a **specific commit** via `--ref` (rather than
  only the worktree HEAD).

### Changed
- **Plan-review gate:** `review-plan` now fast-fails non-claimable tickets before
  spending an LLM call; a genuine `BLOCK` outranks a prerequisite-pass
  `INDETERMINATE`; reviews are invalidated consistently on dependency drift; signing
  is scoped to the subject + related material (not the whole store); `[rebar:<id>]`
  citation edge-verification honors inherited hierarchy links; and the close gate
  accepts an unchanged planning-phase attestation.
- Plan-review deep-links default to the **hosted guide** instead of a consumer
  `file://` path, and the commit-trailer format + CLI-served guides now resolve for
  repo-less clients.
- **Completion close** is pinned to a single HEAD sha across verify/sign to close a
  TOCTOU window.
- The store is mounted at **one central CLI gate**, so no command can skip it.
- **Reconciler `Backend` port** build-out: scalar port surface
  (`project`/`query_project`/`assert_env_ready`), inbound absence probe behind a
  `SupportsAbsenceProbe` capability, canonical-shape outbound field diff and link diff
  (`SupportsLinks`), a rich-text codec + comment limits behind the port, and the vendor
  mapper injected into the differ/apply core.
- The optional-`metrics` dependency boundary is enforced, keeping the core install
  lean.

### Fixed
- The residual unsigned-close warning now points at a working recovery path.
- Completion-verifier test discovery is reliable and diagnosable.
- Prerequisite-indeterminate logs report the model that actually ran.

### Ops
- The review bot reads per-run `_usage` and emits token counts to CloudWatch, and the
  two weekly live-LLM jobs record their token usage.

## [0.10.0] - 2026-07-23

A feature release: a pluggable reconciler **Backend port** that decouples the Jira
adapter behind a typed interface, capability-tiered model delegation and phased
plan-review attestations for the agent workflow, a ChatGPT connector-limited agent
guide, plus a broad round of plan-review/code-review gate hardening and fixes.

### Added
- **Reconciler `Backend` port** — the reconciler's transport is now a typed `Backend`
  interface (`JiraBackend` + a contract suite), selected through a new
  `reconciler.backend` config + registry (`select_backend`), with the Jira vendor
  modules relocated under `adapters/jira/`. This lays the groundwork for non-Jira
  backends (pinned in ADR 0035 §(d)).
- **Capability-tiered model delegation** in the `rebar-implement` skill — steps run on
  model tiers matched to their difficulty.
- **Phased plan-review attestations** — plan-review attestations are now phase-aware.
- **ChatGPT connector-limited agent guide** (`docs/chatgpt-agent-guide.md`) for
  checkout-less / tracker-less sessions.
- Plan-review **attestation currency**: `rebar review-plan --status` (read-only, no
  LLM) plus a reuse marker to detect a stale attestation cheaply.
- `rebar list` accepts **comma-separated type filters** (e.g. `--type=bug,task`).
- Local **code-review preview** (`rebar review-code`) surfaced as discoverably as
  `rebar review-plan`.
- Author guides are **packaged in-package**, so `rebar explain` works from any install.

### Changed
- `--force` gate-bypass is generalized to **all** start-work gates (not just
  plan-review), with documented semantics in the user/agent guidance.
- Rebar's own project configuration now enforces plan-review material pins and
  requires a current execution-phase plan review before close. The reusable
  configuration defaults remain opt-in for downstream projects; legacy unpinned
  attestations remain accepted.
- Plan-review hardening: honors verified `[rebar:<id>]` cross-ticket citations, binds
  reviews to the related ticket material, requires a current review when configured,
  refuses to pass on an incompletely-loaded ticket hierarchy, evaluates direct
  prerequisites independently, and pins the Pass-2 verifier to greedy sampling
  (temperature 0).
- `rebar metrics` defaults to a real date window; completion checks are skipped for
  replaced bugs.
- Code review's `--base` uses a merge-base (three-dot) diff.
- Contributor DCO sign-off guidance is identity-neutral.

### Fixed
- Prerequisite coverage now **accepts runner envelope metadata** (`runner`, `model`,
  `trace_id`, `_usage`) instead of failing validation and forcing an INDETERMINATE
  verdict that blocked the claim.
- Metric-plan routing regression pinned; `T8` applicability made deterministic.
- The overlap perf gate is robust to CI runner contention.

### Security
- Bump **zizmor 1.27.0 → 1.28.0** (a yanked release / GHSA advisory) and ruff
  0.15.19 → 0.15.22.

## [0.9.1] - 2026-07-19

### Added

- Homebrew tap now auto-bumps its formula from PyPI (scheduled self-bump
  workflow; no stored token).

### Fixed

- Plan-review finding discovery now de-duplicates findings and drops
  contentless ones (empty body / no checklist item).

## [0.9.0] - 2026-07-20

A feature release: passive delivery / agent-process metrics behind a new
`rebar metrics` command, a `caused_by` link relation and structured bug-close
classification, an absolute module-size cap, broader plan-review / code-review
gate coverage, and a batch of reconciler and CI durability fixes.

### Added

- `rebar metrics` — a passive analytics command spanning the agent-process,
  code-health, delivery, and gate-economics lenses, with a metric registry,
  per-figure source/confidence labels, and an explicit `unavailable` state
  (the metrics-instrumentation epic). Snapshots persist to a going-forward
  NDJSON store, and readers derive figures from events, git history, gate
  sidecars, GitHub-Actions coverage/CI-health, and mined Claude transcripts.
  The command is discoverable and janitor-aware.
- Backfill historical metrics for the new lenses: complexity/clone snapshots at
  sampled commits, and bug `close_class` / `caused_by` over already-closed bugs.
- `caused_by` as a 7th link relation, including blame-derived `caused_by`
  inference on bug close and an explicit `--caused-by` flag.
- Persist a structured bug-close `--class` enum (`close_class`), backfilled from
  structural signals and threaded through the CLI, MCP tools, NDJSON import, and
  the canary's close-on-recovery steps.
- Extract the shared CI gate + test suite into a reusable GitHub Actions
  workflow.
- Provenance & identity: record an immutable genesis channel for the CLI/MCP/
  Python entry points, attribute Jira/import-created tickets (inferring legacy
  Jira authorship), and preserve provenance through SNAPSHOT compaction.
- Plan-review gate — new advisory criteria and grounding probes: an
  asserted-capability grounding probe (R1), a decomposition-shape container
  criterion (R3), a necessity/no-op probe plus a lightweight bug-review tier
  (R4), an `asserted_capability_confirmed` kernel sub-answer (R5), and an
  operator-attested-evidence DET lint. Plan-review portability checks (finder,
  scoped coaching move, eval corpus) added.
- A standing per-criterion gate-effectiveness recorder (sidecar-native,
  zero-LLM).

### Changed

- Module-size policy: replace the allowlist with an absolute, admin-locked cap
  and delete the drained allowlist mechanism entirely.
- Adopt the cross-vendor `AGENTS.md` standard: add a canonical `AGENTS.md`,
  reduce `CLAUDE.md` to a one-line `@AGENTS.md` pointer, re-home ticket-model
  guidance into `docs/`, ship a client `AGENTS.md` template with a drift-guard
  test, and rename the example-skills namespace `claude-skills` -> `agent-skills`.
- Centralize event signing in a shared `finalize_event` seam with `enforce_since`
  grandfathering, and run `verify-identity` as a pre-merge whole-store gate.
- Unify Jira link-direction handling behind a single source of truth.
- Extend plan-review's rising-floor convergence to the code-drift re-review axis
  so re-reviews hold the same quality floor as first-pass reviews.
- Harden the durable write path against concurrent-git faults; make reconciler
  infra exceptions fail-silent no-ops via `REBAR_RECONCILER_FAIL_SILENT_NOOP`
  and gate an ERROR sidecar on the infra-exception path.

### Fixed

- Fix inverted/reversed Jira link directions on both inbound and outbound sync
  (honor `swap_endpoints`), with inverse-aware dedup.
- Paginate `get_comments` so comment sync stops re-posting existing comments.
- Fail loud on swallowed comment drops, with comments/labels canary coverage.
- Fix a reconcile-bridge startup failure by dropping an illegal runner context
  from the job-level env.
- Route the enrich-queue prune through the locked `delete_events` path.
- Stop false-rejecting sidecars and null-commit ledger entries; make ledger
  resolution robust and skip archived entries rather than fail-closing authentic
  events.
- Retry transient "could not parse HEAD" errors on store git commits, and run
  git maintenance in the foreground on the tickets worktree.
- Harden ticket-id / prompt-id path handling and drop the stack trace from a
  `/rerun` 502.
- Repair `probe-rebar.sh` drift from the current CLI contract and wire it into
  golden-path CI so future drift is caught automatically.
- Harden the live-E2E index-visibility poll against Jira propagation lag with
  backoff, removing a source of flaky runs.
- Re-materialize the host certbot renewal timer on autodeploy so certificate
  renewal survives redeploys.
- Harden fsck Check 3's stale `index.lock` reclaim against a TOCTOU race, and
  stop bridge-fsck flagging a legitimate window-absence as a dangling reference.
- Normalize the CI run conclusion before casting the `Verified` vote (so a
  neutral/skipped conclusion is no longer misread as a failure), and close the
  `Verified`-gate required-check-drift hole with a parity guard.
- Lock and pathspec-scope the purge-bridge deletion commit, and add a prune
  concurrency regression test.
- Cross-check reducer cache validity against on-disk attestations so a stale
  cache can no longer mask an attestation change.
- Isolate config tests under a temporary XDG home so fixtures no longer leak the
  developer's real user config.
- Guard `int(git rev-list --count …)` parsing in test helpers against empty
  output.
- Set `ignore_changes` on the assume-role policy to end phantom Terraform drift.

### Documentation

- Add author-facing plan-review and code-review on-ramp docs.
- Document post-merge close discipline — fast-forward your worktree, not the
  shared checkout.
- Document the autodeploy self-update coverage decision.
- Remove a dead `../session-logs` relative link in `docs/concurrency.md`.

## [0.8.0] - 2026-07-16

### Breaking

- Reject whitespace-only ticket titles on create/edit across the CLI, library, and
  MCP surfaces: a title is now rejected when it is empty after trimming whitespace
  (previously a spaces/tabs/newlines-only title was silently accepted). Titles with
  real content but surrounding whitespace are still accepted and stored verbatim (no
  trimming), and Unicode is preserved as-is (the pre-existing `→`→`->` normalization is
  unchanged). Back-out/expand-contract note: this is a caller-visible tightening; a
  pre-merge store scan confirmed zero existing whitespace-only titles, so no data
  migration is required, and reverting the guard restores the prior accept-all behavior
  with no stored-data change.
- Remove the 8 scheduled back-compat shims (DE7)
- Remove the remaining user-facing deprecations (DE7)

### Added

- Restrict Submit to Contributors + Administrators
- Code-review arm for the eval harness (epic sure-foyer-aroma)
- Base linter-deference + Pass-2 verifier discipline (epic sure-foyer-aroma)
- Content-triggered overlays + deletion-impact overlay (epic sure-foyer-aroma)
- Scope-intent overlay — diff vs union of trailer tickets (epic sure-foyer-aroma)
- Deterministic conflict-marker DET detector (epic sure-foyer-aroma)
- File-follow-on-ticket + delete-bad-test coach moves (epic sure-foyer-aroma)
- FP-ledger + grounding-health/approach-viability telemetry (epic sure-foyer-aroma)
- Tests overlay — full behavioral-testing standard (epic sure-foyer-aroma)
- Security overlay — dso red-team recall + FP-guards (epic sure-foyer-aroma)
- Performance overlay — AI-advantaged recall + bright-line FP-guards (epic sure-foyer-aroma)
- Api-compat overlay — asymmetric-change/producer-consumer recall + FP-guards (epic sure-foyer-aroma)
- Db-migrations overlay — symmetry/destructive/lock recall + FP-guards (epic sure-foyer-aroma)
- Llm-prompts overlay — contract-vs-prose recall + FP-guards (epic sure-foyer-aroma)
- Flag unmapped Jira workflow statuses at snapshot-build time
- Add first-class `idea` status excluded from dispatch
- Bypass completion/attestation close gates for idea->closed
- Exempt `idea` tickets from noisy health checks
- Add atomic `idea` command across CLI/MCP/library
- Map local `idea` status to Jira `IDEA`
- Pass-2 vocabulary expansion — two graded sub-answers + restatement gloss (WS1)
- Reviewer self-calibration anchors — scale + one-way blast_radius (WS6)
- Prompt-hardening pass — shared stance preamble + affirmative-framing guard (WS7)
- E4 confident-assertion + scope-exclusion scan, and a 1-TURN hedge finder (WS2)
- LLM-routed enumeration overlays — prohibition scan (T13) + CI-trigger audit (T14) (WS3)
- Removal-rationale criterion — Chesterton's Fence, the removal-side dual of A1 (WS11)
- DET-floor verify-command lint — the G-3a defect blacklist (WS4)
- Rubric enrichments across seven criteria (prompt-only) (WS5)
- Coach MOVE_REGISTRY — foundation/enhancement move + sharpened verification move (WS8)
- Sidecar cohort field for chunk-contamination analysis (WS9)
- Docs invariants + rebar explain + registry-derived criteria guide (WS10)
- Adjective-adjective-animal aliases for new tickets
- Deterministic G5 decomposition signal (store child count)
- Severity-first MAX impact model + per-gate impact_fn dispatch
- Pass-1 finding-memory (recall) via post-Pass-1 candidates
- Recalibrate criteria thresholds + postures (approved set)
- Two-lane tier-tagged MAX impact model (impact_code)
- Suppress docs + llm-prompts advisory nits (nit_suppressed)
- Durable code_review artifact type + reviewbot data capture
- Permissive rollout + impact-model versioning + calibration tooling
- --repair remediation to drive the live store to fsck-zero (A3)
- Unify session-id resolution into one shared resolver (+FORCE_CLOSE fix)
- Record claiming session id on open->in_progress
- Expose claimed_session on read surface + compaction safety
- Multi-harness resolver + harness/remote provenance
- Claude Code SessionStart shim -> REBAR_SESSION_ID + AI_AGENT
- Codex + Cursor capture shims (harness tag)
- Generated REBAR_* env-var registry with a CI drift gate
- Baseline consumer swap (convergence rollout Phase-3, flag-gated)
- Enable public GitHub intake — Issues on, forms, SUPPORT rework

### Fixed

- Rotate off a fingerprint-less pointer (defensive rotation)
- Derive code root from tracker in file_impact commit precheck
- Accurate StatusMappingError message for unmapped Jira status
- Remove fragile import-cycle guard; make execution-mode test env-independent
- Isolate per-mutation failures; fail loud instead of aborting the pass
- Make Contributors membership reproducible (default RebarBotNava)
- Recover the sole dict from a list-shaped structured output
- Force-close recovery alert tickets to bypass the completion-verification gate
- Round-trip resolve, not a brittle alias-length bound
- Surface git's real stderr on create-path commit failure
- Recover prose-wrapped JSON when a non-JSON brace precedes it
- Skip closed changes in the backfill reconciler
- Persist review-bot state across container recreation
- Treat 409 "change is closed" as terminal + document c943 disposition
- Escape </script> breakout in inline-script interpolation
- Give verify_attested_commit an explicit repo_root (F14)
- Retire folded events to *.retired instead of deleting (I1)
- Snapshot-horizon-safe replay — fold horizon + rebuild-on-stray (RC2b)
- 429 rate-limit backoff in the ACLI subprocess retry loop (C4)
- Observe outbound both-sides conflicts + allowlist drops (C1/C3)
- Reconciler in-flight guard + folded-snapshot retire + A3 close-out tests
- Finish Jira hard-delete -> outbound re-create (C2)
- Wire REBAR_LLM_TIMEOUT into the runner so it bounds each call
- Reset the index on event-append failure; check git add before commit
- Don't reopen closed tickets on an unmapped inbound Jira status
- Take the write lock around the push-retry merge; log async spawn failures
- Surface lost cross-clone claims (derived fork record + exit-10)
- Wire dead safety machinery — HTTP retry, write-ahead, lease steal

### Changed

- Publish detector_matches as public criteria-model API
- Give the completion gate a public context-builder contract
- Delete dead _call_with_backoff retry decoy
- Delete dead _dispatch_mutation + coverage-only table
- Flatten replay_events dispatch into a lookup table
- Fold produce_code_review_verdict into a request object
- Split at-cap engine_b.py and _llm_commands.py along seams
- Funnel list_tickets surfaces through TicketQuery
- Thin the __init__.py god-facade behind _lib_* submodules
- Decompose six oversized functions into phase helpers
- Split invariant detection from remediation (SC4)
- Centralize reconciler git ops behind git_adapter (SC5/6/7)
- Complete + centralize the deprecation registry (S1)
- Introduce the adapters/jira vendor seam + ADR (O5+S5)
- Remove dead internal signature-compat shims (pre-beta cleanup)
- Lift binding-store paths to git_adapter constants + characterize (SC5)
- Extract replay engine to _replay.py (module-size seam)

### Documentation

- Clarify Requirements, prune CLI ref, move signing to its own doc
- Sync version marker, event-schema ref, README CLI; add version guard
- Update help text off the removed --no-sync alias to --no-pull (DE7)
- Fix audit-flagged drift in concurrency.md and architecture.md
- Add PyPI version, Python versions, license, and CI status badges
- Add audience-grouped docs index and a day-to-day user guide
- Publish GOVERNANCE.md, MAINTAINERS.md, role-based CoC contact

### Other

- Merge "fix(session-log): rotate off a fingerprint-less pointer (defensive rotation)" into main
- Merge "ci(release): harden mcp_registry against PyPI-propagation race" into main
- Merge "fix(close): derive code root from tracker in file_impact commit precheck" into main
- Merge changes Ie7190cde,I9a30fbc7 into main
- Merge "fix(reconciler): isolate per-mutation failures; fail loud instead of aborting the pass" into main
- Merge changes I85667852,I2fa270f4,Ie0e3519b,I776e594f,If53b7868, ... into main
- Merge "feat(reconciler): flag unmapped Jira workflow statuses at snapshot-build time" into main
- Merge "fix(gerrit): make Contributors membership reproducible (default RebarBotNava)" into main
- Merge changes I1711dc21,Iec0742f0,Iac9b2196,I21be3b93,Ic7bd173d into main
- Merge "feat(alias): adjective-adjective-animal aliases for new tickets" into main
- Merge "fix(test): round-trip resolve, not a brittle alias-length bound" into main
- Merge "fix(review-bot): skip closed changes in the backfill reconciler" into main
- Merge "fix(infra): persist review-bot state across container recreation" into main
- Merge changes I430b9507,I8593a80c into main
- Merge changes I72028311,If1b9d36d,I809bec64,Ie801f431,Ia667047c, ... into main

## [0.7.1] - 2026-07-06

### Added

- Warn on stderr when a non-bug ticket lacks file_impact
- Fail completion verification fast when file_impact ticket lacks a referencing commit

### Fixed

- Give gh repo context in github_release job (GH_REPO)
- Stop reconciler corrupting the store; fix fsck ORPHAN false positives
- Bypass a loopback Claude-Code payload proxy for rebar's LLM calls

### Other

- Merge "feat(create): warn on stderr when a non-bug ticket lacks file_impact" into main
- Merge "ci(release): auto-publish to MCP Registry via GitHub Actions OIDC" into main
- Merge "fix(runner): bypass a loopback Claude-Code payload proxy for rebar's LLM calls" into main
- Merge "feat(close): fail completion verification fast when file_impact ticket lacks a referencing commit" into main
- Release 0.7.1
- Merge "Release 0.7.1" into main

## [0.7.0] - 2026-07-06

### Added

- Additive, kind-keyed attestations (plan-review + completion coexist) — epic dark-acme-lumen (#98)
- REBAR_SESSION_ID precedence over ambient SESSION_ID (c1bf) (#101)
- Project-supplied criteria overlay — open vocabulary + .rebar/ overlay + cache isolation (ef7e) (#103)
- Agentic four-pass code-review capability + Gerrit LLM-Review voter (epic b744) (#102)
- Generalize DET-invariant scan consumer + per-criterion fail_mode, expose to plan-review (7f0d) (#106)
- Overlay-aware registry_version + built-in disable + disabled_builtins manifest (08af) (#107)
- Per-criterion eval runner + calibration view — rebar criteria eval (55b8) (#105)
- Live criterion preview endpoint + routing-overlay authoring (6e31) (#109)
- Unify plan-review + code-review into shared rebar.llm.criteria layer (5065) (#108)
- WS8 — wire GitHub-OAuth Gerrit auth end-to-end (epic b744) (#112)
- Explicit trust-boundary scope gate for the T5c security overlay (2e89)
- Deterministic public-exposure detectors in the security overlay (830a)
- Mirror-lock guardian + terraform reconcile (WS7 follow-ups 8ccf + a774)
- CI as a second Gerrit gate vote (Verified) via gerrit-to-platform (epic 1fa8)
- Delivered_now predicate for container completion-awareness
- Completion Pass-2 sub-call
- Pass-3 completion floor for container tickets (epic 66ac / story 6533)
- Activate the Verified two-vote gate (epic 1fa8 story S6)
- Uncertified child withholds certification, doesn't block closure
- Make the tickets sync remote configurable (sync.remote, default origin)
- CI Verified gate — require a resolvable rebar ticket in every commit
- Feature-branch flow — merge-carry copyCondition, submit-type pin, feature-branch ACLs
- Merge-change review path — auto-merge delta only, fail-closed
- Continuous auto-deploy + CI config-gate (make box reflect main)
- Box-adapt autodeploy to copy-based /opt/rebar (mirror clone + rsync)
- Emit merge_change_409_guard event at the merge 409-guard site
- Convergence foundation — pure classify() matrix, per-binding baseline, breaker, census
- ADR 0029 archived/deleted→Done round-trip suppression (444d prereq)
- Bridge-fsck offline binding-drift audit — the classifier's 2nd consumer (8de5)
- Bridge-fsck classifier-driven snapshot arm — dangling + unbound_jira (8de5)
- Fold reconcile_check onto classify() + wire canary binding-drift alerting (8de5)
- Binding-store-driven acting walk heals drift classes A + C
- Level-triggered ADOPT heals drift class B (Jira-native issues)
- Baseline dual-write shadow — convergence rollout Phase 1 (7d23)
- Record the rebar version+SHA that certified an attestation
- T2 semantic-resolution seam — refute_semantic dispatch, default-off
- Pyright T2 semantic backend — confirm-only diagnostics resolver
- Annotate MCP tools with behavior hints (ToolAnnotations)
- Typed public return contract — schema-derived TypedDicts (3a10)

### Fixed

- Resolve three open bugs — read-CLI space-form flags, stale probe, plan-review signature contract (#93)
- Key proportionate scrutiny on container/leaf, not ticket type (a278) (#110)
- Decouple criterion id from rubric filename — net-new project criteria are authorable (stew-kid-motif) (#111)
- Stop inbound differ clobbering local description with Jira-truncated body
- Tune G5 decomposition to coherence-primary; de-tautologize Pass-2 impact (#115)
- Run the four-pass gate in a snapshot session + install its scanners
- G2p Gerrit-3.x compat — compact change-id pin + replication.config copy
- Run the four-pass gate ATTESTED like plan review (code + ticket clone)
- Fetch origin/tickets from the mirror so the code-review agent has ticket access
- Close gate honors graph=False — verify the ticket's own criteria, not children
- Has_llm_steps detects LLM steps in v3 branch/loop/map arms
- Bump reducer-cache version so the attestations projection invalidates stale caches
- Name-check batch criteria `when` refs and honor patternProperties outputs
- Replace absolute wall-clock perf ceilings with load-independent relative metrics
- Skip live-Jira reconcile dry-run when no Jira project is configured
- Withhold certification on a child-enumeration read error
- Honor config-file mcp.readonly in the LLM runner's comment gate
- NO_SYNC truthy convention, verify_completion graph tri-state, test-fake signature
- Surface a fail-closed detector match as a named blocking finding
- Allowlist documented throwaway keys so docs edits aren't false-blocked
- Replicate feature/* to the mirror so g2p dispatches CI for feature-branch changes
- Noqa F811 on the impact re-export shadow (unbreak main lint; a425)
- Rebuild the code-review runner from the re-rooted cfg
- Reconcile-check reads .cache.json state, not nonexistent ticket.json
- Seed .env-id via `rebar init` so binding-drift alerts can be filed
- Hide monotonic managed_refs from default view so removed links don't read as live
- Correct server.json env contract + add CI drift-guard
- Reject invalid --status values instead of silently returning []
- Unwrap a single-element top-level array in validate_to
- Scale verify output cap + never silently zero-dispatch verify
- Auto-rotate current-log pointer per session
- Normalize live Jira snapshot before comparing
- Batch-criterion authoring writes the rubric at its raw id, not the routing overlay
- Map non-PASS + 0 findings + no gap to coverage-gap, not 'finding'
- Correct stale rebar.llm.eval import in prompt-eval workflow
- Treat an empty rebar-id: marker as unmarked (adoptable)

### Changed

- Break the reducer->_engine_support layering inversion
- Decompose transition_compute + split close path to a sibling
- Split build_server into per-cluster tool registrars
- Extract run_differs phase out of reconcile.py
- Extract the typed schema into _config_schema.py
- Hoist the _Handler class to a transport module
- Extract create_one/update_one to dispatch_one.py
- Consolidate the _git() subprocess wrappers into gitutil.run_git
- Consolidate _reap_process_group into a stdlib-only _proc leaf
- Consolidate inline atomic-writes into fsutil.atomic_write
- Consolidate the by-path lazy loaders into _loader.lazy_load
- Make rebar._reads a leaf and route llm reads off the facade (item 9.3)
- Collapse the list_tickets filter set into a TicketQuery dataclass
- Group the flat rebar.llm root into prompting/ and evals/ subpackages

### Documentation

- Correct stale forced-tool/disable-thinking rationale (#94)
- ADR 0007 — editing a prompt CONTRACT from the visual editor (5b9e) (#95)
- WS8 — Gerrit auth-hardening runbook (GitHub OAuth) + plugin provenance (epic b744) (#104)
- Gerrit contributor flow — CONTRIBUTING.md + CLAUDE.md workflow + README links
- Mirror-lock rollback — CONTRIBUTING.md escape-hatch link + runbook trigger/fast-unlock
- Summarize plan-review, completion-verifier, code-review gates
- Record the executed two-vote-gate E2E + SHA-pin setup-python (S5/S6/S3 evidence)
- Correct submit gate to the two-vote (LLM-Review + Verified) reality
- Fix stale references surfaced by codebase audit
- Document the stale reducer-cache workaround for mixed-build checkouts
- Correct child-closure gate semantics (post-30c2ef3ad)
- Document the feature-branch flow (S4)
- Commit S3 feature-branch CI validation evidence
- 0026-0029 — reconciler convergence invariants (merge base, binding lifecycle, absent≠deleted, status/echo)
- ADR 0030 — select pyright compiler-CLI diagnostics as v1 T2 backend
- Feature-branch lifetime policy, back-out runbook, cost/latency + inventory (S6)
- Commit the S6 live back-out evidence to the evidence doc
- Add GitHub community health files
- Correct run_workflow durability docstrings to true crash semantics
- Publish an API-stability matrix across the public surfaces (830d)
- Publish a scale-envelope doc with measured numbers (d063)
- Add a single-sourced README Quickstart (golden path) (3bf6)

### Other

- Add interactive `rebar jira-onboard` wizard (b5db-7433) (#96)
- Epic 7d43: convergent plan-edit re-review (full-run + deterministic rising floor) (#97)
- S1 (5bfc): Terraform base for AWS Gerrit PoC
- S2 (786b): deploy Gerrit + review-bot receiver behind nginx/TLS
- Use external named volumes for Gerrit site subdirs (down -v safe)
- S3 (a88d): rebar Gerrit project + LLM-Review label + submit requirement
- S4a (42ba): review-bot Gerrit identity + event plumbing
- Add smoke-check.sh (bot token dual-scope + events-log backfill)
- S5 (4e82): Gerrit->GitHub one-way replication (non-force, deploy key)
- Merge remote-tracking branch 'origin/main' into worktree-epic-d251
- S6-pre (dcea): GitHub mirror-lock pre-work (snapshot + ADR-0011)
- S4b (918c): review-bot receiver logic + LLM-Review voter + backfill
- Fold review NITs — redact URL-encoded token; document lock-dict PoC scope
- Manual /rerun + reconcile cursor + boto3 voter_errors + committed e2e
- Offline tests for get_patch base64 + XSSI-JSON decode paths
- S6 (075f): GitHub mirror-lock (deploy-key-only ruleset) + runbook
- Add mirror-mode cutover playbook to the runbook (toggles, banner, CONTRIBUTING)
- S8 (1628): client walkthrough — self-host Gerrit + rebar review-bot to gate GitHub
- S7 (3178): e2e proof + backup/restore drill + monitoring + runbooks
- Refresh observability.sh header to list all metric sections (S2/S5/S4b/S7)
- Rigorous e2e (==MAX + 3 terminal states + GitHub-appearance) + quiesced restore drill
- Document index/ + cache/ as rebuildable, excluded from restore archives
- Merge pull request #100 from navapbc/worktree-epic-d251
- Merge pull request #113 from navapbc/worktree-robust-sparking-sketch
- Merge remote-tracking branch 'origin/main' into worktree-effervescent-bouncing-robin
- Merge pull request #114 from navapbc/worktree-effervescent-bouncing-robin
- Merge "chore(module-size): allowlist plan_review/attest.py (821 LOC, pre-existing over-cap)" into main
- Merge "fix(code-review): run the four-pass gate in a snapshot session + install its scanners" into main
- Merge "fix(gerrit): g2p Gerrit-3.x compat — compact change-id pin + replication.config copy" into main
- Merge "fix(code-review): run the four-pass gate ATTESTED like plan review (code + ticket clone)" into main
- Merge "fix(review-bot): fetch origin/tickets from the mirror so the code-review agent has ticket access" into main
- Merge "docs(1fa8): record the executed two-vote-gate E2E + SHA-pin setup-python (S5/S6/S3 evidence)" into main
- Merge changes I0e8af6ed,Iaf7d6fde,I3a14391d into main
- Merge changes I793fe78b,I9c000ac9 into main
- Merge "fix(workflow): has_llm_steps detects LLM steps in v3 branch/loop/map arms" into main
- Merge "feat(review-bot): merge-change review path — auto-merge delta only, fail-closed" into main
- Merge changes I44e436a2,I1e0d3e3f,I10047530,Iafd28642,Ide74e305 into main
- Public completion seam, validated step-ids, signature-gate seam
- Merge "docs(completion): correct child-closure gate semantics (post-30c2ef3ad)" into main
- Merge "fix(ci): replicate feature/* to the mirror so g2p dispatches CI for feature-branch changes" into main
- Merge "ci: exclude feature/** from push triggers (drop redundant mirror CI)" into main
- Merge "docs(88ab): document the feature-branch flow (S4)" into main
- Merge "refactor(reads): collapse the list_tickets filter set into a TicketQuery dataclass" into main
- Merge "docs(88ab): commit S3 feature-branch CI validation evidence" into main
- Merge "ci(prompt-index): make the drift gate tolerant of the rebar.llm.prompting move" into main
- Merge "feat(review-bot): emit merge_change_409_guard event at the merge 409-guard site" into main
- Merge changes I4855fa51,I6f4061bf,Ic3f20d20,I60640bb5,I6ad0f8f6, ... into main
- Merge "test(e2e-s1): sibling story B 20260704002741" into feature/e2e-20260704
- Merge "feat(reconciler): level-triggered ADOPT heals drift class B (Jira-native issues)" into main
- Merge "feat(reconciler): baseline dual-write shadow — convergence rollout Phase 1 (7d23)" into main
- Merge "test(reconciler): construct the literal both-snapshots-unbound adopt cell (5854)" into main
- Merge "feat(attest): record the rebar version+SHA that certified an attestation" into main
- Merge feature/grounding-t2 into main — code-grounding T2 semantic resolution (epic 850f)
- Merge "fix(review-bot): rebuild the code-review runner from the re-rooted cfg" into main
- Merge "test(e2e-s6): add throwaway base module src/rebar/_e2e_s6_base_20260704045044.py" into main
- Reconciler ref-lock primitive (bare-ref CAS acquire/release/read)
- Ref-lock lease self-healing (renew/heartbeat + CAS-break-on-stale steal)
- Cut reconciler pass-lock/phase-gate over to refs/reconciler/* (ref backend)
- Retire the file-lock backend, b859 retry loop, and merge=ours carve-out
- Merge changes Ic13a3141,I77b9d956,I7f5fe8c5,I2c7b11ea into main
- Merge feature/oss-readiness into main — OSS-readiness surface
- Release 0.7.0

## [0.6.0] - 2026-06-29

### Added

- V2 declarative IR — branch/loop/map schema, frame-scoped linter, v1→v2 shim
- V2 thin interpreter — branch/loop/map, iteration-keyed replay
- Bounded-concurrent map fan-out (parallel agent calls, serialized commits)
- IR<->BPMN serializer + rebar moddle descriptor (lossless round-trip)
- Provider-agnostic PydanticAIRunner behind the Runner seam + parity bar
- Structured-output reliability stack — retire the second-interpreter LLM
- Ephemeral bpmn-js visual editor — launcher + host + IR round-trip
- Real view/edit editor — auto-layout + properties panel (vendored bundle)
- S1 — evidence contract + fail-open harness + optional-extra scaffolding
- S2/S3/S4 — Engine A resolver, T0 deps lane, Engine B detectors
- S4 — honor project tree-sitter custom grammar (ast-grep customLanguages)
- S5 — public oracle API (3 surfaces) + dimension vocab + grounding-info read tool + docs
- Show workflow semantics — start event, branch logic, prompt text; stop dropping new nodes
- Clear connection labels (Q2) + morph config-preservation guard
- Make pydantic_ai the default runner (cutover step 1/N); de-change-detector runner tests
- Finish d6d1 cutover — drop deps from [agents], wire OTel→Langfuse tracing, docs
- Add tracker.dir + tracker.branch config section [14b8]
- Route tracker_dir() through Config + add tickets_branch(); unify reads.py duplicate [8f43]
- Create/symlink/exclude the tracker at the configured dir [3e28]
- Resolve the tickets branch name via tickets_branch() across all git paths [4dde]
- Configured-vs-mounted mismatch WARN + tracker-config e2e tests [5436]
- Contract-bearing step model — walking skeleton (5e78)
- Backfill I/O contracts for all 8 built-in ops (e050)
- Closed front-matter key set + canonical writer (d25d)
- Unified prompt model — reviewer→prompt migration + derived index (afe6)
- Execution_mode enum + single_turn/agentic runner dispatch (4b2f)
- Validation depth — 3-state shallow static + runtime consumer-input (c768)
- Typed palette + prompt library + in-UI authoring/write-back (6592)
- In-editor JSON validation — debounced /validate + help panel (998e)
- Structured per-field properties panel (a83a)
- Editable structured `when` field for branch (a83a follow-up)
- Backfill inputs/outputs contracts on the 6 built-in reviewers
- Implement the plan-review verification gate (epic 5fd2)
- Implement the ISF criterion's session-log mechanics (child 681b)
- Coaching-spec finding fields + G3/G4 container per-child loop
- Runtime size-handling ladder (ca03 AC4/AC6)
- Standing eval suite (child 7284) + Pass-2/ISF discrimination cases
- Sidecar retention prune + per-pass latency/cost metrics (db7b)
- Project-extensible coach moves (75a9) + move-registry docs (6c5c)
- T12 discrimination fixtures in the eval suite (7284 AC)
- Per-plan budget cap + cap-hit INDETERMINATE shedding + centrality
- Chunk-atomic checkpointing + extract sizing.py; per-tool fail-open tests
- Lean list output by default; --full / full= opt-in for bodies (#14)
- Code-drift invalidation for attestations (Story 1, epic boil-golem-veto) (#17)
- Progressive drift-refresh re-review (Story 2, epic boil-golem-veto) (#18)
- First-class conditional overlays + delegating batch step + visual editing (epic A) (#22)
- Automated Jira↔rebar sync via GitHub Actions + client setup docs (#20)
- Completion-verifier as an engine workflow (B3)
- ProductionBatchRunner over the extracted Pass-1 loop (epic B / B1, part 2) (#32)
- Prompt/criteria library write + enumerate data model (epic B / B-DM) (#35)
- Plan-review as a v3 engine workflow (B2) (#38)
- Cut plan-review + completion gates to the workflow engine behind a flag (epic B / B5) (#40)
- Opt-in continuous-loop cadence for the reconcile bridge
- Library-backed criteria/prompt/overlay-trigger pickers + in-editor authoring (epic B / B-UX)
- UX revision — plain-language labels, dropdown parity, on-demand insert, structured ladder (B-UX)
- Parent-first cascade for claim / transition (open→in_progress) (#47)
- Enforce plan-review gate on transition open→in_progress; consolidate claim/transition gate (#49)
- Observability logging for criteria batching + LLM calls (#55)
- Reinstate per-pass latency/cost metrics on the workflow gate (toy-kink-ire) (#58)
- Managed-reference provenance gate — symmetric, churn-free removal sync for parent + links (#56)
- Epic c81c — container fan-out optimization (cache · parallelize · merge · bin-pack) (#61)
- Regression-gate the migrated reviewers + workflow-engine doc (epic 6f2d) (#62)
- Decompose the per-criterion judgment (atomic checks + independence)
- Verifier model downgrade in the workflow path + per-step model: docs (WS2 gawky-koi-grain)
- Principled token-budget chunking for the Pass-2 verify (WS3 tangly-shunt-scoop)
- Extract Pass-3 deterministic decision into shared kernel (WS1 perky-climb-trait)
- Extract Pass-2 finding-verifier + verification contract (WS2 jolty-stain-upturn)
- Extract Pass-4 coach mechanism + pluggable move-registry (WS3 groovy-lava-arc)
- Add force_close to rebar.transition (library/CLI parity) (#75)
- Enforceable, testable contracts at LLM-workflow stage seams (#77)
- Calibrate block thresholds + fix verify-budget false-block (3d3d, 59bc) (#87)
- Step-usage telemetry + double the completion-verifier step floor (#86)
- Calibrate per-criterion block thresholds from dogfooded data (3d3d)
- Impact-discrimination eval scorer + sidecar impact-distribution report

### Fixed

- Branch arms survive editor Save — '@' arm ids were illegal BPMN NCNames
- Properties panel was dead at runtime; edges weren't rendering
- Real layered layout in the serializer (no overlaps, docked edges, expanded sub-processes)
- Loud failure on unmappable elements + unique branch-arm names (Q4/Q5)
- Completion verifier passes gracefully on tickets with nothing to verify; raise step floor
- Resolve the two cutover blockers — pydantic_ai ignored its step budget; reviews had none
- Verify_completion works on the pydantic_ai runner; add live cutover-validation suite
- Coerce out-of-enum citation kinds (don't fail structured output / the close gate)
- Detect a config file appearing where none was discovered (cache freshness)
- Mirror discovery's REBAR_CONFIG-absent fall-through in the probe paths
- Detect the external marker via AST, not a substring scan
- Validate tool IDENTITY in availability gates (BSD ctags / shadow sg)
- Guard the agents extra before pydantic_ai submodule imports in run()
- Address three-pass code-review findings (C1/C2/S1-S2)
- Inspector registers step library so scripted contracts surface
- Address branch-review findings (criterion mis-attribution; id width)
- Feed the pre-resolved ticket graph to ISF (681b AC1)
- Extract claim cluster to _commands/claim.py (module-size gate) + format
- Surface a systemic LLM failure instead of a hollow PASS (fuel-posse-ball)
- Gitignore write-lock + graph-cache runtime artifacts (stem-ewe-tomb)
- Catch leaks into pre-existing state dirs e.g. .rebar/ (hurt-brow-swan)
- Enforce .env-id init gate on every write, incl. leaf appends (roar-nurse-stomp)
- Ghost/alias-guard run-state recorder before first write (bind-hcd-dam)
- Emit promotion REDIRECT after the LINK commit, not before (hulky-bag-aisle)
- Auto-attach to existing origin/tickets non-interactively (wet-chair-peg)
- Bound read-path reconverge so show never stalls on a held lock (slim-fetch-ledge)
- Reclaim a mkdir write-lock orphaned by a dead process (yaw-gravel-linen)
- Surface systemic LLM failures instead of a hollow PASS (fuel-posse-ball) (#19)
- Handle over-length descriptions + raise bulk-sync timeout (bug 626d follow-up) (#23)
- Fit description to Jira's ADF size limit, not plain-text length (#25)
- B3 reconcile must not reference/emit an absent agent summary
- Close remaining AC gaps flagged by the completion verifier (#29)
- Inbound comments — read nested "comment" snapshot key (bug 0ee6) (#31)
- Converge unmappable assignees to unassigned instead of churning (bug 9b94)
- Require an EXACT assignee match — never fuzzy-assign (bug 9b94)
- Resolve normalized assignee variants (joe-oakhart -> Joe Oakhart) (bug 9b94)
- Inbound field sync — mirror Jira-side edits instead of reverting them
- Sync issue-link relationships (blocks/relates) in both directions (bug 3f04) (#36)
- Default verify.gate_engine to bespoke (workflow plan-review verify not live-ready) (#42)
- Model ladder "Add" must persist the new row (B-UX UX follow-up)
- Complete workflow plan-review live plumbing + re-default to workflow (tepid-bus-pomp) (#48)
- Strengthen E5+E6 to flag offline-only (proxy) validation of a defaulted path (#51)
- Double + centralize LLMConfig token/iteration/timeout defaults (#53)
- --force must NOT bypass the unresolved-children close guard (warty-karma-matte)
- Propagate gate-session context into Pass-1 thread fan-out
- Pin ticket store in gate snapshot + read-only contract + tool-call backstop
- GC the pinned ticket-store entries (tickets-<sha>) (#69)
- Gate coach_notes on surviving>0 — no coach LLM call on a clean PASS (WS4 crimp-polar-jag)
- Centralize code-read-root resolution (P2 no_repo_root) (#71)
- Convey output schema to the model in the prompted structured path (#74)
- Double the default agent step budget (25 → 50) (#78)
- One atomic writer + one id rule for prompt authoring (#79)
- Resolve gate config once at the run boundary (honor caller's config) (#80)
- Unify the RetryExhaustedError / JiraAPIError taxonomy (#81)
- Verify-step budget exhaustion no longer false-blocks the claim (59bc)
- Raise default agent step budget + wire max_tokens + fast-fail truncated output
- Push-retry stash-pop corruption — repair + write-path self-heal (6818) (#91)

### Changed

- Remove the LangChain/LangGraph/deepagents runner code (cutover step 2/N)
- Single shared input-schema resolver (b642 / review M2)
- Derive Prompt.is_reviewer from category (3bb5 / review M1+M4)
- Remove shallow_contract_check __getattr__ re-export (ae52 / review M3)
- Extract inspector contract-views to editor_contracts (748a / review M6)
- Move criteria + pass prompts into the prompt library
- Break reducer↔_engine_support import cycle via rebar._alias leaf (#13)
- Move Pass-4 move registry to passes.py (module-size gate)
- Consolidate 5 canonical-JSON/hash sites onto the _store/canonical seam (blunt-tramp-kale)
- Unify the two jittered-backoff functions onto one helper (snag-reek-ember)
- Extract recorder seam so executor.py stays under the size cap
- Consolidate canonical-JSON/hash + advisory-lock backoff (epic civil-marlin-flare) (#21)
- Extract Pass-1 finder machinery into pass1.py (epic B / B1, part 1) (#30)
- Retire bespoke run_review + B4 parity harness + gate_engine flag — workflow is the sole gate (epic B / B-RETIRE) (#50)
- Migrate drift-refresh probe onto the workflow gate + delete bespoke _run_passes/pass2_verify/verifier_cfg (WS1 odd-cocoa-chase)
- Split prompts.py front-matter cluster to prompts_frontmatter.py (#72)
- Assemble the ticket graph once per gate run (run-scoped memo) (#76)
- Split applier.py at the dispatch/handlers seam (self-waltz-ace) (#82)
- Split outbound_differ.py into per-concern differ modules (unfed-liner-arson) (#83)
- Decompose reconcile_once() into in-place phase helpers (hush-quail-holm) (#84)

### Documentation

- Ground plan-review chunking + budget defaults (919-run study)
- Opus-vs-Sonnet model tradeoff + intentional grouping (460-run round 2)
- Criteria storage + exec-tier chunking + agentic-tier study (round 3)
- Dogfood the plan-review gate on its own epic (baseline)
- Decompose epic + full-suite review + overlay triggering + proportionate-scrutiny re-tune (round 4)
- Reconcile criteria registry — "EXP" was the existing T2 empirical probe
- Complete the criteria set + add a registry completeness guard
- Relocate plan-review probe artifacts to a labeled non-test/non-prod dir
- Criteria coverage/research/gaps — add G6/T10/T11/T12 + roll-ins (round 5)
- Validation/tuning experiment plan + seeded-defect recall pilot (round 5b)
- Validation scorecard — recall + precision on real DSO plans
- Plan-review finalize — three-pass adoption + non-DSO generalization (criteria v7->v8)
- Record T12 kill-switch adjudication (config-gating is the rollback)
- Agent-vs-single-turn bright line — centralize code-grounding, reclassify G3/G4/T10/T11
- Prompt-engineering — reason-first contract + affirmative/verbosity framing
- Pink-elephant-safe FP framing, zero-findings-is-success, aggregate non-frontier Pass-2
- Resolve charity-vs-skepticism — finder skeptical, verifier owns entailment
- Verifier — skepticism of the FINDING via charitable plan-reading (one critical pattern)
- Add approved intent-source-fidelity (ISF) criterion to the registry
- Raise rough-test output/iteration limits (they were far too low)
- Fix Pass-2 scaling (batch aggregate) + INDETERMINATE-not-dropped; re-run finds
- T5c security-prompt FP fix (domain-appropriate; no already-in-repo "leakage")
- T5c experiment -> flip security to AGENT; seed eval suite with observed FPs
- Settle grounding tiers via experiment — T10/T11→AGENT; refined bright line
- Faithful three-pass review cycle — agentic Pass-1 + process fixes validated
- Feed FULL child bodies — never truncate the reviewed artifact
- No-truncation-by-design — absence-FP verifier rule + context-window escalation
- Never chunk ticket content — batch→single-criterion→escalate→too-big-finding
- Real context-window ladder (spike) + P8 reviewability DET criterion
- Workflow-engine-v2 de-risk POCs + structured-output research
- Plan-review gate — AGENT tool-access contract + FP eval cases
- Code-grounding spike — ctags refutation yield 100%/0-FP polyglot; grep-ast not a repo-wide indexer; OpenGrep-as-registry validated
- Spike 2 — de-risk per-child review findings (engine validate, collision/member guard, 88% real-corpus yield, deps gauntlet, evidence mapping)
- Document the visual editor for developers; ship the vendored bundle
- Correct layout description — own layered layout, expanded inline
- Document start/end events, branch labels, prompt text, parallel fan-out
- Remediate vestigial pre-cutover references found by the independent audit
- Clear the last non-historical cutover references (3rd audit pass)
- Reference the 3 de-risk POCs (epic acceptance criterion)
- Clarify the store is not human-readable + add synthetic sample event log
- Authorize GitHub auto-merge when opening PRs
- Workflow authoring v2 reference + execution_mode ADR (3a8f)
- Purge stale old-prompt-system references (catalog.json / Langfuse-fetch)
- Correct gate description from "three-pass" to "four-pass (find→verify→decide→coach)" (#15)
- Link setup guide from README + fill gaps learned from the live cutover
- Document the parent-hierarchy sync limitation (#37)
- Require claiming a ticket before working on it (incl. moving an epic in_progress) (#45)
- Consumer seam doc + verifier-rules scaffold + behavioral evals (WS4 ashen-deed-mantle)
- Accuracy fixes — core deps, install hints, migrated paths, gate keys (#73)

### Other

- Merge pull request #7 from navapbc/ci/run-on-all-branches
- Merge branch 'main' into epic/workflow-authoring-v2-foundation
- Merge pull request #8 from navapbc/epic/workflow-authoring-v2-foundation
- Fix da27 completion-gate gaps: built-in prompt golden tests + structured-only step-config editor (#9)
- Merge pull request #10 from navapbc/feat/plan-review-gate
- Merge pull request #11 from navapbc/chore/lint-format-automation
- Consistent error-handling convention + enable BLE001/T201 lint gate (epic ring-gun-jot) (#16)
- Merge branch 'main' into worktree-civil-marlin-flare
- Merge remote-tracking branch 'origin/worktree-civil-marlin-flare' into worktree-civil-marlin-flare
- Merge pull request #24 from navapbc/feat/gate-workflow-migration
- Merge branch 'main' into docs/jira-sync-verify
- Merge pull request #27 from navapbc/docs/jira-sync-verify
- Merge branch 'main' into worktree-civil-marlin-flare
- Merge pull request #26 from navapbc/worktree-civil-marlin-flare
- Merge pull request #28 from navapbc/fix/assignee-unmappable-converge
- Close the Jira-sync producer↔consumer test-coverage gap class (epic f89d) (#43)
- Merge pull request #46 from navapbc/feat/reconcile-continuous-loop
- Merge pull request #44 from navapbc/feat/b6-samples
- Merge pull request #41 from navapbc/feat/b-ux-editor
- Merge pull request #52 from navapbc/worktree-force-childclose
- Repo-snapshot isolation for the code-reading gates (epic raze-vet-ditch) (#57)
- Plan-review Pass-2 verify: workflow verifier model + drift-refresh migration + coach gating + token-budget chunking (epic solid-timer-unison) (#64)
- Merge pull request #65 from navapbc/feat/completion-verifier-binary-discipline
- Merge pull request #66 from navapbc/worktree-tingly-wishing-nygaard
- Merge pull request #67 from navapbc/worktree-tingly-wishing-nygaard
- Merge remote-tracking branch 'origin/main' into worktree-local-rebar-epics
- Merge remote-tracking branch 'origin/main' into worktree-local-rebar-epics
- Merge pull request #68 from navapbc/worktree-local-rebar-epics
- Unify CREATE/UPDATE assignee resolution + add ticket.default_assignee (claim default) (#89)
- Merge remote-tracking branch 'origin/main' into feat/plan-review-calibration-and-verify-recovery
- Merge pull request #88 from navapbc/feat/plan-review-calibration-and-verify-recovery
- Merge branch 'main' into worktree-plan-review-agentic-budget
- Merge pull request #90 from navapbc/worktree-plan-review-agentic-budget
- Release 0.6.0
- Merge pull request #92 from navapbc/worktree-release-0.6.0

## [0.5.2] - 2026-06-19

### Added

- Typed core Config dataclass + from_mapping (252e)
- TOML loader + discovery + CLI>env>project>user>defaults layering (43a0)
- Route verify gate + display_mode through unified load_config (fe78)
- Rebar config — resolved-value + per-layer provenance command (c647)
- Legacy-config back-compat alias window + unknown-key cutover (83e6)
- EV-1 unify sync model under REBAR_SYNC_PUSH/REBAR_SYNC_PULL (b1eb)
- EV-3a core config-backed env renames + aliases (6b1b)
- EV-3b rename TICKETS_TRACKER_DIR -> REBAR_TRACKER_DIR (9492)
- EV-3c reconciler/LLM tunable renames + id-guard value-flip (301b)
- EV-4 remove REBAR_LLM_RUNNER knob; derive the runner (1465)
- Wire the CLI precedence layer — `rebar -c SECTION.KEY=VALUE` (cdd4)
- 0ac6 slice 1 — route ticket_clarity.threshold through the typed Config
- 0ac6 slice 2 — route reconciler.* through the typed Config
- Add5 — register session_log type + write-path rules
- 0ac6 slice 3 — route jira.* through the typed Config
- 0ac6 slice 4 — [tool.rebar.llm] config-file support in LLMConfig
- 7657 — isolate session_log from graph/health paths + default list
- 1368 — exclude session_log from outbound Jira reconcile
- 0b32 — unify canonical event-byte serialization + structural guard
- E2e3 — recent_session_logs across library, CLI, and MCP
- 7d1d — Hybrid Logical Clock for skew-immune event ordering (P2.1)
- E7e4 — session-log capture helper across library, CLI, MCP
- 31e8 — structured search predicates, --sort, derived updated_at (P1.1)
- T1 — carry source_* provenance through write path + surface in ticket_state
- T2 — NDJSON export (rebar export + export_tickets + export.schema.json)
- T3 — NDJSON import core (rebar import + import_tickets, two-pass)
- T4 — import idempotency (skip by source_id) + deferred-push + docs
- WS-B1 — workflow DSL v1 schema + hardened YAML safe-parser
- WS-B3 migrate shim + WS-B2 reference-integrity linter
- WS-B4 — `rebar workflow new/validate` CLI + 3-step scaffold
- WS-C1 — WORKFLOW_RUN/WORKFLOW_STEP event types + per-key LWW
- WS-C2 — thin linear executor + Burr-tripwire
- WS-C3 — run identity, idempotent resume, capture, TTL sweep
- WS-C4 — run_workflow + status/result across lib/CLI/MCP
- WS-E — scripted-step library (built-ins incl. unsecured gate)
- WS-H — commits-on-ticket event type (COMMITS)
- WS-I — read-only Mermaid graph render
- MCP layer — typed outputSchema for workflow read tools (ffc4)
- WS-J1+J3 — extras taxonomy + guard_import + optionality CI
- WS-J2 — `rebar llm setup` wizard (completes WS-J)
- WS-D2 — hardened git-ref filesystem snapshot
- WS-D1 — generalize RunRequest + one finalization strategy
- WS-D3 + WS-D4 — scoped ticket tool, multi-provider, runtime hardening
- WS-K2 — review_ticket reframed as a workflow + RunnerAgentStep bridge
- WS-K3 — code_review example workflow + retire LangflowRunner
- WS-F1 — invert resolve_prompt to git-canonical
- WS-F2 (partial) — template-variable parity gate + prompt-ref lint
- WS-F2 complete — prompt variant overlays + JSON schema + parity
- WS-G — prompt evals (Inspect AI seam + grader discipline + promptfoo)
- WU-1 — re-enable stock git gc (drop gc.auto=0 forcing)
- WU-3 — .gitattributes merge=ours for shared mutable root files
- WU-2 — union-merge (not reset --hard) for unrelated histories
- Pluggable per-op structured-output contract + completion_verdict schema
- Completion-verifier prompt + verify_completion operation
- Expose verify_completion on CLI + MCP
- Completion-verification gate + signing on close
- Enable completion-verification close gate + LLM module for this project
- TAG_DELTA event type + reducer core (P2.3 WU-1)
- CLI/library/MCP emit TAG_DELTA (P2.3 WU-2)
- Jira inbound applier emits TAG_DELTA + intent reads deltas (P2.3 WU-3)
- Fsck unknown-newer-type warning + docs (P2.3 WU-4)
- Bridge_fsck unknown-type warn + shared guard + edit-help set-tags note (P2.3 WU-4)

### Fixed

- A downed MCP server must not silently yield zero tools (9bd5)
- Soft-delete children guard honors effective parent_id (4253)
- Address session code-review findings (ruff, compact, mcp gates)
- Ignore non-absolute XDG_CONFIG_HOME; document macOS path (OSS validation)
- Prone-octet-cheek — auto-push the inline-commit write paths
- 31e8 — resolve P1.1 review findings (cache version, parser edge, tests)
- Address subagent-validation concerns across WS-B/C/E/I/J + ffc4
- Harden against prior-art gotchas (GHA/Argo/tarfile/dotprompt)
- Converge reliably — decisive model + bounded exploration
- Epic verification = own criteria + child-closure trust
- Root cause = ToolStrategy forced tool_choice; use natural termination
- Shared tag-name validation on leaf path + set‖remove tests (P2.3 WU-2)
- Deterministic child-closure GATE before the LLM (bug a254)
- Drop child-mention from the prompt (avoid pink-elephant priming)
- Verify_completion degradation needs a childless ticket (a254 follow-up)
- Portable read-only publish + sandbox the sweep test (CI macOS + leak)

### Changed

- Conflicts key only off declared file_impact; drop fuzzy inference + planning config
- Cache config resolution on the command hot path (e211)
- EV-2c remove dead internal flags + self-resolve handoffs (38c7)
- EV-2a remove PROJECT_ROOT — REBAR_ROOT is the sole repo-root override (dab5)
- EV-2b remove TICKET_CMD validate injection seam (096c)
- WS-A — extract runner.py fs/repo cluster to llm/fs_tools.py
- Route LINK writes through the canonical _store write path (S1)
- DRY five validated duplication clusters (S6)
- Dead-code removal + low-severity hygiene (S8)
- Reduce accidental complexity — named budget + inbound field-map (S7)

### Documentation

- Config ADR + reference (design of record for epic a621)
- F81a — sweep README/CLAUDE.md/docs for the TOML config surface
- 0ac6 — move reconciler/jira/llm into the wired inventory
- 8e10 — document the type, exemptions, and conventions
- Install docs — runtime (prod) vs rebar development; new deps (epic a88f)
- Correct stale 'dependencies = []' claims; make LLM-optionality unmistakable
- WU-4 — document the non-destructive-recovery safety invariant
- Brief note on ticket hierarchy (parent/child) + pointer to --help
- Update CLI --help (edit + overview) for tag grammar (P2.3 WU-4)
- Documentation-truth sweep — purge deleted-bash-engine narrative + fix drift (S3)
- Document the deliberate tombstone boundary for unresolved blockers (S5)
- Correct stale "warn-only" module-size report claim (epic follow-up)

### Other

- Fix all pre-existing ruff check + format failures (bug deea-b923)
- Release 0.5.2

## [0.5.1] - 2026-06-16

### Added

- Generalize list filters — children_count, --min-children, --unblocked/--blocked (worm-burr-fly)
- Port to Python behind REBAR_COMPUTE (Tier C, sure-beech-taunt)
- Port to Python behind REBAR_COMPUTE (Tier C, gawk-grove-site)
- Flip REBAR_COMPUTE default bash→python (step 5)
- Rebar._store write/sync core behind REBAR_WRITE_CORE (port + wiring)
- Flip REBAR_WRITE_CORE default bash→python (step 5)
- Argparse CLI skeleton + help goldens + auto-init middleware (E0, no cutover)
- Cut the rebar CLI entrypoint over to the in-process argparse CLI (E1)
- Port get-file-impact + get-verify-commands in-process (E2)
- Port exists + resolve + format in-process (E2.2)
- Port list-descendants in-process (E2.3)
- Port clarity-check + check-ac + quality-check + summary in-process (E2.4)
- Environment-bound manifest signing + verification
- Replace closure verdict-hash gate with the signature system
- Relocate transition/claim write core into rebar._commands.txn (E5c)
- Port transition/reopen in-process + relocate unblock logic (E3)
- Port claim in-process over the relocated claim core (E3)
- Port compact/compact-all in-process + rewire compact-on-close (E3)
- Port scratch set/get/clear in-process (E3)
- Port delete in-process (E3) — completes the write/lifecycle cluster
- Port fsck in-process (E4)
- Port fsck-recover in-process (E4)
- Port init in-process + interactive auto-init consent gate (E4)
- Add finding/citation/severity schema $defs for the LLM review framework
- Add finding/citation/severity + review_result output schemas
- Agent-operations framework + review_ticket reference op
- First-class multi-provider config; ship + validate Claude and ChatGPT
- Windowed reads + ignore-aware discovery + tool-use steering
- Add opt-in DeepAgentsRunner; keep langgraph as the review default
- Implement the LangflowRunner REST client (closes 0224)
- Code-review operation + multi-reviewer aggregation (closes 47fb)
- Batch epic-vs-spec scan operation (closes 453c)
- E5 — port bridge-status + purge-bridge in-process
- E5b — rewire reconciler off the bash compat shims
- Register output schemas for sign & verify-signature
- Wire link diffing into the outbound and inbound differs
- Apply Jira link sync end-to-end (outbound create + inbound write)
- Make LLM-optionality airtight + add exhaustive cross-interface guard
- Ephemeral self-hosted Langfuse CI + fix v4 SDK trace-id capture

### Fixed

- Make children_count opt-in (preserve show==list==search invariant); drop list-epics from --help/docs (worm-burr-fly)
- Stop bash-suite arity tests leaking .tickets-tracker into REPO_ROOT + pin --basetemp
- Address review — uniform verdict shape, 0600 key, hardening
- Close concurrency + fail-closed gaps from 2nd review
- Address gate-replacement review (S1 head-binding + hygiene)
- Pre-existing test-isolation + macOS bash-3.2 failures
- Empty-key hardening (review) + macOS compact-all mapfile
- Fsck must skip hidden dirs (.bridge_state/.git) like the bash glob
- Correctness fixes from the E0–E4 opus review (+ regression tests)
- Address code-review + research findings; add multi-provider support
- Address PR #6 review — deny-list in citation resolution; stream read_file
- Address PR #6 review round 2 (symlink escape, citation scan, docs)
- Address PR #6 self-review findings (aggregation, langflow, deepagents)
- Correct the broken Jira link client primitives
- Bound the acli subprocess with process-group reaping (d843 pass 1)
- Link write-safety + deterministic link dedup tests (d843 pass 2)
- Bound network git subprocesses with a timeout (c16f)
- Resolve the STATUS fork tie-break by event UUID as documented (8874)
- Harden the live agent path — 3 runtime bugs found by live validation
- _trace double-yield masked errors; seed trace test so review converges

### Changed

- Reduce to a thin deprecating wrapper over generic list (worm-burr-fly)
- Retire bash compute — delete next-batch/validate scripts + REBAR_COMPUTE (step 7)
- Retire the bash write/sync core (step 7)
- Start an external-integration suite; generalize the live CI job
- E5c — delete the bare _engine/event_append.py
- E6a — rewire rebar-runtime importers off the compat shims
- E6.5a(1/6) — composer alias-compute in-process + grounded E7 plan
- E6.5a(2/6) — resolver alias/jira_key lookup in-process
- E6.5a(3/6) — link --dry-run preview in-process
- E6.5a(4/6) — transition un-archive (archived->open) in-process
- E6.5a(5/6) — bridge-fsck audit in-process
- E6.5a(6/6) — bridge-probe off the bash dispatcher
- E7a — rewire reducer/graph pytest tiers off the compat shims
- E7d(partial) — reducer test fixture off the bash helper
- E7d — graph tier + rebar.graph flat API off ticket-graph.py
- E7e — sever reconciler/validate off the bash dispatcher
- E7-final — delete the bash engine; rebar is one Python impl
- Consolidate duplicated seam helpers; drop dead code & stale docs

### Documentation

- Direct agents to record plans/progress in rebar, not scratch notes
- Mark Tier D retired (§6, architecture write-path + offender table)
- Persist the opus-reviewed execution plan + handoff state
- Compare rebar to OSS ticket systems with remediation roadmap
- Add detailed remediation implementation plan
- Revise remediation plan per review (round 1)
- Revise remediation plan per review (round 2)
- Revise remediation plan per review (round 3)
- Revise remediation plan per review (round 4)
- Correct dead-script cleanup — ticket-link.sh still serves link --dry-run
- Revise remediation plan per review (round 5)
- Add BRIDGE_ALERT live writer; clarify revert/PRECONDITIONS (pre-empt round 6)
- Fix ticket-revert.sh classification per review (round 6)
- Validate surviving changes with experiments + proven-art convergence
- Make the P2.3 first-delta boundary rule precise
- Clear review MINORs (round on the refined plan)
- De-risk implementation with real-code experiments (EXP-R*)
- Session log — OSS comparison, validated plan, 7 rebar epics
- Transition_core enforces the signature gate, not verdict-hash
- Document the agent-operations framework
- Record E6.5a-complete + E8-batch-1 state; pin E8b/E7/E9 resume
- Pin precise E7d-remaining (6 helper-coupled tests + engine delete)
- Record E7e blocker — reconciler/validate invoke the dispatcher
- Reference Serena MCP code-navigation in CLAUDE.md
- Rewrite architecture.md to the in-process Python core; mark Tier E done

### Other

- Merge pull request #3 from navapbc/claude/oss-ticket-systems-analysis-g857w0
- Merge remote-tracking branch 'origin/main' into claude/ticket-signature-verification-j9op9e
- Harden reliability/maintainability: fail-closed verdict gate, lint/type/coverage CI, real-bug fixes
- Pin gating scope with follow_imports=silent
- Sweep low-risk ruff findings and promote them to gating
- Address branch review: correct Python floor to 3.11; cover verdict-gate gaps
- MacOS/bash-3.2 compat: drop mapfile + guard empty-array expansions
- Address PR review (Copilot): drop unused import, narrow fail-closed except
- Merge origin/main (Tier E in-process ports) into reliability branch
- Merge pull request #2 from navapbc/claude/project-reliability-maintainability-7i8to3
- Merge origin/main into the signing branch (resolve Tier E conflicts)
- Merge pull request #4 from navapbc/claude/ticket-signature-verification-j9op9e
- Separate first-time init from worktree symlink in the auto-init gate
- Merge pull request #5 from navapbc/claude/ticket-init-symlink-separation-em51mi
- Merge remote-tracking branch 'origin/main' into claude/langflow-langfuse-setup-j8rotq
- Apply ruff format across src + tests (recommended defaults)
- Merge origin/main: separate worktree-symlink from first-time init in the auto-init gate
- Merge remote-tracking branch 'origin/main' into claude/langflow-langfuse-setup-j8rotq
- Ruff format the rebar.llm files (CI format gate)
- Merge pull request #6 from navapbc/claude/langflow-langfuse-setup-j8rotq
- Release 0.5.1

## [0.5.0] - 2026-06-12

### Added

- Adopt jira-capability-probe as reachable bridge-probe preflight (young-sill-path)
- Move rebar writable state out of .claude/ to .rebar/ (petty-pixel-rat)
- Canonical exit-code contract doc + conformance test (urge-index-zoom)
- Version constant + unknown-event forward-compat rule (astir-plank-scuff)
- Error_envelope on every --output json failure path (large-comet-mica)
- Shared event-append module; lock the reconciler's event writes (pokey-matte-flute)
- REBAR_PUSH policy + mkdir-lock stress test (hip-rod-graze)
- Tier B foundation + first leaf-write ports behind REBAR_LEAF_WRITES (cakey-siren-syrup)
- Tier B port tag/untag to rebar._commands (rely-suede-chase)
- Tier B port archive to rebar._commands (peak-fawn-bug)
- Tier B port create to rebar._commands.composer (slum-visor-snail)
- Tier B port edit to rebar._commands.composer (curb-haste-dent)
- Tier B port link to rebar._commands.composer (per-snip-shore)
- Tier B port revert to rebar._commands.composer (bossy-metal-mull)
- Tier B port unlink to rebar._commands.unlink (shiny-pig-fig)
- Tier B cutover — flip REBAR_LEAF_WRITES default to python (bored-nape-kin)

### Fixed

- Fold st_mtime_ns into dir-hash so same-size rewrites invalidate cache (1d76-b6d1)
- Carry recorded file_impact into conflict detection (db34-9db9)
- Reopen arity guard + complete help overview (3758-60b9, e14e-70b3)
- Gate mutating reconcile/fsck, fix gate contracts + doc drift (9d7c-081b, f6f6-bc8e, ef5f-f307, efb4-4931, b7af-9623)
- Import rebar._native before rebar_reconciler.mode in reconcile gate (9d7c-081b follow-up)
- Reject unknown options in the collapsed ready arm (23d2-e0f3 follow-up)
- Load engine MODE_CAPS by file path to survive rebar_reconciler shadowing (9d7c-081b follow-up)
- Require a git-backed tracker when resolving via repo-root (23d2-e0f3 regression)
- Fold st_mtime_ns into the cache key (zonal-folly-ditch)
- Search/deps reject unknown options instead of silently ignoring (witty-lath-trend)
- Don't remove .git/index.lock under REBAR_MCP_READONLY (terse-frost-ale)
- Cap-0 modes (dry-run/reconcile-check) honor their no-write contract + return the plan (yaw-plait-doe)
- Case-insensitive truthy parse for readonly/reconcile-live env gates (ship-mogul-glob)
- Revert of an ARCHIVED event un-archives the projection (vocal-jig-apron)
- Enforce element contract + reject empty --title (jaded-sled-pyre, woozy-hat-match)
- Add archived->open unarchive seam + accurate verdict-hash help (bored-grain-wok, dreamy-lop-rat)
- UUID dedup on replay; delete dead strategy layer (ship-guy-sod)
- Expose exclude_deleted on library + MCP list_tickets (real-payee-noun)
- Remediate code-review findings (H1/M1/M3/M4/L1)
- Make inbound create 1-pass idempotent (robe-creek-zealot)
- Derive status via the reducer, not a raw STATUS scan (vary-ion-fry)
- One heading vocabulary across clarity-check/check-ac; drop dso language (bandy-name-hilt)
- Bash suites must run the editable rebar, not a stray global one (8dc0-799f)

### Changed

- Collapse dual read path into one engine impl + uniform freshness (23d2-e0f3)
- Repackage reducer/graph/reads as rebar.* subpackages (fare-rant-clasp)
- Extract inbound_translate.py from applier.py (tangly-abbey-smelt)
- Extract pass_io.py from applier.py (tangly-abbey-smelt)
- Extract rebar_id_audit.py from applier.py (tangly-abbey-smelt)
- Extract apply_base.py foundational layer (tangly-abbey-smelt)
- Extract batch_dispatch.py from applier.py (tangly-abbey-smelt)
- Extract apply_outbound.py from applier.py (tangly-abbey-smelt)
- Extract apply_inbound.py from applier.py (tangly-abbey-smelt)
- Extract typed_dispatch.py (registry + dispatcher) (tangly-abbey-smelt)
- Extract apply_planning.py; applier.py now <800 (tangly-abbey-smelt)
- Rename acli-integration.py -> rebar_reconciler/acli.py (AC2, tangly-abbey-smelt)
- Extract jira_fields.py from acli.py (tangly-abbey-smelt)
- Extract acli_subprocess.py from acli.py (tangly-abbey-smelt)
- Extract acli_cli_ops.py from acli.py (tangly-abbey-smelt)
- Extract AcliRestMixin (acli_rest.py) from AcliClient (tangly-abbey-smelt)
- Extract AcliGraphMixin (acli_graph.py); acli.py now <800 (tangly-abbey-smelt)
- Tier B retirement — delete the switch + bash leaf bodies (bored-nape-kin)

### Documentation

- Clarify children_count is total non-deleted children, not open (isle-wheat-spire)
- Codify module-size policy + warn-only CI size report (pond-rebel-flora)
- Committed strangler-fig migration plan for adult-oxide-slave (prim-myth-grain)
- Record Tier B porting status — all 11 leaf writes ported, dual-run green, default bash

### Other

- Release 0.5.0

## [0.4.0] - 2026-06-10

### Added

- JSON Schemas for every output shape + typed MCP returns (#T1)
- --output json for create/claim/transition/reopen/delete (#T3)
- --output json for list-epics/summary/check-ac/quality-check/fsck/bridge-fsck (#T4)

### Fixed

- Track the lifecycle-section ticket for cleanup

### Changed

- Standardize structured output on --output/-o (drop --json/--format) (#T2)

### Documentation

- Add post-release smoke test (update local from channel + probe); migrate probe to --output

### Other

- Release 0.4.0

## [0.3.0] - 2026-06-10

### Changed

- Single source for _BLOCKING_RELATIONS (#2 Step 4b)
- Remove REBAR_NATIVE_READS kill-switch — single in-process path (#2 Step 5)
- Rename dso-id identity scheme to rebar-id; drop skipped scaffold
- Complete DSO migration — status labels, local_id, env vars, cleanup

### Documentation

- Explain why the engine installs unpacked to disk + native reads (#1)

### Other

- Release 0.3.0

## [0.2.0] - 2026-06-09

### Added

- Scope hierarchy promotion to blocking deps at comparable levels
- --help/-h/help prints usage without executing the command
- Canonical JSON Schema for ticket state + cross-interface validation
- Verdict-hash gate is opt-in (default off)
- In-process native library reads behind REBAR_NATIVE_READS (#2 Steps 0/2/3)

### Fixed

- Validate edit priority/ticket_type; parse summary from JSON
- Open-children guard honors reparented children + accurate wording
- Exists resolves aliases / short ids / prefixes
- Validate is repo-wide — drop ticket_id from library + MCP (bug 5199-ffba)
- Add `ready --json` to the engine — lib ready() + MCP ready_tickets (bug 1598-f136)
- Add VERIFY_COMMANDS processor — verify_commands survives list/search + compaction (T1, bug f026)

### Changed

- Single source of truth — delete jq reducer, route show/get-file-impact through Python (T2-T4, bug f026)
- Lift search + ready logic into packages (SSOT, #2 Step 1)
- Clean break from dso — rename reconciler echo marker, decouple text

### Documentation

- Add releasing runbook (PyPI / Homebrew tap / MCP Registry update process)
- Per-channel install instructions (Homebrew / PyPI / MCP Registry / source); release runbook MCP gotchas
- Validate scope, link/unlink semantics, promotion, auto-push
- Type verify_commands in ticket_state schema (T5, bug f026)
- Add problem-first hook + 'Why rebar' section
- Accurate prerequisites — flock optional w/ fallback, extras, test env

### Other

- Shorten description to <=100 chars (MCP Registry limit)
- Release 0.2.0

## [0.1.1] - 2026-06-09

### Other

- Add Apache-2.0 license; MCP registry manifest + Homebrew/MCP install docs
- Release 0.1.1: license metadata on PyPI + MCP-registry ownership annotation

## [0.1.0] - 2026-06-09

### Other

- First commit
- First commit
- Extract DSO ticket system + Jira reconciler into standalone rebar
- Merge extract-ticket-system: standalone rebar ticket system + Jira reconciler
- Decouple from DSO plugin; add 3-interface parity suite; fix fsck/qualify bugs
- Harden concurrency, extract txn, rename to rebar, agent-fitness features
- Rename dist to nava-rebar; add PyPI Trusted Publishing workflow

[unreleased]: https://github.com/navapbc/rebar/compare/v0.13.1...HEAD
[0.13.1]: https://github.com/navapbc/rebar/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/navapbc/rebar/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/navapbc/rebar/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/navapbc/rebar/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/navapbc/rebar/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/navapbc/rebar/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/navapbc/rebar/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/navapbc/rebar/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/navapbc/rebar/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/navapbc/rebar/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/navapbc/rebar/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/navapbc/rebar/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/navapbc/rebar/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/navapbc/rebar/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/navapbc/rebar/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/navapbc/rebar/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/navapbc/rebar/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/navapbc/rebar/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/navapbc/rebar/compare/v0.1.0...v0.1.1
