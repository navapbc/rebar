# rebar remediation — detailed implementation plan

*Companion to [`oss-comparison-and-remediation.md`](oss-comparison-and-remediation.md).
That document argues **what** to fix and **why**; this one specifies **how** —
concrete seams, schema/wire impact, invariant (I1–I9) compliance, test plans,
rollout, and risk controls. Scope: the recommended cut line, **Phase 1 (P1.1–P1.4)
+ Phase 2 (P2.1–P2.2)**. Phases 3–4 are sketched at the end for sequencing only.*

## Ground rules every work item must satisfy

1. **Invariants are a merge gate.** No new committed shared-mutable file, no new
   cross-client lock (I3/I6/I7). New per-clone state is gitignored and
   rebuildable. New write events flow through the *one* locked path
   (`_store/event_append.py` / bash `_flock_stage_commit`) — no side channels (I5).
2. **Two writers, one format.** Writes originate from **both** the bash engine
   (`ticket-lib.sh`) and the Python committer (`_store/event_append.py`); the
   reconciler uses the Python path. Any change to event *shape* or *filename* must
   land in **both** and keep `tests/scripts/test-ticket-write-commit-event.sh`
   byte-parity (`jq -S -c` == `json.dumps(sort_keys, compact)`).
3. **Three interfaces, one behavior.** Library (`src/rebar/__init__.py`), CLI
   (`src/rebar/_cli/__init__.py` + `_cli/help/*.txt`), and MCP
   (`src/rebar/mcp_server.py`) must stay at parity (`tests/interfaces/`).
   Read-side additions need an output JSON Schema (`src/rebar/schemas/`) and a
   golden.
4. **Forward compatibility.** Anything an older clone could choke on goes through
   the preserve-and-ignore machinery (`reducer/_version.py:KNOWN_EVENT_TYPES`,
   `SCHEMA_VERSION`), pinned by `tests/interfaces/test_event_schema_forward_compat.py`.

---

## Phase 1 — quick wins (additive, no wire-format break)

### P1.1 — Query upgrade (close gotcha G5)

**Goal.** Field-scoped predicates, `OR`, negation, and caller-controlled sort in
`search`/`list`, reusing the in-memory reducer path (read-only; I3 clean).

**Seams.**
- `src/rebar/reducer/search.py` — `search_states()` is today substring-AND only.
  Add a small query parser in a new `reducer/_query.py` (keep `search.py` < cap):
  tokenize into terms; recognize `field:value` predicates
  (`status:`, `type:`, `priority:`, `assignee:`, `tag:`, `parent:`), a leading
  `-`/`not:` negation, and `OR` between groups (default `AND`). Free text still
  matches the existing `_haystack`. Unknown `field:` → treat as literal substring
  (no breakage).
- `src/rebar/reducer/_filters.py` — already has the predicate logic for the
  fixed flags; expose a single `match_predicate(state, field, op, value)` and
  have both `_query.py` and the existing `apply_ticket_filters` call it (one
  comparison vocabulary, no divergence).
- `--sort` — add to the read facade `src/rebar/_engine_support/reads.py` and
  `_reads.py`. Allowed keys: `priority`, `created`, `updated`, `id`, `status`;
  `-key` for descending. Default sort unchanged (stable) so existing goldens hold.

**Interface surface.**
- CLI: new `--sort`/`-s` on `list`/`search`/`ready`; query string already free-form
  so predicates need no new flag. Update `_cli/help/list.txt`, `search.txt`.
- Library: `list_tickets(..., sort=None)`, `search(query, ..., sort=None)`.
- MCP: same kwargs on `list_tickets`/`search` tools; bump their `inputSchema`.

**Wire/schema.** None (read-only). No `SCHEMA_VERSION` bump.

**Tests.** `tests/unit` parser unit tests (predicate/OR/negation/precedence);
`tests/interfaces/test_parity.py` add a query+sort case across all three;
new golden for a sorted list. Backward-compat: a plain `search "login"` must
return byte-identical results to today.

**Risk.** Low. Pure read-side. *Mitigation:* unknown predicates degrade to
substring; default sort preserved.

**Effort.** ~1–1.5 days.

---

### P1.2 — `rebar export` / `rebar import`

**Goal.** `export` = stable JSON snapshot of replayed state; `import` = create
tickets from a JSON or GitHub-issues payload through the normal locked write path.

**Seams.**
- New read command `export` in `_cli/__init__.py` → calls `reduce_all_tickets`
  and emits `{schema_version, exported_at, tickets:[<ticket_state>...]}`. The
  per-ticket shape is the **existing** `ticket_state` output schema — reuse it,
  add only an envelope schema `schemas/export.schema.json`.
- New write command `import` → for each input record, compose a `CREATE` (and
  follow-on `COMMENT`/`LINK`/`tag`) through `_commands/composer.py` and the normal
  `event_append` path. **No** raw event injection (that would bypass I2 filename
  generation and I5). An `--id-map` option records source→new-id so links resolve.
- Adapters live in `src/rebar/_io/` (new, small): `export_json.py`,
  `import_json.py`, `import_github.py` (maps GH issue fields → rebar create args;
  network read only, no write to GitHub).

**Interface surface.** Library `export_tickets()` / `import_tickets(records)`;
CLI `export`/`import`; MCP `export_tickets` (read) and `import_tickets` (write,
gated by `REBAR_MCP_READONLY`). Help texts for both.

**Wire/schema.** New `export.schema.json` envelope; per-ticket shape unchanged.
`import` writes only existing event types → no `SCHEMA_VERSION` bump.

**Tests.** Round-trip property test: `export` → fresh repo → `import` →
`export` yields the same logical state (modulo new ids/timestamps). GitHub
adapter unit test against a recorded fixture (no live network; `integration`
marker for the live path).

**Risk.** Low–medium. *Mitigation:* import is just composed creates — it cannot
violate append-only or filename invariants because it never writes raw events.
Importing into a non-empty store is **additive** (never updates/deletes).

**Effort.** ~2 days (JSON), +1 day (GitHub adapter).

---

### P1.3 — Collection-field merge fix (close gotcha G4)

**Goal.** Concurrent tag additions on two clones must not clobber each other.

**Root cause.** `edit_ticket(tags=[...])` emits a single `EDIT` that *replaces*
the whole `tags` field; under skewed-clock replay (I8) the last-by-timestamp EDIT
wins and drops the other clone's tag. The dedicated `tag`/`untag` events are
already delta-shaped and safe.

**Design (choose A; B is the fallback).**
- **A — route `edit(tags=)` through deltas (no wire change).** In
  `_commands/composer.py`, when an edit includes `tags`, diff against the
  ticket's current replayed tags and emit `tag`/`untag` delta events for the
  difference instead of a whole-field `EDIT`. Replace-semantics disappear; merge
  is set-union/least-surprise. Smallest blast radius, ships in Phase 1.
- **B — model `tags` as an OR-Set CRDT in the reducer** (`reducer/_processors.py`
  `process_*` for tag add/remove keyed by event UUID). More general (covers any
  future collection field) but a reducer-semantics change → defer to Phase 2 with
  HLC, and only if A proves insufficient.

**Wire/schema.** A: none (reuses `tag`/`untag`). B: reducer change + convergence
test + `SCHEMA_VERSION` consideration.

**Tests.** Two-clone regression mirroring
`test_concurrency_regression.py`: clone-1 adds `tag:x`, clone-2 adds `tag:y`
concurrently, reconverge → both tags present on both clones. Add to the doctrine
suite.

**Risk.** Low (A). *Mitigation:* `edit` callers see identical end-state for the
single-writer case; only the concurrent case changes (strictly better).

**Effort.** ~0.5 day (A).

---

### P1.4 — `rebar gc` + maintenance doctrine (close gotcha G3)

**Goal.** A safe way to reclaim space on a long-lived `tickets` branch without
breaking the reset-recovery safety net that depends on reflog history.

**Background.** The write path sets `gc.auto=0` in the tracker worktree
(`event_append._ensure_gc_auto_zero`, and the bash equivalent) precisely so a
background `git gc` can't prune the reflog commits the sync algorithm
(`ticket-sync.sh`, "discarded commits survive in reflog") relies on. The cost is
unbounded loose-object/pack growth.

**Design.**
- New maintenance command `rebar gc` (operator-only, like `init`; **not** over
  MCP) that, under the write lock (I5) and only when **not** in a rebase/merge
  recovery state (I9 guard, reuse `_lock.check_no_rebase_in_progress`):
  1. `git -C <tracker> reflog expire --expire=<window>` with a **conservative
     default window** (e.g. 14 days, configurable `gc.reflog_window`) so recent
     recovery history is retained;
  2. `git -C <tracker> gc` (or `repack -ad`) to pack;
  3. report bytes reclaimed.
- Pair with **compaction**: document a "compact then gc" cadence in
  `docs/concurrency.md` and add a `--compact-first` flag that runs per-ticket
  `compact` over eligible tickets before packing. Compaction stays I1/I9-safe
  (SNAPSHOT folds, `*.retired` renames merge as union).

**Wire/schema.** None. Local-repo maintenance only.

**Tests.** `gc`-then-recover regression: write, `gc`, then force an
unrelated-history / diverged sync and assert the reset-recovery path still works
(recovery must not depend on reflog older than the window). Bytes-reclaimed
sanity test on a synthetic many-event ticket.

**Risk.** Medium (touches reflog the recovery path uses). *Mitigation:*
conservative default window ≫ the ≤1/min sync cadence; refuse to run mid-recovery;
keep it operator-only and off the agent (MCP) surface.

**Effort.** ~1.5 days.

---

## Phase 2 — correctness backbone (phase carefully)

### P2.1 — Hybrid Logical Clock for event ordering (close gotcha G1)

**Goal.** Make EDIT/COMMENT (and all) cross-clone ordering causal and
skew-immune, generalizing I8 beyond STATUS forks, while keeping lexical
filename sort == replay order and staying readable by older clones.

**Why HLC (not pure Lamport).** rebar filenames must stay **lexically sortable
and roughly wall-clock-aligned** (humans and `ls` read them; existing files are
ns integers). A Hybrid Logical Clock keeps a physical-time high part (so order
still tracks real time across unrelated clones) plus a logical counter (so
causally-related events from one actor never tie or invert under skew). This is
the same family git-bug uses (Lamport `MemClock`+`PersistedClock`); HLC adds the
wall-clock alignment rebar's filenames want.

**Encoding (the careful part).** Today: `${timestamp_ns}` = up to 19-digit ns
int. New prefix must be **fixed-width and ≥ any already-written ns prefix** so
old + new files still sort correctly in one directory. Proposed fixed-width
decimal: `PPPPPPPPPPPPPPPPPPP` (19-digit ns physical) `CCCC` (4-digit logical
counter, zero-padded) → a 23-digit sortable integer string, monotonic and never
narrower than legacy 19-digit values *when left-aligned by total width*. **Open
decision for review:** legacy 19-digit names sort *below* any 23-digit name
lexically only if widths are normalized — so the migration must either (a)
zero-pad on read in the reducer's sort key, or (b) left-pad new names to a width
that compares correctly against legacy. Pin the exact comparator in a test.

**Clock source shared by both writers.** A per-clone clock file
`.rebar/hlc.state` (gitignored, I3/I7 — local, rebuildable from
`max(prefix seen)` across the event log):
- Python writers read/update it in `event_append` just before composing the
  event dict (the timestamp is produced at the seam, not re-derived in the
  committer — preserve that).
- Bash writers (`ticket-lib.sh`) call a tiny `python3 -m rebar._store.hlc next`
  helper so **one** implementation advances the clock (no bash/Python clock
  drift). Update happens **under the existing write lock**, so the read-modify-
  write of `hlc.state` is serialized per clone (no new lock — I6 clean).
- On startup/first write, seed from `max` event prefix in the worktree so a fresh
  clone of a populated store doesn't regress below existing events.

**Reducer.** `reducer/_processors.py` already resolves STATUS forks by UUID
(skew-independent). With HLC, EDIT/COMMENT order by the HLC prefix becomes
causal; keep the UUID tiebreak for exact-equal prefixes (defense in depth).
Update the sort key to the width-normalized comparator above.

**Wire/schema.** Filename prefix semantics change → **bump `SCHEMA_VERSION` to 2**
and document in `event-schema.md`. **Backward compat:** an older clone replays by
lexical filename order regardless of how the prefix was derived, so it still
*works* (just without the causal guarantee) — provided the width comparator is
chosen so old+new interleave correctly. New clones reading old-only stores see
pure-ns prefixes (counter implicitly 0). No event *body* change, so unknown-type
machinery isn't even needed; this is an ordering refinement, not a new type.

**Tests (this is where the current gap is closed).**
- **New EDIT/COMMENT convergence regression** in
  `tests/integration/test_concurrency_regression.py`: two clones with
  **artificially skewed clocks** each edit the same field / add comments; after
  reconverge both clones agree on field value and comment order, and the order
  respects causality (the clone that observed the other's event before writing
  must sort after). This test does not exist today — it is the executable proof
  G1 is fixed.
- Width-comparator unit test: legacy 19-digit and new 23-digit names sort into
  the correct global order.
- Forward-compat: older-reducer replay of a v2 store still produces a valid
  (if best-effort-ordered) state.

**Rollout.** Land behind a default-on flag `REBAR_HLC` with an env kill-switch
for one release; ship the convergence test first (red), then the clock (green).

**Risk.** Medium — it changes the ordering key, the one thing every clone agrees
on. *Mitigations:* fixed-width comparator pinned by test; clock is local/
rebuildable (no committed shared state, I7); single clock implementation shared by
both writers; UUID tiebreak retained; staged behind a flag with the convergence
test as the gate.

**Effort.** ~3–4 days incl. the new regression harness.

---

### P2.2 — Authenticated identity + optional signing (close gotcha G2)

**Goal.** Make "who did this" trustworthy without breaking the zero-config path.

**Design (opt-in, advisory-first).**
- **Identity resolution.** On write, resolve an author identity from
  `git config user.email`/`user.name` (already available where the tracker lives)
  and stamp it on the event `author` field (events already carry `author`/`env_id`
  at the seam). This is *recorded* identity — a strictly better default than the
  free-string `--assignee`/`--author` today, even before signing.
- **Optional signing.** When `identity.sign=true` (config) or `REBAR_SIGN=1`,
  the per-event commit is GPG/SSH-signed using the operator's git signing config
  (`git commit -S` in the locked commit step — `event_append.stage_and_commit`
  and the bash `_flock_stage_commit`). Verification is **advisory**: `show`/`fsck`
  surface `signed: verified|unverified|unsigned` per event/ticket; replay never
  *rejects* on an unverified event (rejecting would let one bad push wedge a
  store — and can't be enforced cross-clone anyway).
- **Surface.** `fsck` gains an identity/signature section; `show --verbose`
  annotates comment/claim provenance with verification state.

**Wire/schema.** Signing rides git's commit layer (no event-body change). If we
also record a verification hint in the event body, that's an additive optional
field → forward-compat safe; keep `SCHEMA_VERSION` at 2 (no break) since unknown
*fields* are already tolerated by the reducer (it reads known keys).

**Tests.** Sign a write, verify `fsck` reports `verified`; tamper an event's
author and assert `unverified`/mismatch is surfaced (not silently trusted);
zero-config path unchanged (no signing config → `unsigned`, no errors). MCP
parity for the new read annotations.

**Risk.** Medium — key management and platform variance (GPG vs SSH signing).
*Mitigations:* fully opt-in; advisory verification (never blocks writes/reads);
falls back to recorded-identity when signing unavailable; no change to the
default install experience.

**Effort.** ~2–3 days.

---

## Phases 3–4 (sequencing only; detail when scheduled)

- **P3.1 GitHub/GitLab bridge** — reuse `rebar_reconciler` differ/applier seams
  (already abstracted for Jira); a new client adapter mirroring `acli.py`.
- **P3.2 Read-only web/TUI viewer** — thin server over `reduce_all_tickets` JSON;
  **read-only** (I-neutral, no write path). Start with the existing output schemas.
- **P3.3 Notifications hook** — post-write hook / `rebar watch` emitting to
  file/webhook; no committed shared state (I6 clean).
- **P4 long tail** — attachments (git-blob-referenced; watch G3 bloat), due
  dates / milestones / time tracking (Jira-parity driven), label metadata,
  comment editing. Defer absent a concrete user need.

## Suggested execution order & dependencies

```
P1.1 query ─┐
P1.2 export ┼─ independent, parallelizable, low risk → land first
P1.3 tags(A)┘
P1.4 gc      ── independent (operator surface)
                         │
P2.1 HLC  ───────────────┘  (P1.3-B, if ever needed, rides on P2.1's reducer work)
P2.2 identity ── independent of HLC but pairs naturally (both per-actor)
```

Land P1.1–P1.4 in any order (no interdependencies; each is its own PR with its own
tests). Gate P2.1 on its new convergence regression being merged red-first. P2.2
is independent but is best reviewed alongside P2.1 since both touch the write seam.
