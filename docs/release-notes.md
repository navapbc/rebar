# Release notes

> **User-facing changelog: [CHANGELOG.md](../CHANGELOG.md).** This file tracks
> agent-visible *contract* changes (event/schema/API); the user-facing changelog
> of features and fixes lives in `CHANGELOG.md`.

Agent-visible contract changes, newest first. rebar shares one `origin/tickets`
across many clients, so contract changes are called out here when they could be
observed by an agent or a different rebar version.

## BREAKING (pre-1.0) — legacy reconcile compatibility surfaces are removed

The public Python facade no longer exports `rebar.reconcile`, the MCP server no longer
registers a `reconcile` tool, and the top-level CLI `rebar reconcile` route is no longer
registered or advertised. Direct `python -m rebar_reconciler --mode reconcile-check` and
`--filter-local-ids` invocations now reject before operational work starts, and bridge status no
longer reads stale `.bridge_state/reconcile-check.json` diagnostics.

Use `rebar bridge preview` / `rebar.bridge_preview(...)` for live Jira-vs-local proposed
changes, `rebar bridge sync` / `rebar.bridge_sync(...)` or `bridge_run(profile=...)` for
mutating synchronization, `rebar bridge fsck` / `rebar.bridge_fsck(...)` for offline
binding/integrity audit, and `rebar bridge status` / `rebar.bridge_status(...)` for operational
state. Scheduled bridge runners may keep the profile spelling `reconcile-check`; it now invokes
canonical preview.

## `severity` is now OPTIONAL on `review_result` findings (epic `pink-complex-xenurine`)

The review-kernel (`pass3_decide`, shared by code-review and plan-review) no longer stamps a
`severity` label onto the findings it emits — it was derived from `impact` alone and could
disagree with the kernel's own `priority = validity × impact` decision, misleadingly
outranking a genuinely higher-priority finding. Review-kernel-sourced findings now carry
`priority` and `decision` (`block`/`advisory`) instead; the Gerrit `LLM-Review` comment and the
CLI text renderers show blocking/advisory, not a severity word.

**Schema effect (`common.schema.json`'s shared `finding` def, used by `review_result` and
`completion_verdict`).** `severity` is removed from the `required` list — a relaxation, not a
field removal (the `severity` property and its enum stay defined). This is scoped narrowly:
`completion_verdict` findings always come from an unrelated completion-verification pipeline
that never computed impact/priority in the first place, and that pipeline continues to always
supply `severity` in practice, so its behavior is unaffected. Only `review_result` findings
sourced from the review-kernel may now lack `severity` — a consumer that unconditionally reads
`finding["severity"]` without checking for its presence should switch to `finding.get("severity")`.

## BREAKING (pre-1.0) — `rebar export` NDJSON timestamps are STRINGS (`schema_version` 1 → 2)

Bug `guilty-pusslike-wyvern` (`a8db-dc3c-983a-40b0`), 2026-08-28. The js-safe decimal-string wire
form adopted for the MCP surface (bug `6fe7-956f-4901-45cf`) and then the CLI `--output json`
surface (bug `e127-a3ad-895a-4a2f`, the entry below) now also applies to the **NDJSON export
projection** — `rebar export` and its library twin `rebar.export_tickets()` — the last surface that
still emitted bare `time.time_ns()` integers. That entry explicitly deferred export as
"a separately versioned interop projection with a round-trip contract against `rebar import`"; this
change makes the coordinated export+import move.

**Why.** The export artifact is consumed by `jq`, `node`, and JS-based DuckDB/pandas loaders —
exactly the float64 consumers RFC 8259 §6 does not protect. Measured on a freshly created ticket
carrying a comment, `rebar export` emitted **4** unsafe integers (`$.created_at`,
`$.comments[0].timestamp`, `$.signature.signed_at`, `$.updated_at`); a stored `…502255001` came back
from `node`'s `JSON.parse` as `…502255000`, silently 1 ns wrong. After the fix: **4 → 0**.

**What changed on the wire.** An integer outside `[-(2**53)+1, (2**53)-1]` is emitted as its EXACT
decimal STRING; in-range integers (`priority`, counts) stay JSON numbers. The affected fields are
`created_at`, `updated_at`, `comments[].timestamp`, `signature.signed_at`, and the
`source_created_at` provenance carried on imported records. `EXPORT_SCHEMA_VERSION` is bumped to
**2**; `export.schema.json`'s description now documents the wire form (the fields were already typed
`["integer", "string", …]`, so no field was retyped).

**Round-trip preserved.** `rebar import` (via `rebar._io._provenance`) now coerces the imported
`created_at` / comment `timestamp` provenance with `int()`, accepting BOTH the new string wire form
and older bare-integer exports, and storing a canonical `int`. `export | import` reproduces the
EXACT nanosecond digits — pinned by a regression test that asserts the digits (`int(wire) == stored`),
not merely the JSON type, since a float64 round-trip would pass a type-only check while returning
`…000`.

**Migration for consumers.** Parse these fields with `int(x)` / `BigInt(x)` — a schema-conformant
consumer already accepted the string form, since `export.schema.json` typed them `["integer",
"string"]` and states the projection may evolve without breaking consumers. What breaks is **naive
raw-integer arithmetic** on the parsed value (`.created_at + 0` in `jq`, `data.created_at - start` in
Node), which now fails LOUDLY instead of silently returning a rounded number. rebar's only in-repo
consumer of the export format is `rebar import`, updated in lockstep here.

## BREAKING (pre-1.0) — nanosecond timestamps are STRINGS on CLI `--output json` too

Bug `unhelping-creviced-rhino` (`e127-a3ad-895a-4a2f`), 2026-08-28. The wire form adopted for the
MCP surface on 2026-08-27 (bug `6fe7-956f-4901-45cf`, the entry below) now also applies to the CLI
`--output json` / `--output llm` reads. `docs/api-stability.md` calls `--output json` rebar's
**strongest contract**, so this is recorded as a deliberate BREAKING retype, not a quiet fix.

**Why the CLI, and why now.** The CLI is the surface where a user is explicitly invited to pipe
rebar JSON into `jq` or `node`, and those are exactly the consumers RFC 8259 §6 does not protect.
Measured against a real ticket on `origin/main`, a stored `created_at` of `1787860170488898642`
came back from `node`'s plain `JSON.parse` as `1787860170488898600` — silently 42 ns wrong, in an
audit trail. The only values whose form changes are the ones a float64 consumer was **already**
reading incorrectly; in-range integers such as `priority` are untouched.

**Measured, per surface** (unsafe integers — `|n| > 2**53-1` — in one command's JSON output,
against a ticket carrying a comment and a signature):

1. `rebar show <id> --output json` — **4 → 0** (`created_at`, `comments[0].timestamp`,
   `signature.signed_at`, `updated_at`).
2. `rebar list --output json` — **2 → 0**; `rebar ready --output json` — **3 → 0**;
   `rebar search`, `rebar session-logs`, `rebar deps`, `rebar summary` and the `--output llm`
   projections of `show`/`list`/`ready` go through the same emitter.
3. `rebar sign --output json` — **1 → 0** and `rebar verify-signature --output json` — **1 → 0**
   (`signed_at`), plus `rebar review-plan <id> --status --output json`.
4. `rebar audit show <id> --output json` — **4 → 0** (the whole ticket state, nested under
   `$.ticket`).

**Explicitly NOT changed.** `rebar export` (NDJSON) and its library twin `rebar.export_tickets()`
still emit integers: that is a separately versioned interop projection (`schema_version`) with a
round-trip contract against `rebar import`, and retyping it needs a coordinated import-side change.
*(Superseded 2026-08-28 by bug `guilty-pusslike-wyvern` (`a8db-dc3c-983a-40b0`) — see the entry
above: export now emits the same string wire form under `schema_version` 2, with a matching `int()`
coercion on import.)*
The Python library facade (`rebar.show_ticket()` et al.) is unaffected in principle — it returns
Python `int`s, which are arbitrary-precision, and only a JSON serialization boundary can lose
digits.

**Migration for CLI consumers.** Coercion is unaffected: `int(x)` in Python and `BigInt(x)` in
JavaScript accept both forms, and `int("123") == int(123)`. What breaks is **naive raw-integer
arithmetic** on the parsed value — `.created_at + 0` in `jq`, `data.created_at - start` in Node,
or a Python comparison against an `int` without coercion. Those now fail LOUDLY (a `TypeError` in
Python, a type error in `jq`) instead of silently returning a rounded number, which is the point:
the previous behaviour corrupted the value with no signal.

**Schema.** No field was RETYPED: `ticket_state.schema.json`, `common.schema.json`,
`sign_result.schema.json`, `verify_signature_result.schema.json` and
`plan_review_status.schema.json` already type these fields `["integer", "string", ...]` and already
describe the string WIRE FORM **with no surface qualifier**. Until this change that prose was true
only of MCP; it is now literally true everywhere the schemas are wired to a `--output json`. One
schema's TOP-LEVEL description did have to change: `sign_result.schema.json` stated that `signed_at`
is "carried as an INTEGER on CLI/library output and as a decimal STRING over MCP", which this change
makes false — it now describes the single cross-surface rule.

**In-repo consumers fixed with it.** A repo-wide sweep for code that parses `--output json` and
type-checks or does arithmetic on a timestamp field found exactly TWO affected readers, both the
same shape and both fail-soft:

* `rebar._engine.rebar_reconciler.conflict_bug_filing._recent_marker_comment` — the reconciler's
  conflict-bug filer.
* `scripts/alert_dedup.recent_marker_comment` — the SHARED dedup primitive behind both scheduled
  alert lanes (`canary_bridge`, `dependency_audit`).

Each parses `rebar show --output json` and type-checked the comment `timestamp` with
`isinstance(ts, (int, float))`. A string timestamp fails that check, so the marker loop skipped
every comment, the helper always returned `False`, and the 24h accumulation cap silently stopped
matching — appending another marker comment on every pass to the ticket the lane had already
filed. Both now coerce with `int()` first, which accepts the integer and the decimal-string form
alike. This is exactly the migration these notes prescribe, demonstrated on rebar's own consumers
— and a reminder that for a FAIL-SOFT reader the failure mode is silent degradation, not the loud
error the rest of this change relies on.

Everything else surveyed is unaffected, for a structural reason rather than by luck: `metrics.bug_trends`
reads in-process via `rebar.reduce_ticket()`, `_commands.compact_plan` and `scripts/rebar_duration_probe*`
read event/sidecar files straight off disk, and `rebar_reconciler.outbound_comments` uses the
timestamp only as an opaque identity key. None of them cross a CLI JSON boundary, and the Python
library returns arbitrary-precision ints.

## BEHAVIOR CORRECTION (pre-1.0) — `fsck --output json`'s `issue_count` now agrees with the exit code

Bug `sugarcane-scrummy-arctichare` (`29c3-b025-04d7-454e`), 2026-08-28. `rebar fsck --output json`
computed `issue_count` as `len(issues)` — it counted **every** emitted `KIND:` line, including the
report-only kinds that never drive the exit code. So a JSON consumer gating on `issue_count > 0`
could reach a *different* verdict than a shell consumer gating on the exit code, against the same
store and the same run.

**What changed.** `issue_count` is redefined as the **counted subset** — the findings that respect
each check's `is_issue` flag — so it now **agrees with the exit code** (0 when the run exits 0, ≥ 1
when it exits 1). Each item in `issues[]` gains an **additive `counted` boolean**; the report-only
kinds (`push_pending`, `status_fork_resolved`, `tracker_dirty_tmp_event`, and any `warn` line) are
`counted: false` and are **excluded from `issue_count`**, while still appearing in `issues[]` so no
finding is lost. An uninitialized/absent tracker now reports a single counted `not_initialized`
issue, making its JSON payload distinguishable from a clean store's empty `issues[]` (previously
byte-identical). The change is applied consistently across the CLI `--output json`, the library
`fsck_report()`, and the MCP `fsck` tool.

**Which field, which surfaces.** `issue_count` on the `Fsck` schema (`schemas/fsck.schema.json`),
observed via CLI `rebar fsck --output json`, library `rebar.fsck_report()`, and the MCP `fsck`
tool. The field's **type is unchanged** (integer, required); only its *computed meaning* changed.
`counted` is a new optional boolean on each `issues[]` item.

**Why this is called out.** `issue_count` is a **required** field in a strong-contract schema, so
changing its value for a given store is an observable behavior change.

**What a consumer relying on the old count must do.** A consumer that wanted "how many findings did
fsck emit" (the old `len(issues)` semantics) must now compute `len(issues)` itself; `issue_count`
answers "how many counted problems" (== the exit-code verdict). No external or CI consumer was
found reading the value — this aligns the code with `docs/user-guide.md`.

## NEW — an over-budget MCP list is REFUSED with a structured error

Bug `daughterly-agitative-ocelot` (`494b-2dd3-e9d3-4fb0`), 2026-08-28. Measured against the
deployed server, one unfiltered `list_tickets` returned **94,541,551 bytes over 177 seconds**
in a single JSON-RPC result. The server never errored — the CLIENT died, and how it died was
client-specific (GitHub Copilot CLI: `Transport closed`; another client: a 219,815-char result
spilled to disk), so no caller could tell "too big" from "server died".

`list_tickets`, `ready_tickets` and `search` are now bounded at the same ~90,000-byte client
budget `_cap_workflow_payload` already enforced on the two workflow reads. Over it, the call is
**REFUSED** with `error_code="response_too_large"` naming the match count, the byte size, the
budget and a remedy — never a silently shortened list, because a short list cannot be told
from a complete one. The refusal rides the existing `install_error_guard` envelope seam, so a
client branches on a code rather than parsing prose. `search` is included because its docstring
already promised "bounded discovery results" while the tool had no size check at all.

**This is additive, not a break.** A previously-unbounded call gains a structured failure it
never had; nothing that fits today starts failing. It depends on the lean projection landing
first (`resistant-constant-nurseshark`, `98b8-5f08-1569-45cc`): without it this bound would
refuse even `list_tickets(status="open")` at 224,610 lean bytes.

**The budget is measured on the WIRE payload, not the reducer rows.** Sizing the raw dicts
under-estimates on THREE axes, and under-estimating is the dangerous direction — it passes a
payload that still overruns. At 2,855 rows: raw dicts 552,772 bytes; after
`TicketStateOut.model_validate` 1,526,327 (the model DECLARES defaults, so validation re-adds
every field the lean projection just dropped as an explicit `null`/`[]`/`""`); after
`js_safe_result` 1,537,747 (19-digit nanosecond ints become longer quoted strings) — a 178%
under-report. Third, **FastMCP emits the result twice**: a `CallToolResult` carries both
`structuredContent` and one `content` text block per row (`json.dumps(row, indent=2)`), which
together measure **2.08x** the structured object alone. The bound counts both halves, and the
regression test's oracle is the real emitted `CallToolResult` rather than a second spelling of
the bound's own arithmetic.

**The remedy is per-tool, because a remedy the tool would reject is a dead end.**
`list_tickets` names its own six filters. `search` names a more specific query, a field
predicate, or its `status`/`ticket_type`/`has_tag` arguments. `ready_tickets` accepts NO filter
arguments — only `sort` and `full`, and `full` switches shape, not which tickets come back — so
its refusal points at `next_batch(epic_id)`, the scoped view of the same unblocked work.

**`ready_tickets` runs close to the budget by design.** After the projection its 59 rows
measure 88,480 of the 90,000 bytes — about 1.7% headroom, ~1,499 bytes per row — so roughly
one more ready ticket makes the default call refuse. The budget was not inflated to mask
that: it is the client's real constraint, and raising it just moves the failure back into the
client where there is no structured signal at all. A narrower discovery-specific output model
is tracked separately as `lucky-egoistic-akitainu` (`0c99-8f2b-0de7-48f2`).

## BREAKING (pre-1.0) — the lean DISCOVERY row drops the signature material too

Story `resistant-constant-nurseshark` (`98b8-5f08-1569-45cc`), 2026-08-28. The
`include_body=False` list row previously dropped only `description` and `comments`. It now
also drops `authorship_ledger`, `attestations`, `signature` and `keyring`.

Those four fields ARE the bulk. Measured on this store (2,855 tickets) they were **88% of a
"lean" list's bytes** — `authorship_ledger` 26,241,422 B (59%), `attestations` 10,370,251 B
(23%), `signature` 2,639,114 B (6%) — so a lean list weighed 44,392,067 B at 15,548 B/row.
After the projection: 4,928,872 B at 1,726 B/row, a **9x** reduction. A list caller is
choosing a ticket; the signature record is read per-ticket via `show` / `verify-signature`.

**Which surfaces narrow, and which do not.** The projection is reached only through
`include_body=False`, and `TicketQuery.include_body` defaults **True**, so a caller that does
not ask for a lean row is unaffected. Every consumer of the lean path:

| surface | before | after |
|---|---|---|
| CLI `rebar list` | lean (bodies dropped) | lean (bodies **and** signature material dropped) |
| MCP `list_tickets` | lean (bodies dropped) — documented lean-by-default | lean (bodies **and** signature material dropped) |
| MCP `ready_tickets` | **FULL** ticket shape, no opt-out | lean-by-default; new `full=True` restores |
| library `rebar.list_tickets` | full | **unchanged** — its own `full` defaults **True** |
| `rebar validate` (2,863 rows) | full | **unchanged** — `include_body` defaults True |
| `next-batch` (62 rows) | full | **unchanged** — `include_body` defaults True |
| `show` / `show_ticket` / `search` | full | **unchanged** — a different read path entirely |

**The narrowing comes from each caller's own flag default, not from the library's.**
`rebar.list_tickets` declares `full: bool = True`, so a direct library call with no `full`
argument still returns the FULL shape. Only the CLI's `--full` (default False) and the MCP
tool's `full=False` select a lean row — which is why exactly three surfaces are listed above.

**1. MCP `ready_tickets` narrows a published default.** It had no `full` flag and returned the
whole ticket shape. It was narrowed deliberately rather than left alone: it was the one
discovery surface that disagreed with the others on its default shape, and it is the largest
unbounded read on the surface (61 ready rows, 693,556 bytes, of which ~88% is signature
material). `full=True` returns the previous bytes exactly.

**2. CLI `rebar list --output json` narrows too**, because it shares `lean_projection` through
`list_states(include_body=False)`. This is a **user-visible** change, not an agent-only one, so
it is recorded here rather than left to be discovered. Measured on `rebar list --status closed`
(2,696 rows), key counts taken from the same ticket (`0024-fa99-d5bb-49bc`) in each run:

| | keys on that row | bytes, all 2,696 rows |
|---|---:|---:|
| before | 39 | 43,661,976 |
| after (default) | 35 | 4,747,485 |
| after, `--full` | 41 | 71,707,799 |

The four keys that leave the default row are exactly `authorship_ledger`, `attestations`,
`signature` and `keyring`, and nothing else changed — a **9.2x** byte reduction. `--full`
restores all four plus the two bodies, verified against the same store.

**What a consumer must do.** If you read any of those four fields off a `list_tickets` /
`ready_tickets` / `rebar list` row, pass `full=True` (MCP) or `--full` (CLI), or read the
single ticket with `show_ticket` / `rebar show`, which are UNCHANGED and still carry every
field. Nothing was removed from any schema — all four ride `extra="allow"`, so this is a
projection, not a shape change, and the opt-out returns exactly what the default returned
before.

## BREAKING (pre-1.0) — nanosecond timestamps are STRINGS on the MCP wire

Bug `unreal-milky-sloth` (`6fe7-956f-4901-45cf`), 2026-08-27. rebar's MCP server emitted
`time.time_ns()` timestamps as bare 19-digit JSON numbers. RFC 8259 §6 guarantees that
implementations "agree exactly on their numeric values" only inside `[-(2**53)+1, (2**53)-1]`, and
every supported MCP client parses JSON numbers as IEEE-754 binary64 — so this broke both ways: a
client using plain `JSON.parse` silently truncated the value, and GitHub Copilot CLI, which parses
losslessly into a `BigInt`, failed outright with `TypeError: Do not know how to serialize a BigInt`
on `list_tickets` and `ready_tickets`.

**What changed.** On the **MCP surface only**, any integer outside the JS-safe range is now emitted
as its exact decimal **string**. CLI `--output json` and the Python library are UNCHANGED and still
emit integers. *(Superseded 2026-08-28 by bug `e127-a3ad-895a-4a2f` — see the entry above: the
same wire form now applies to CLI `--output json` as well. The library remains unchanged.)*

**Which keys.** `created_at`, `updated_at`, `last_reopened_at`, `source_created_at` and comment
`timestamp` / `source_created_at` (all optional), plus `signed_at` on `sign_result`,
`verify_signature_result` and `plan_review_status`.

**Why this is called out as BREAKING.** `signed_at` is a **required** key in those three schemas,
and `docs/api-stability.md` says required keys are not retyped. Their `type` is now
`["integer", "string"]` (plus `"null"` where it already was), so the integer form still validates and
a reader that already coerced with `int()` is unaffected. It is recorded here rather than shipped as
a silent exception, so the "strongest contract" rule stays literally true.

**What a consumer must do.** Parse these fields as an arbitrary-precision integer — `int(x)` in
Python, `BigInt(x)` in JavaScript — and accept both the integer and string forms. Do **not** use
`Number(x)`: binary64 rounds silently, with no error. The real stored value `1787856371950409998`
comes back from a plain `JSON.parse` of the old numeric form as `1787856371950410000`. Any consumer
already doing `Number(x)` was **already** reading a corrupted value; this change is what makes the
exact value reachable.

**Invariant.** The instant is unchanged and the conversion is lossless: the decimal digits are
identical to the integer form, only the JSON type differs.

## BREAKING (pre-1.0) — unreadable config errors gate resolution (no more fail-OPEN)

Operator ruling on ticket 39f8-ae7c ("Unreadable config should result in an error"),
2026-08-21. When `rebar.toml` exists but cannot be read, resolving any opt-in `verify.*`
gate (the plan-review claim gate, the completion-verification close gate) now raises
`rebar.config.ConfigError` — re-exported as `rebar.ConfigError` — with the parse fault
chained as `__cause__`, instead of silently resolving the gate to its configured default
and letting the operation proceed.

Agent-visible effects:

- `rebar claim` / `rebar transition` (CLI) fail with exit 1 and a clean
  `Error: cannot resolve <gate> for <ticket>: …` line naming the config fault.
- `rebar.claim()` / `rebar.transition()` (library) raise `ConfigError` where they
  previously proceeded; callers that catch only `RebarError` will now see the
  `ConfigError` propagate. Catch `rebar.ConfigError` to handle it.
- The ticket is left in its prior state (the error is raised before the store lock;
  no gate payload, stamp, or status write is recorded).
- A **missing** config is unchanged: absent `rebar.toml` still means defaults.

Remediation is always the same: fix the config file, then retry the operation. The
`GateState.UNREADABLE` enum member introduced by ticket f5c4-b2d1 is removed with it
(never released).

## BREAKING (pre-1.0) — the MCP gate resolvers error on an unreadable config too

The same 39f8-ae7c ruling, applied to the MCP surface (ticket 8408-54bb). The
`mcp.readonly`, `mcp.allow_llm`, and `mcp.allow_jira_sync` gate resolvers
(`rebar.config.mcp_readonly()` / `rebar.config.mcp_gate()`) now raise `ConfigError` —
chained from the parse fault, naming the gate — when the config cannot be read, instead
of silently resolving to a fallback (read-only for `mcp_readonly`, the removed `fail`
keyword's value for `mcp_gate`).

Agent-visible effects:

- An MCP tool call that hits an unreadable config fails with the structured error
  envelope carrying the new `config_unreadable` code (in `rebar.KNOWN_ERROR_CODES`),
  so a driving agent can distinguish a broken config from a deliberate read-only/off
  policy refusal and tell the operator to fix the file.
- `rebar.config.mcp_gate(attr)` drops its `fail` keyword — there is no malformed-config
  fallback to choose any more. Callers pass just the attribute name.
- The LLM runner's read-only sensing (`comment_ticket` withholding) propagates the same
  error instead of silently going read-only.
- The effective capability posture is unchanged: a broken config still never enables
  writes, billable LLM calls, or live Jira sync — the fault now surfaces loudly instead
  of reading as configuration.
- A **missing** config is unchanged: absent `rebar.toml` still means defaults.

## Library force bypass carries its audit reason on `force`

The public library lifecycle operations now share one force contract:
`force: str | None`, where `None` alone means the operation is not forced and a supplied
string is the audit reason. An explicitly empty string is still a supplied force and is
recorded as `(no reason given)`, matching a bare CLI `--force`. This applies to both
`rebar.claim()` and `rebar.transition()`.

The temporary migration aliases have now been removed before v1.0:
`rebar.transition(force=True)` and `rebar.transition(force_close="<reason>")` raise
`TypeError` naming the replacement `force="<explicit reason>"` spelling. `reason` no
longer doubles as a force note; it is only the justification for a reason-required
administrative close.

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

This is an automation-wrapper change; noun-based `rebar bridge preview` and
`rebar bridge sync` are the supported operator CLI. The later reconcile-compatibility
contraction removed top-level `rebar reconcile`, direct `--mode reconcile-check`, and direct
`--filter-local-ids`; scheduled provider adapters retain the profile spelling
`reconcile-check` only as a compatibility profile that invokes preview.

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

`bridge_fsck` retains its name, schema, return value, and error behavior. The former
Python and MCP `reconcile(mode=...)` compatibility adapters were later removed; use the
explicit bridge operations for programmatic callers. The interactive setup wizard remains a
CLI-only operator flow.

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

The expand-contract window originally retained `rebar reconcile`, direct engine `--mode`,
and `--filter-local-ids`; that compatibility window has now closed for top-level
`rebar reconcile`, direct `--mode reconcile-check`, and direct `--filter-local-ids`. Use
canonical preview for proposed changes and `bridge fsck` for offline binding/integrity audit.
See [ADR 0092](adr/0092-bridge-primary-vocabulary-compatibility-adapters.md) for the original
compatibility decision.

The production workflow maps its retained profiles exactly: `reconcile-check` →
`bridge preview`, `dry-run` → `bridge preview`,
`bootstrap-strict` → `bridge sync --max-changes 10`, `bootstrap-throttle` →
`bridge sync --max-changes 100`, and `live` → `bridge sync`.

Canonical `bridge preview` / `bridge sync` (including the direct-engine canonical verbs) now
expose only 0 success/benign, 1 operational failure, and 2 invalid invocation/configuration.
Converged, paused, another-pass-in-flight, reschedule, and the historical phase-gate outcome
are benign canonical exit 0 states with a stable one-line state marker. The engine still
classifies and executes a single pass; route adapters translate only the final status/message,
so repository and ref effects are identical.

The remaining direct-engine compatibility route keeps argument-less live behavior and the
supported rollout modes (`dry-run`, `bootstrap-strict`, `bootstrap-throttle`, `live`). The
removed `reconcile-check` diagnostic and `--filter-local-ids` CLI surface now reject; callers
should move to explicit bridge operations. The production workflow no longer carries a 3/75
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
MCP. Current releases have also removed the library `force_close` kwarg; use the canonical
reason-carrying `force` parameter.

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
