# rebar event schema

Every ticket is a directory under the `tickets` orphan branch worktree
(`.tickets-tracker/<ticket_id>/`); every mutation is one append-only JSON **event
file** in it. State is computed by replaying the events (the reducer,
`src/rebar/reducer/`). Nothing is ever stored as compiled state
except the local, rebuildable `.cache.json` (gitignored — see docs/concurrency.md).

> This document is the **event** (write) schema. The **output** (read) contract —
> the replay-derived JSON shapes that `show`/`list`/`deps`/… emit, each backed by
> a JSON Schema under `src/rebar/schemas/` — is documented separately in
> [output-schemas.md](output-schemas.md). Outputs are compiled state, not events.

## Filename contract (I2)

```
${timestamp_ns}-${uuid}-${TYPE}.json
```

- `${timestamp_ns}` — a single-integer **Hybrid Logical Clock** prefix (P2.1,
  `rebar._store.hlc.next_tick`): `max(per-clone cache, the target ticket's
  witnessed max-prefix, time_ns()) + 1`. It tracks wall-clock ns but never ties or
  inverts for causally-related events from one actor (the `+1` floor), so replay
  order is **skew-immune and causal**, not merely best-effort wall-clock. It stays
  a 19-digit integer (until ~year 2286), and ordering compares prefixes **as
  integers** (`reducer/_sort.prefix_ts`) so legacy ns names and HLC names form one
  global order regardless of width. Staged behind `REBAR_HLC` (default-on;
  `REBAR_HLC=0` reverts to raw `time.time_ns()`). The clock is >2^53, so jq must
  never read or compute on it (P1.0 keeps jq out of the event path).
- `${uuid}` — a fresh UUID4 per event; makes every filename globally unique, so
  two clients writing concurrently never collide and git merges the two files as
  a union (`ticket-lib.sh:85`, `_commands/txn.py`).
- `${TYPE}` — the event kind (below).

Dotfiles (`.cache.json`, `.tombstone.json`, `.env-id`, …) are NOT events and are
excluded from replay (the reducer globs `*.json` and skips names starting with `.`).

**New event kinds MUST use this scheme** and append-only semantics (I1).

## Event types

Replay dispatch: `reducer/_processors.py` (`process_*`).

| TYPE | Written by | Effect on replayed state |
|------|-----------|--------------------------|
| `CREATE` | `ticket-create.sh` | Seeds `ticket_type`, `title`, `parent_id`, `priority`, `assignee`, `description`, `tags`, an UNCONDITIONAL `creation_channel` (the ingress that produced this genesis — one of `cli`/`mcp`/`python`/`jira`/`import`; see "Creation-channel provenance" below), and MAY carry an optional `status` (the reducer defaults it to `open` when absent). A non-`open` genesis `status` is produced ONLY by the `rebar idea` command/`create_idea` MCP tool/`rebar.idea(...)` library entry — which emit a single CREATE with `status=idea` for an `epic`, so an idea is born in `idea` (never momentarily `open`/claimable) with no intervening `STATUS` event. There is no general `create --status` flag. Exactly one CREATE per ticket (fsck checks presence). Valid `ticket_type`s: `task`, `story`, `bug`, `epic`, `session_log`. **`session_log`** is a verbose-log type that is gate/lifecycle-exempt, excluded from the graph/health compiles (via `reduce_all_tickets(exclude_session_logs=…)`) and default `list`, never synced to Jira, and refuses `STATUS` (no claim/transition) — see [The session_log ticket type](#the-session_log-ticket-type) below. |
| `STATUS` | `_commands/txn.py` (transition/claim) | Sets `status`; carries `current_status` (the optimistic-concurrency expectation) and `parent_status_uuid` (the prior STATUS uuid) for fork resolution. |
| `EDIT` | `ticket-edit.sh`, `_commands/txn.py` (claim) | Merges `data.fields` (title/priority/assignee/description/parent) into state (last-writer-by-replay-order). **Tags are no longer mutated via `EDIT` (P2.3)** — historical `EDIT.fields.tags` still replays as the base, but no upgraded writer emits it; use `TAG_DELTA`. |
| `TAG_DELTA` | `edit --add-tag/--remove-tag/--set-tags`, `tag`/`untag`, Jira inbound applier | Convergent tag add/remove deltas: `data.{added[], removed[]}` mutate the current `tags` in replay order (remove-then-add, so **add wins** on an intra-event conflict; idempotent). Replaces the whole-field `EDIT.tags` clobber so two clones adding different tags both survive. `--set-tags` is compiled to a delta vs observed tags (add-wins). The inbound reconciler marks `data.source="inbound"` so `local_label_intent` excludes it from user-intent. |
| `COMMENT` | `ticket-comment.sh` | Appends `{body, author, timestamp}` to `comments`. |
| `LINK` / `UNLINK` | `ticket-graph.py` / `ticket-link.sh` | Add / cancel a relation. Relations: `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`, `discovered_from`, `caused_by` (`graph/_links.py:CANONICAL_RELATIONS`). `relates_to` is reciprocal; the rest are directional. `caused_by` is directional, non-blocking, and non-cycle-inducing. Only `blocks`/`depends_on` can create cycles. **Hierarchy escalation:** for `blocks`/`depends_on` only, the endpoints must be siblings — comparability is structural (position in the parent hierarchy), never by ticket type. Endpoints that do not share a parent are escalated to the children of their nearest common ancestor, emitting a `REDIRECT: A→B promoted to …` note; parentless tickets count as siblings of one another, and an ancestor/descendant pair is refused outright; the other (non-blocking) relations are recorded exactly as given. The `unlink <source> <target> [relation]` operation selects the most-recent net-active link for the ordered pair when relation is omitted, or exactly the canonical relation when supplied (leaving the pair's other active relations untouched); it must target the *promoted (ancestor)* endpoint to cancel a promoted blocking link. At the wire level, the emitted `UNLINK` event cancels that selected `LINK` by `link_uuid` and need not carry a relation selector itself. A `session_log` endpoint refuses `blocks`/`depends_on` (it never enters the dependency graph) but permits the non-blocking `relates_to`/`discovered_from`/`caused_by`. |
| `FILE_IMPACT` | `set-file-impact` | Records a tri-state file-impact declaration for conflict-aware scheduling: paths, explicit none, or undeclared from an absent or empty non-none declaration. |
| `VERIFY_COMMANDS` / `PRECONDITIONS` | `set-verify-commands` / preconditions util | Record DD-level verify commands / precondition metadata. |
| `SIGNATURE` | `sign` (`rebar.signing`) | Records `data.{manifest, algorithm, signature, key_id, head_sha, signed_at}` — an HMAC-SHA256 attestation over a ticket's **manifest of verified steps**, computed with the **environment-specific** signing key (`REBAR_SIGNING_KEY` or the gitignored `.signing-key`). Replayed into `state['signature']` (last-writer-wins, like FILE_IMPACT/VERIFY_COMMANDS). `verify-signature` recomputes the HMAC with the local key and certifies the steps match — `key_id` (a key fingerprint, never the key) lets verification distinguish a tampered manifest from a signature made by a *different* environment. Like FILE_IMPACT/VERIFY_COMMANDS this is last-writer-wins by replay (filename) order, so concurrent signs converge deterministically to the lexicographically-last `{ts}-{uuid}-SIGNATURE.json` (the UUID breaks any timestamp tie) — not to a semantically "best" signature; re-sign to supersede. |
| `ARCHIVED` | `archive` / lifecycle | Marks the ticket archived (excluded from the default list). |
| `SNAPSHOT` | compaction (`ticket-compact.sh`) | Folds a run of prior events into one compiled-state event under the write lock; the folded files are renamed `*.retired` (I1's only exception). `data.source_event_uuids` lists what it folded (fsck cross-checks this). |
| `WORKFLOW_RUN` | workflow executor (`rebar.llm.workflow`) | Per-`run_id` last-writer-wins into `state.workflow_runs[run_id]`. Each event carries the COMPLETE current run record (status, timing, inputs, captured now/uuid for deterministic replay); replay keeps the last event per `run_id` by HLC+UUID filename order, so concurrent runs converge with no extra tie-break. The map is created lazily, and only the one `run_id` key is replaced (two runs on one ticket never clobber). Never synced to Jira. |
| `WORKFLOW_STEP` | workflow executor (`rebar.llm.workflow`) | Per-`(run_id, step_id)` last-writer-wins into `state.workflow_steps[run_id][step_id]`. A step's idempotency marker + result, committed AFTER the step's effect, carrying the full per-step record (status, outputs, error, captured non-determinism); a retry's later event supersedes the earlier one. Lazy + per-key like `WORKFLOW_RUN`. Never synced to Jira. |
| `COMMITS` | `attach_commits` (`rebar.attach_commits`) | Unions commit records into `state.commits` (used by the code-review workflow). `data.commits` is a list of SHAs or `{sha, message?, author?}` records, deduplicated by `sha` (first-in-replay-order wins); union-add is order-insensitive, so all clones converge. Restored verbatim by `SNAPSHOT` (survives compaction). Never surfaced to Jira (the outbound differ does not read `commits`). |
| `BRIDGE_ALERT` / `REVERT` / `SYNC` | reconciler / revert | Jira-bridge alerting, event reversal, and bridge sync bookkeeping. |

*The "Written by" column names the **historical bash writers** (`ticket-create.sh`,
`ticket-edit.sh`, `ticket-comment.sh`, `ticket-graph.py`, `ticket-link.sh`,
`ticket-compact.sh`, `ticket-lib.sh`, …), which have since been migrated to the
in-process Python write path (`rebar._commands` leaf/lifecycle writers +
`rebar._store` append/commit, with `rebar.reducer`/`rebar.graph` for replay/relations).
It identifies the originating event producer, not a current file path.*

## File-impact declarations (`FILE_IMPACT`)

`FILE_IMPACT` replaces the ticket's file-impact declaration by replay order (last writer wins).
Every event carries `data.file_impact`; an explicit no-impact declaration also carries
`data.file_impact_scope: "none"` and `data.no_file_impact_reason`. No event, or an empty
FILE_IMPACT event without those explicit none fields, is undeclared. Replay exposes all three
derived fields—`file_impact`, `file_impact_scope`, and `no_file_impact_reason`—in exactly one
of these states:

- **paths:** a non-empty `file_impact`, `file_impact_scope: "paths"`, and an empty
  `no_file_impact_reason`.
- **none:** `file_impact: []`, `file_impact_scope: "none"`, and the substantive
  `no_file_impact_reason`.
- **undeclared:** `file_impact: []`, `file_impact_scope: "undeclared"`, and an empty
  `no_file_impact_reason`.

The state is not additive: a later event supersedes the earlier declaration completely.
Transitioning to paths clears a prior reason, transitioning to none clears prior paths and sets
the reason, and transitioning to undeclared clears both. Documentation-only and test-only
tickets must use **paths** because they do change repository files; `none` is reserved for
work whose output or evidence lives outside the repository.

## The session_log ticket type

`session_log` is a first-class ticket type (one of the valid `ticket_type`s in the
`CREATE` row above) for **verbose, durable, agent-facing logs** stored in the rebar
store and surfaced later by keyword. It is deliberately kept out of the
dependency-graph / store-health hot paths so its large bodies never tax the
operations that run constantly during the parallel-agent workflow. Its behavior
differs from the work types:

- **Gate- and lifecycle-exempt.** `clarity_check` / `check_ac` / `quality_check`
  treat it as exempt (always pass), and `validate` never flags it (orphan / empty /
  etc.). It **cannot** be `claim`ed or `transition`ed — it refuses the `STATUS`
  event; `show`, `comment`, and `edit` work normally.
- **Visibility — searchable, hidden from `list`.** Included in keyword `search` and
  in single-ticket `show`, and listed by `recent_session_logs` /
  `list_tickets(ticket_type="session_log")`, but **excluded** from the default `list`
  and from `ready` / `next_batch` / `deps` / `validate` (the graph/health compiles,
  via `reduce_all_tickets(exclude_session_logs=…)`), so log size and count never
  affect those hot paths.
- **Non-blocking links only.** `relates_to` / `discovered_from` are permitted on a
  `session_log` endpoint (so a log can reference the work it documents); `blocks` /
  `depends_on` are **refused** on either endpoint, and a log never enters the
  dependency graph (see the `LINK` / `UNLINK` row above).
- **Never synced to Jira.** `reconcile` excludes `session_log` (it is in the
  reconciler's `EXCLUDED_SYNC_TYPES` and absent from the local→Jira type map), and it
  never appears in `bridge_fsck`.
- **Title convention (guidance, NOT enforced).** Titles should carry a short summary
  of the work, not merely a date / time / session id; nothing validates this.

Capturing and retrieving session logs — the `rebar session-log` helper, the local
git-ignored `.rebar/current_session_log` pointer, and the per-session auto-rotation
behavior — is documented for day-to-day use in [user-guide.md](user-guide.md).

## Schema version & forward compatibility

The event log is the **wire format between clones running different rebar
versions** — they share one `origin/tickets` and merge each other's event files
as a union. The format carries an explicit version constant:
`reducer/_version.py: SCHEMA_VERSION` (see that file for the current value). Bump it
when the wire format changes in a way other clones must be aware of. (v2 = P2.1: the
filename prefix became a single-integer HLC value; same width and encoding, so
older clones still string-compare correctly — the change is semantic ordering, not
a body change. v3 = P2.3: the new `TAG_DELTA` event body — the **first** bump that
adds a new event *type*, so it is the first to actually exercise the unknown-type
forward-compat rule below; the integer is declarative, `KNOWN_EVENT_TYPES` does the
real gating. v4 = epic gnu-whale-ichor / e165: the new `KEY_ADD` / `KEY_REVOKE`
event bodies fold an identity's signed key lifecycle (TOFU genesis, signed
add/revoke) into an epoch-scoped keyring; like `TAG_DELTA` this adds new event
*body* types, so it engages the unknown-type forward-compat path — an older (v3)
clone preserves-and-ignores a `KEY_ADD`/`KEY_REVOKE` (the file survives, the
keyring mutation is invisible until it upgrades). v5 = epic gnu-whale-ichor: the
keyring becomes **position-based** — each record is `{public_key, added_at,
revoked_at}` where a position is the event's `{timestamp}-{uuid}` filename prefix
(an immutable anchor a verifier resolves to the introducing tickets-branch
commit), replacing the author-assignable `added_epoch` / `revoked_epoch` ordinal
cursor; no new event *body* type (`KEY_ADD`/`KEY_REVOKE` are unchanged on the
wire), so the forward-compat path is not engaged — the bump records the projection
change so clones agree on the keyring shape.) There is **no** VERSION event and no version negotiation —
cross-version safety is handled by a single rule:

**Unknown event types are preserved-and-ignored.** `KNOWN_EVENT_TYPES`
(`_version.py`) is the canonical set of types the reducer's replay dispatch
applies — the `TYPE` rows above, minus the externally-scanned `PRECONDITIONS`
(handled by `_compute_preconditions_summary` + its own `compact_preconditions`,
not the main replay) and the bridge-only `SYNC`. An event whose `event_type` is
**not** in that set was written by a newer rebar, and is handled two ways:

- **ignored** at the state level — `_processors.replay` skips it without error,
  so the ticket stays fully readable on the older clone;
- **preserved** at the file level — `ticket-compact.sh` never folds it into a
  SNAPSHOT nor deletes it, so an older clone's compaction cannot destroy a newer
  clone's data. (The same treatment `*-SYNC.json` and `*-PRECONDITIONS*.json`
  files already get.)

**Detectability + rollout (P2.3).** Preserve-and-ignore keeps an old clone from
*corrupting* the store, but the new event's effect is *invisible* there until it
upgrades — e.g. an un-upgraded **reconcile host** would reduce without `TAG_DELTA`
and push a stale tag set to Jira. `fsck` therefore emits a `WARN` when the store
contains event types newer than the running binary ("upgrade rebar"), so the
window is detectable rather than silent. **Deployment rule: upgrade
reconciler-running clones FIRST** when rolling out a new event type.

Pinned by `tests/interfaces/contracts/test_event_schema_forward_compat.py`.

## Replay & fork determinism

- Events replay in `${timestamp_ns}` filename order, compared **as integers**
  (`reducer/_sort.event_sort_key` → `prefix_ts`); the reducer is pure
  (deterministic given the file set).
- **HLC causal order (P2.1).** Because the prefix is a Hybrid Logical Clock
  (above), COMMENT/EDIT ordering by prefix is now **causal and skew-immune**: a
  clone that observed another clone's event before writing witnesses its prefix
  and ticks strictly after it, so concurrent same-field edits converge to the same
  value on every clone (no last-wall-clock-writer clobber). This generalizes I8
  beyond STATUS forks.
- **STATUS forks** (two STATUS events sharing a `parent_status_uuid` — e.g. two
  clients transitioning the same ticket concurrently) are resolved **skew-
  independently by the lexically-lower event UUID** (`_processors.py` `process_status`),
  kept as defense-in-depth for exact-equal prefixes, so every clone converges to
  the same winner regardless of clock skew or replay order (invariant I8).

## Session provenance (`claimed_session`)

A claim / bare `open -> in_progress` transition additively stamps the claiming
coding-agent session id onto its `STATUS` event as **`data["session"]`** (epic
crust-fetch-stump). The id is produced by the shared resolver
`resolve_session_id()` with ordered precedence
`REBAR_SESSION_ID -> CLAUDE_CODE_SESSION_ID -> SESSION_ID -> None` (first non-empty;
never git HEAD — see [`docs/config.md`](config.md) "Session provenance"). When no
session is present the key is **omitted**, so the event bytes are identical to the
pre-feature path.

The reducer (`_processors.py` `_fold_claimed_session`) folds `data["session"]` into
the compiled-state key **`state["claimed_session"]`** on the `open -> in_progress`
edge only (mirrors `assignee`; enumerated in `ticket_state.schema.json` and
`rebar.types`). It is applied only when the incoming event's status is applied — so
in a STATUS fork it follows the **lexical-UUID winner** and a losing concurrent claim
never overwrites it; a session-less re-claim folds `None`, clearing any stale prior id.

**Multi-harness provenance (story c557).** The same `open -> in_progress` STATUS additively
carries two more opaque keys when present: `data["harness"]` (from the rebar-owned `AI_AGENT`
convention var, the tool base name `claude-code` / `opencode` / `codex` / `cursor`, optionally
`_<version>`-suffixed)
→ `state["claim_harness"]`, and `data["remote_session"]` (from `CLAUDE_CODE_REMOTE_SESSION_ID`)
→ `state["claim_remote_session"]`. Both fold on the same edge with the same fork-winner gating
and session-less-clear semantics as `claimed_session`, are defaulted in `make_initial_state`,
and are enumerated in `ticket_state.schema.json` (+ `rebar.types`) and the LLM schema
(compact keys `chn` / `rsn`). The resolver var list is extended to
`REBAR_SESSION_ID -> CLAUDE_CODE_SESSION_ID -> OPENCODE_SESSION_ID -> SESSION_ID` (Codex has no
readable session var; it uses its `REBAR_SESSION_ID` shim — see [`docs/config.md`](config.md)).

**Forward/back-compat + compaction.** `data["session"]` is an additive data key; an
older clone's reducer ignores it (it reads only `status` / `current_status`). A
post-feature `SNAPSHOT` carries `claimed_session` in its `compiled_state` and restores
it verbatim; a pre-feature snapshot lacks the key and restores to an explicit `None`
(the `process_snapshot` guard), so a snapshot-served state and a fresh-replay state
agree. The key never enters the Jira reconciler (local reducer-state only).

## Creation-channel provenance (`creation_channel`)

Every genesis `CREATE` records **which public interface produced it** as an additive,
immutable `data["creation_channel"]` (epic jira-reb-977, story 6fe2). Unlike the
present-only `source_*` provenance keys, it is stamped **unconditionally** on the CREATE
`data`, and the reducer (`_processors.py:process_create`) projects it into compiled state
as `state["creation_channel"]` (enumerated in `ticket_state.schema.json` + `rebar.types`).

**Closed six-value enum** (`common.schema.json#/$defs/creation_channel`, mirrored by the
runtime `rebar.reducer._version.CREATION_CHANNELS`; a contract test pins the two in sync):

| value | meaning |
|-------|---------|
| `cli` | the `rebar` CLI (`create` / `idea` / `identity create` / `session-log`) |
| `mcp` | the MCP server's write tools (`create_ticket` / `create_idea` / `create_identity` / `log_session`) |
| `python` | a direct `rebar.*` library call (the default at the library boundary) |
| `jira` | Jira-inbound attribution: the reconciler's inbound materialization path stamps `jira` on the CREATE it writes directly for an imported Jira issue (and on a Jira-minted placeholder assignee identity). Story e622. |
| `import` | NDJSON `rebar import`: the fresh LOCAL ticket an import creates records `import` — regardless of the channel the exported source record carried (that origin lives on in `source_*`, not in `creation_channel`). Story e622. |
| `unknown` | **projection-only fallback**: a legacy `CREATE` that carried no field reduces to `unknown`. NEVER a valid live-write value — `validate_creation_channel` rejects it, so no writer may stamp it |

**Default-per-interface.** The three local ingresses all converge on
`composer.create_core`, which now REQUIRES the channel (keyword-only, no default) so each
converging caller must declare it: the CLI helpers pass `"cli"`, the MCP adapter threads
`"mcp"` through a private `_creation_channel` keyword on the `rebar.*` facade (kept out of
the documented public signature), and a direct library call takes the facade's `"python"`
default. `create_identity_core` / `ensure_identity_for` carry a `creation_channel="python"`
default that `identity_cli` overrides to `"cli"`, and the reconciler's inbound assignee
mint overrides to `"jira"` (story e622).

**Jira-inbound + import are RECORDED, not inferred (story e622).** Two writers bypass
`composer.create_core` and stamp the channel on the CREATE `data` themselves:

- **Inbound Jira.** `apply_inbound_events._inbound_create_write_create_event` assembles
  the CREATE `data` directly and writes it via `_write_event_file`; it now sets
  `data["creation_channel"] = validate_creation_channel("jira")` (validated against the
  same closed vocabulary `create_core` enforces). Because this is a real recorded value,
  it carries **no** `creation_channel_inferred` marker.
- **NDJSON import.** `_io/_provenance.create_kwargs` pins `_creation_channel="import"`, so
  the imported ticket's genesis CREATE records `import`. This is deliberately independent
  of the exported source record's own channel — an imported ticket is a *fresh local
  creation through the import ingress*; the foreign store's identity is preserved only as
  `source_*`, never copied into `creation_channel`.

**Legacy-Jira inference (`creation_channel_inferred`).** A Jira-originated CREATE written
*before* this feature carried no channel and would provisionally project `unknown`. The
reducer's `_processors._project_legacy_creation_channel` (called at the end of
`process_create`, and only when the CREATE recorded **no** channel) upgrades that
projection to `jira` — and sets `state["creation_channel_inferred"] = True` — **only** when
the genesis envelope bears the exact legacy-Jira signature: the ticket id starts with
`jira-` **and** the author equals `LEGACY_JIRA_AUTHOR` **and** the env_id equals
`LEGACY_JIRA_ENV_ID` (both `"reconciler"`, defined once in `reducer/_version.py` and reused
by the reconciler writer so the signature can never drift). On **any** near-miss — a
non-`jira-` id, a wrong/missing author, or a wrong/missing env_id — the channel stays
`unknown` and no marker is set. A post-feature CREATE that recorded a real channel is never
touched.

> **Trust boundary — `creation_channel_inferred` is heuristic AUDIT metadata, NOT a
> security attestation.** A recorded channel (`jira`/`import`/`cli`/`mcp`/`python`) reflects
> the actual ingress; an *inferred* `jira` is a best-effort backfill for pre-feature
> history. The inference reads ONLY the immutable genesis envelope (ticket_id / author /
> env_id) — never tags, bindings, `source_*`, comments, or any mutable state — so it yields
> zero false positives, but it is not a cryptographic proof of origin and must not be relied
> on as one. When origin trust matters, use the signed-attestation machinery, not this flag.

**Immutability.** `creation_channel` (and the later-story marker
`creation_channel_inferred`, a `{"const": true}` flag) are in
`_processors.py:_IMMUTABLE_EDIT_FIELDS`, so `process_edit` skips them — an `EDIT` can never
overwrite genesis provenance. Other specialized processors never assign these fields.

**How it differs from actor/environment/`source_*` provenance.** `author`/`author_id` and
`env_id` record **who** wrote the event and in **which environment**; `source_*` records
**where an imported ticket came from** in a foreign store. `creation_channel` records
**which of rebar's own interfaces** the genesis write came through — an orthogonal axis
that is present on every ticket, not only imported ones.

**Forward/back-compat + compaction.** The key is additive: an older clone's reducer simply
projects `unknown` for a CREATE that predates the field, and ignores nothing (it reads
`data` generically). The key never enters the Jira reconciler (local reducer-state only).

**Where the provenance lives in a `SNAPSHOT` (survives compaction + rebuild).** Genesis
`creation_channel` provenance lives in **`SNAPSHOT.data.compiled_state`** — the reduced
ticket state the compactor folds — **not** in the `SNAPSHOT` envelope. The envelope's own
`author` / `env_id` identify the **compactor** (whoever ran `compact`), so they must never be
read as the ticket's genesis channel. Compaction preserves the recorded value:
`_snapshot_strip_keys()` (`compact.py`) strips only `{updated_at, signature}`, so both
`creation_channel` and `creation_channel_inferred` ride into `compiled_state` and
`process_snapshot` restores them verbatim on SNAPSHOT-only replay. A full-log rebuild
(`rebuild_snapshot_from_full_log`, `include_retired=True`) re-projects the same value by
replaying the retained `.retired` CREATE. A **pre-feature** `SNAPSHOT` (compacted before the
field existed) carries no channel in its `compiled_state`; because a SNAPSHOT-only replay has
no CREATE to re-infer from, `process_snapshot` **re-infers at restore time** (story 568c) —
the exact legacy-Jira signature (`jira-*` id + `reconciler` author/env_id) yields
`jira` + `creation_channel_inferred=true`, every other case yields `unknown`. The
`"creation_channel" not in compiled_state` guard makes this migration touch ONLY pre-feature
snapshots, so a recorded channel is never clobbered. `fsck` flags such a stale snapshot as
`SNAPSHOT_STALE_CHANNEL` when a retained CREATE exists, and `fsck --repair-snapshots` rebuilds
it to persist the channel durably.

## Provider provenance (`provider_provenance`)

A signed gate verdict records the model it ran under as a plain string (`"model"`). That string
alone cannot distinguish a verdict produced against the direct-Anthropic API from one produced
behind a gateway or a different provider hosting the same model family, so a consumer could not
tell a Bedrock-backed `LLM-Review` from an Ollama-backed one even though rebar signs both.

Gate sidecar payloads therefore carry an **additive, optional** `provider_provenance` object
alongside the existing `model` string, written by all three gate sidecars (plan-review,
completion, code-review):

| Key | Meaning |
| --- | --- |
| `provider` | the resolved provider name (e.g. `anthropic`, `bedrock`) |
| `model` | the resolved model string, verbatim |
| `endpoint_host` | the HOST of a configured `base_url`, or `null` when none is set |
| `tier` | `first_class` when rebar vouches for the route, `best_effort` behind a custom endpoint |
| `capabilities` | the EFFECTIVE `ModelCapabilities` record that drove the run |

**Additive and optional, deliberately.** The pre-existing `model` string is unchanged, and readers
that do not know the key ignore it — so this is a backward-compatible payload change requiring no
store-compatibility capability gate and no migration. A payload written **without**
`provider_provenance` (any record produced before this field existed) still loads and its
attestation still verifies; a reader wanting the provenance treats its absence as "unknown,
legacy" rather than as an error.

**It is not part of the signed bytes.** Signing binds `(ticket_id, manifest)`, where the manifest
is a list of `"key: value"` lines — the payload is the queryable sidecar record, a separate
artifact. So adding this key cannot invalidate an existing signature.

**No credential material is ever recorded.** `endpoint_host` is derived with
`urlparse(base_url).hostname`, which strips any `user:password@` userinfo (unlike `.netloc`, which
retains it), and neither the configured `api_key` nor any part of a credential-bearing `base_url`
appears anywhere in the persisted payload.

## Compaction (I9)

Compaction runs under the per-clone write lock, writes a `SNAPSHOT` that folds
the events it retires, and renames the folded files to `*.retired`. A remote
clone appending a new (unique-named) event merges as a union; the SNAPSHOT must
already fold any event its result depends on. Never retire an event a
not-yet-folded state could still need. Compaction folds only events of a
**known** type (`KNOWN_EVENT_TYPES`); unknown-type events (forward-compat payload
from a newer rebar) are skipped — left on disk, never folded or deleted — per the
schema-version rule above.

See `docs/concurrency.md` for the I1–I9 invariants and the merge-as-union
sync/reconvergence algorithm, and `docs/architecture.md` for the components.
