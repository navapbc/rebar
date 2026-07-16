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
| `CREATE` | `ticket-create.sh` | Seeds `ticket_type`, `title`, `parent_id`, `priority`, `assignee`, `description`, `tags`, and MAY carry an optional `status` (the reducer defaults it to `open` when absent). A non-`open` genesis `status` is produced ONLY by the `rebar idea` command/`create_idea` MCP tool/`rebar.idea(...)` library entry — which emit a single CREATE with `status=idea` for an `epic`, so an idea is born in `idea` (never momentarily `open`/claimable) with no intervening `STATUS` event. There is no general `create --status` flag. Exactly one CREATE per ticket (fsck checks presence). Valid `ticket_type`s: `task`, `story`, `bug`, `epic`, `session_log`. **`session_log`** is a verbose-log type that is gate/lifecycle-exempt, excluded from the graph/health compiles (via `reduce_all_tickets(exclude_session_logs=…)`) and default `list`, never synced to Jira, and refuses `STATUS` (no claim/transition) — see CLAUDE.md "Session logs". |
| `STATUS` | `_commands/txn.py` (transition/claim) | Sets `status`; carries `current_status` (the optimistic-concurrency expectation) and `parent_status_uuid` (the prior STATUS uuid) for fork resolution. |
| `EDIT` | `ticket-edit.sh`, `_commands/txn.py` (claim) | Merges `data.fields` (title/priority/assignee/description/parent) into state (last-writer-by-replay-order). **Tags are no longer mutated via `EDIT` (P2.3)** — historical `EDIT.fields.tags` still replays as the base, but no upgraded writer emits it; use `TAG_DELTA`. |
| `TAG_DELTA` | `edit --add-tag/--remove-tag/--set-tags`, `tag`/`untag`, Jira inbound applier | Convergent tag add/remove deltas: `data.{added[], removed[]}` mutate the current `tags` in replay order (remove-then-add, so **add wins** on an intra-event conflict; idempotent). Replaces the whole-field `EDIT.tags` clobber so two clones adding different tags both survive. `--set-tags` is compiled to a delta vs observed tags (add-wins). The inbound reconciler marks `data.source="inbound"` so `local_label_intent` excludes it from user-intent. |
| `COMMENT` | `ticket-comment.sh` | Appends `{body, author, timestamp}` to `comments`. |
| `LINK` / `UNLINK` | `ticket-graph.py` / `ticket-link.sh` | Add / cancel a relation. Relations: `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`, `discovered_from` (`graph/_links.py:CANONICAL_RELATIONS`). `relates_to` is reciprocal; the rest are directional. Only `blocks`/`depends_on` can create cycles. **Hierarchy promotion:** for `blocks`/`depends_on` only, the recorded endpoints are promoted up the parent hierarchy so the dependency is between comparable levels (epic↔epic, story↔story, task/bug↔task/bug), emitting a `REDIRECT: A→B promoted to …` note; the other (non-blocking) relations are recorded exactly as given. `UNLINK` is pair-scoped (no relation arg) and cancels the most-recent link for an ordered `<source> <target>` pair, one per event — and must target the *promoted (ancestor)* endpoint to cancel a promoted blocking link. A `session_log` endpoint refuses `blocks`/`depends_on` (it never enters the dependency graph) but permits the non-blocking `relates_to`/`discovered_from`. |
| `FILE_IMPACT` | `set-file-impact` | Records the `{path, reason}` array `next-batch` uses for conflict-aware scheduling. |
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
real gating.) There is **no** VERSION event and no version negotiation —
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
