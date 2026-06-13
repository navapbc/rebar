# rebar remediation — detailed implementation plan

*Companion to [`oss-comparison-and-remediation.md`](oss-comparison-and-remediation.md).
That document argues **what** to fix and **why**; this one specifies **how** —
concrete seams, schema/wire impact, invariant (I1–I9) compliance, test plans,
rollout, and risk controls. Scope: the recommended cut line — a prerequisite
(**P1.0**), **Phase 1 (P1.1–P1.4)**, and **Phase 2 (P2.1–P2.3)**. Phases 3–4 are
sketched at the end for sequencing only.*

> **Note on accuracy.** This plan is grounded in the current tree as of the
> revision date. Several seams were re-verified during review: the bash leaf
> writers (`ticket-create.sh`, `ticket-edit.sh`, …) are **retired/dead** — live
> writes flow through Python (`_commands/_seam.py`, `ticket_txn.py`) plus the
> reconciler and `ticket-compact.sh`. Citations below point at the live code.

## Ground rules every work item must satisfy

1. **Invariants are a merge gate.** No new committed shared-mutable file, no new
   *cross-client* lock (I3/I6/I7); a purely **local** per-clone lock (like the
   existing write lock) is allowed. New per-clone state is gitignored and
   rebuildable. New write events flow through the locked write path — no side
   channels (I5).
2. **Writers and byte format — see P1.0 first.** **Four** event serializers exist
   and only one is canonical: `_store/event_append.py:56-61` writes canonical
   `json.dumps(sort_keys=True, separators=(",",":"))`, while **three** others write
   plain `json.dumps(event, ensure_ascii=False)` (unsorted, non-compact) — the
   reconciler helper `_engine/event_append.py:123`, the STATUS/claim path
   `ticket_txn.py:219` (which renames+commits directly, bypassing
   `stage_and_commit`), and SNAPSHOT compaction `ticket-compact.sh:274`. There is
   **no** byte-parity test in `tests/scripts/` (the one cited in
   `_store/event_append.py:15`'s docstring does not exist). **P1.0 unifies all four
   and adds the test; every later item that adds or reorders fields — P2.1, P2.2's
   detached signature, P2.3 — depends on it.**
3. **Live write/timestamp topology (verified).** The single `time.time_ns()`
   ordering timestamp is generated at **four** live seams, not one:
   - `_commands/_seam.py:153` — create / edit / comment / link / unlink / tag /
     untag / archive / set-file-impact / set-verify-commands (generated *before*
     the lock in `_store/event_append.stage_and_commit`, lock at `:117`);
   - `ticket_txn.py:~200,333,359` — transition / claim (generated **under** the
     lock that file takes at `:78`);
   - `ticket-compact.sh:~253` — SNAPSHOT (bash; via inline `python3`);
   - reconciler `rebar_reconciler/inbound_translate.py` `_event_meta` — batched
     events written by `_engine/event_append.append_event` (rename under lock, **no
     per-event commit** — commits are batched by the applier).
   Any change to how the timestamp is derived must land at **all four**.
4. **Three interfaces, one behavior.** Library (`src/rebar/__init__.py`), CLI
   (`_cli/__init__.py` + `_cli/help/*.txt`), MCP (`mcp_server.py`, readonly gate
   `mcp_server.py:_readonly()`) stay at parity (`tests/interfaces/`). Read-side
   additions need an output JSON Schema (`schemas/`), an MCP `outputSchema`
   registration, and a golden.
5. **Forward compatibility.** Anything an older clone could choke on goes through
   `reducer/_version.py` (`KNOWN_EVENT_TYPES`, `SCHEMA_VERSION`), pinned by
   `tests/interfaces/test_event_schema_forward_compat.py`.

---

## P1.0 — Prerequisite: unify the canonical event-byte format (enables P1.3/P2.x)

**Goal.** One event byte format and an executable parity gate, so later items
that add/reorder event content rest on a real guarantee (closes review finding #6).

**Seams — all four serializers (not just the reconciler).** Factor the canonical
serializer into one shared helper (e.g. `_store/event_append.canonical_bytes`) and
route **every** event producer through it:
- `_engine/event_append.py:123` (reconciler) — import the helper;
- `ticket_txn.py:219` (STATUS/claim) — replace its `json.dump(event, …,
  ensure_ascii=False)`; this path renames+commits inline (`:236-243`), so the
  helper must be importable without pulling in `stage_and_commit`'s lock;
- `ticket-compact.sh:274` (SNAPSHOT) — its inline `python3` writer must use the
  same canonical form (import the helper in the heredoc).
Also fix the false guarantee in `_store/event_append.py:15`'s docstring (it cites a
test that doesn't exist) once the real test lands.

**Tests.** Add `tests/scripts/test-ticket-write-commit-event.sh` (the test ground
rule 2 previously *assumed* existed) **and** a Python parity test that drives an
event dict through **all four** producers and asserts byte-identical output. Re-run
the reconciler + compaction suites — folded/`*.retired` bytes are read, not
re-serialized, so existing committed data is unaffected.

**Risk.** Low-medium: changes committed bytes for reconciler/STATUS/SNAPSHOT
events. *Mitigation:* only key order/whitespace changes (semantically identical
JSON); the reducer parses by key, not bytes, so replay and existing data are
unaffected; land standalone before any field additions.

**Effort.** ~1–1.5 days (four call sites incl. the bash heredoc).

---

## Phase 1 — quick wins (additive, no wire-format break)

### P1.1 — Query upgrade (close gotcha G5)

**Goal.** Field-scoped predicates, `OR`, negation, and caller-controlled sort in
`search`/`list`, reusing the in-memory reducer path (read-only; I3 clean).

**Seams.**
- `reducer/search.py:search_states()` is substring-AND only. Add a parser in a new
  `reducer/_query.py`: tokenize; recognize `field:value` predicates (`status:`,
  `type:`, `priority:`, `assignee:`, `tag:`, `parent:`), leading `-`/`not:`
  negation, and `OR` between groups (default `AND`). Free text still matches
  `_haystack`. Unknown `field:` → literal substring (no breakage).
- `reducer/_filters.py` already holds the fixed-flag predicate logic; expose one
  `match_predicate(state, field, op, value)` shared by `_query.py` and
  `apply_ticket_filters` (one comparison vocabulary).
- **Sort** lives in the read facade. Replay order uses `reducer/_sort.py`
  (event ordering) — **do not touch that**; presentation sort is separate, in
  `_engine_support/reads.py` / `_reads.py`. Keys: `priority|created|updated|id|
  status`, `-key` descending. **Default sort unchanged** so goldens hold.

**Surface.** CLI `--sort/-s` on `list`/`search`/`ready` (+ help texts); library
`sort=` kwarg; MCP same kwarg with bumped `inputSchema`.

**Wire/schema.** None. No `SCHEMA_VERSION` bump.

**Tests.** Parser units (predicate/OR/negation/precedence); `test_parity.py`
query+sort case across all three interfaces; a sorted-list golden; back-compat:
plain `search "login"` returns byte-identical results to today.

**Risk.** Low (pure read-side). *Mitigation:* unknown predicates degrade to
substring; default sort preserved.  **Effort.** ~1–1.5 days.

---

### P1.2 — `rebar export` / `rebar import`

**Goal.** `export` = stable JSON snapshot of replayed state; `import` = create
tickets from JSON / GitHub-issues payloads through the normal locked write path.

**Seams.**
- Read command `export` → `reduce_all_tickets` → `{schema_version, exported_at,
  tickets:[<ticket_state>...]}`. Per-ticket shape is the **existing**
  `ticket_state` schema; add only an envelope `schemas/export.schema.json` **and**
  register an MCP `outputSchema` for the read tool (parity with other read tools).
- Write command `import` → per record compose `CREATE` (+ follow-on
  `COMMENT`/`LINK`/`tag`) via `_commands/composer.py` through the normal
  `event_append` path. **No raw-event injection** (that would bypass I2 filename
  generation / I5). `--id-map` records source→new-id so links resolve. Adapters in
  new `src/rebar/_io/`: `export_json.py`, `import_json.py`, `import_github.py`
  (maps GH issue fields → create args; network **read** only).
- MCP: `export_tickets` (read) and `import_tickets` (write, gated by
  `_readonly()`).

**Wire/schema.** New envelope schema; per-ticket shape unchanged; `import` writes
only existing event types → no `SCHEMA_VERSION` bump.

**Tests.** Round-trip: `export` → fresh repo → `import` → `export` yields the same
logical state (modulo new ids/timestamps). GitHub adapter against a recorded
fixture; live path under the `integration` marker.

**Risk.** Low–medium. *Mitigation:* import is composed creates — cannot violate
append-only/filename invariants; importing into a non-empty store is additive
(never updates/deletes).  **Effort.** ~2 days (JSON) + 1 day (GitHub).

---

### P1.3 — *(moved)* tag convergence is a wire change, not a quick win

Originally proposed here as a "no-wire-change, ~0.5-day" reroute of `edit(tags=)`
through "delta `tag`/`untag` events." **That premise was false:** `tag()`/`untag()`
both emit a **whole-field `EDIT`** (`_commands/leaf.py:73-80,95-101`;
`process_edit` sets `state["tags"]` wholesale, `reducer/_processors.py`), and no
`TAG`/`UNTAG` event type exists (`reducer/_version.py:KNOWN_EVENT_TYPES`,
`_store/event_append.py:EVENT_TYPES`). So there is nothing delta-shaped to reuse;
a real convergence fix needs **new event semantics → a wire-format change.** It is
therefore re-scoped and **moved to Phase 2 as P2.3** (it shares the convergence
test-harness and `SCHEMA_VERSION` bump with the clock work).

**Phase-1 interim (cheap, honest):** document tags as last-writer-wins under
concurrency in `docs/concurrency.md`, and have `edit(tags=)` / `tag` / `untag`
**re-read current tags under the write lock** before composing the EDIT (already
true for `leaf.tag/untag` via `current_tags`; extend to the `edit` path). This
shrinks the *single-clone* race window but does **not** fix cross-clone
convergence — that is P2.3. *Effort:* ~0.25 day; *risk:* low.

---

### P1.4 — `rebar gc` + maintenance doctrine (close gotcha G3)

**Goal.** Reclaim space on a long-lived `tickets` branch without breaking the
reset-recovery safety net that depends on reflog history.

**Background (corrected seams).** The write path sets `gc.auto=0`
(`_store/event_append.py:64 _ensure_gc_auto_zero`, and the bash equivalent) so a
background `git gc` can't prune the reflog commits recovery relies on. That
recovery now lives in **`rebar._store.sync`** (`_store/sync.py:54-77`,
`reset --hard origin/tickets` for unrelated/diverged histories, "recoverable via
reflog") — **not** in `ticket-sync.sh`, which is now a ~21-line shim. The cost of
`gc.auto=0` is unbounded loose-object/pack growth.

**Design.** Operator-only command `rebar gc` (**not** over MCP, like `init`) that,
under the write lock (I5) and only when not mid-recovery (reuse
`rebar._store.lock.check_no_rebase_in_progress` — note: **no** leading underscore):
1. `git reflog expire --expire=<window>` with a **conservative default**
   (`gc.reflog_window`, default 14 days ≫ the ≤1/min sync cadence) so recent
   recovery history survives;
2. `git gc`/`repack -ad`; 3. report bytes reclaimed.
Pair with compaction: a `--compact-first` flag runs per-ticket `compact` over
eligible tickets first (I1/I9-safe: SNAPSHOT folds, `*.retired` renames union),
and document a "compact then gc" cadence in `docs/concurrency.md`.

**Wire/schema.** None (local-repo maintenance).

**Tests.** `gc`-then-recover regression: write, `gc`, then force an
unrelated/diverged sync and assert `_store/sync` recovery still works (must not
need reflog older than the window). Bytes-reclaimed sanity on a synthetic
many-event ticket.

**Risk.** Medium (touches reflog the recovery path uses). *Mitigation:*
conservative window; refuse mid-recovery; operator-only, off the MCP surface.
**Effort.** ~1.5 days.

---

## Phase 2 — correctness backbone (phase carefully)

### P2.1 — Monotonic-integer Hybrid Logical Clock for event ordering (close gotcha G1)

**Goal.** Make EDIT/COMMENT (and all) cross-clone ordering causal and skew-immune,
generalizing I8 beyond STATUS forks, while keeping lexical/integer filename order
== replay order and staying readable by older clones.

**Encoding — single monotonic integer (not a composite width).** Keep the prefix a
**single integer** `hlc = max(time.time_ns(), last_seen_hlc + 1)`. This is a
Hybrid Logical Clock collapsed into one value: it tracks wall-clock ns (so order
still follows real time across unrelated clones) but never ties or inverts for
causally-related events from one actor (the `+1` floor). **Why this dissolves the
width hazard** (review finding #2): there is no second fixed-width field, so legacy
19-digit ns names and new HLC names are *both plain integers*. The fix is to make
ordering compare them **as integers**, not strings:
- Change `reducer/_sort.py:event_sort_key` (line 21) from `ts_segment =
  name.split("-")[0]` (string-compared) to `int(ts_segment)` (with a safe
  fallback for malformed names), preserving the existing `(ts, type_order, name)`
  tuple and the LINK<UNLINK tiebreak (its two consumers, `reducer/__init__.py`
  and `reducer/_cache.py:102`, don't depend on the element's type, and no test
  pins it as a string — so the change is caller-safe).
- Align the **other filename-order sites** (verified enumeration — all are
  width-hazard-exposed string compares today):
  - `graph/_links.py:53` — `os.path.basename(x[1]).split("-")[0]` (LINK/UNLINK
    ordering); switch the first key element to `int(...)`.
  - `_commands/unlink.py:47` — `x[1].name.split("-")[0]`; same fix.
  - `ticket_txn.py:187-190` — the STATUS-fork scan uses a **bare `sorted()`** over
    full filenames (whole-string lexical), *not* a `split("-")[0]` prefix.
    **Behavior note:** fork *resolution* is UUID-keyed and skew-independent
    (unaffected), but the scan picks "most recent STATUS" by filename order, so it
    must move to integer-prefix ordering too; verify the change against the
    STATUS-chain `parent_status_uuid` advancement (which *does* depend on order).
  Factor a single `int`-prefix comparator so all four sites share one impl.
Under integer comparison, old + new interleave correctly regardless of digit
width, and in practice the HLC stays 19 digits until year ~2286 (ns rollover to 20
digits), so even **string**-comparing older clones order correctly for ~250 years
— which retires the cross-version concern (finding #9) and makes rollback clean
(finding #10): turning the clock off leaves plain integers that still sort right.

**One clock source, serialized, no seam/lock contradiction (finding #4).** Put the
clock in `rebar._store.hlc` with `next_tick()` that performs the read-modify-write
of a **gitignored, rebuildable** per-clone file `.rebar/hlc.state` (I3/I7) under a
**dedicated local lock** `.rebar/hlc.lock`, acquired and released *inside*
`next_tick()` (never held across the write lock → no lock-ordering hazard). Because
`next_tick()` self-serializes, it can be called at the existing seam **without**
moving timestamp generation into the committer — resolving the
"under-the-lock vs at-the-seam" contradiction the first draft had. All four live
seams (ground rule 3) call the same helper:
- Python (`_seam.py`, `ticket_txn.py`, reconciler `inbound_translate`) → import
  `rebar._store.hlc.next_tick()`;
- bash (`ticket-compact.sh`) → `python3 -m rebar._store.hlc next` (one impl, no
  bash/Python drift).
Seed/migrate: on first tick `next_tick()` initializes `last_seen` from
`max(existing event prefix)` across the worktree, so a fresh clone of a populated
store never regresses below existing events. **Injectable clock** for tests: the
physical source reads an override (`REBAR_HLC_NOW`) so the skewed-clock harness
(below) can drive it — this injection point does not exist today and is part of
this item's scope (finding #10).

**Reducer.** STATUS forks already resolve by UUID (skew-independent,
`reducer/_processors.py:78-120`) — keep that UUID tiebreak for exact-equal
prefixes as defense in depth. With HLC, EDIT/COMMENT ordering by prefix becomes
causal.

**Wire/schema.** Prefix *semantics* change (still a single integer) → **bump
`SCHEMA_VERSION` to 2** and document in `event-schema.md`. No event-*body* change,
so the unknown-type machinery isn't engaged; older clones still replay (correctly,
per the integer-width argument above).

**Tests — this closes the currently-missing convergence guard.**
- **New skewed-clock EDIT/COMMENT convergence regression** in
  `tests/integration/test_concurrency_regression.py`: two clones with injected
  skew (`REBAR_HLC_NOW`) each edit the same field / add comments; after reconverge
  both clones agree on field value and comment order, **and** order respects
  causality (a clone that observed the other's event before writing sorts after).
  This test does not exist today — it is the executable proof G1 is fixed.
- `event_sort_key` integer-vs-string unit test: legacy 19-digit and new names sort
  into one correct global order.
- Forward-compat: a v1-reducer replay of a v2 store still yields a valid state.

**Rollout & rollback.** Land the convergence test **red first**, then the clock
(green), behind a default-on `REBAR_HLC` with an env kill-switch for one release.
Rollback is clean (integers either way).

**Risk.** Medium — it changes the ordering key. *Mitigations:* single-integer
encoding removes the width hazard; integer comparator pinned by test; clock is
local/rebuildable (no committed shared state, I7) and self-serialized by a local
lock (no new cross-client lock, I6); UUID tiebreak retained; staged behind a flag
with the convergence test as the gate.  **Effort.** ~4–5 days incl. the injectable
clock seam + new regression harness.

---

### P2.2 — Authenticated identity (in-event), with optional commit signing (close gotcha G2)

**Goal.** Make "who did this" trustworthy without breaking the zero-config path —
and **coherent with the many-events-per-commit reality** (review finding #5).

**Why in-event identity is primary (not per-commit signing).** Per-*commit*
signing cannot attest per-event authorship because the commit↔event mapping is
many-to-one in two live paths: **compaction** writes one commit covering a SNAPSHOT
+ N `*.retired` renames (`ticket-compact.sh`), and the **reconciler** batches many
`append_event` writes into applier-level commits (its `append_event` does a locked
rename but **no** per-event commit). So:
- **Primary — recorded identity per event.** Resolve an author identity from
  `git config user.email`/`user.name` at write time and stamp the event `author`
  (events already carry `author`/`env_id` at the seam). Optionally store a
  **detached signature over the event's canonical bytes** (depends on P1.0's single
  byte format) in an additive optional field — verifiable independent of git
  commits, and preserved through reconciler batching.
  - **Compaction caveat (in scope).** Today a SNAPSHOT stores only
    `source_event_uuids` (`ticket-compact.sh:263`), **not** per-event author/
    signature — so folded events' identity would be lost on compaction. P2.2 must
    extend the SNAPSHOT payload to carry the recorded identity (and signature, if
    present) of each folded event, and `process_snapshot`
    (`reducer/_processors.py:337-341`) to surface it, so verification still reads
    back through `*.retired` folds.
- **Secondary — optional commit signing.** When `identity.sign=true` / `REBAR_SIGN=1`,
  also `git commit -S` in the locked commit step for the single-event Python/STATUS
  paths, as a complementary commit-DAG integrity layer — explicitly **not** the
  per-event authority (so its weakness under compaction/batching is harmless).
- **Verification is advisory.** `show`/`fsck` surface
  `identity: verified|unverified|unsigned` per event/ticket; replay never *rejects*
  (one bad push must not wedge a store, and cross-clone rejection is unenforceable).

**Wire/schema.** Optional in-event signature/identity field is additive →
forward-compat safe (the reducer reads known keys; unknown fields tolerated). No
`SCHEMA_VERSION` break beyond the P2.1 bump.

**Tests.** Sign a write → `fsck` reports `verified`; tamper an event author → the
in-event signature check surfaces `unverified` (not silently trusted); a folded
(compacted) signed event still verifies through the SNAPSHOT; zero-config path →
`unsigned`, no errors; MCP parity for the new read annotations.

**Risk.** Medium — key management / platform variance (GPG vs SSH). *Mitigations:*
fully opt-in; advisory verification (never blocks); falls back to recorded-identity
when signing unavailable; default install experience unchanged.  **Effort.** ~3–4
days (incl. extending the SNAPSHOT payload + `process_snapshot`).

---

### P2.3 — Tag (and collection-field) convergence as a CRDT (re-homed from P1.3)

**Goal.** Concurrent tag add/remove on two clones converge deterministically
instead of clobbering (the real fix the false-premise P1.3 promised).

**Design — OR-Set, new delta events.** Introduce `TAG`/`UNTAG` event types (or a
single `TAGSET` delta carrying add/remove ops), each op keyed by its event UUID so
adds and removes form an **observed-remove set**: an `UNTAG` removes only the add
ops it observed; concurrent re-adds survive. Reroute `_commands/leaf.tag/untag`
and `edit(tags=)` to emit deltas; add `process_tag/process_untag` to
`reducer/_processors.py`; add the types to `KNOWN_EVENT_TYPES` /
`EVENT_TYPES`. Removal is genuinely expressible (a whole-field EDIT or a naive
set-union cannot express it — hence a new event type is unavoidable).

**Wire/schema — and its real forward-compat cost (state it honestly).** New event
types → **`SCHEMA_VERSION` bump** and the preserve-and-ignore path: an **older
clone treats `TAG`/`UNTAG` as unknown → preserved but not applied, so tags written
by a newer clone are invisible on the old clone** until upgrade. Tags are advisory
(not blocking/scheduling), so this degradation is acceptable.

**Dual-write transition — and the replay rule that makes it safe.** For one
transition release the writer *also* emits the legacy whole-field `EDIT` so
mixed-version fleets still see tags. **Critical:** a v2 reducer must NOT apply both,
or the EDIT's wholesale `state["tags"]` assignment (`process_edit`,
`reducer/_processors.py:283-302`) replayed after a concurrent `TAG`/`UNTAG` would
clobber the OR-Set merge and reintroduce last-writer-wins. So the rule is: **once a
ticket has any `TAG`/`UNTAG` event, the v2 reducer treats tags as delta-owned and
ignores the `tags` key of every `EDIT` on that ticket** (the legacy EDIT then
serves *only* old clones). Pin this with a replay test (interleave EDIT and TAG for
the same tag; v2 result must equal the OR-Set result regardless of ordering).
Retire the legacy EDIT in the release after. Forward-compat pinned by
`test_event_schema_forward_compat.py`.

**Tests.** Two-clone CRDT convergence (add `x` vs add `y` → both; add vs concurrent
remove → deterministic add-wins/observed-remove), reusing the P2.1 skewed-clock
harness. Mixed-version dual-write test (old clone still sees tags during the
transition release).

**Risk.** Medium (wire change + reducer semantics). *Mitigations:* dual-write
transition; advisory field; convergence test as the gate.  **Effort.** ~2–3 days
(rides P2.1's harness and `SCHEMA_VERSION` bump).

---

## Phases 3–4 (sequencing only; detail when scheduled)

- **P3.1 GitHub/GitLab bridge** — reuse `rebar_reconciler` differ/applier seams; a
  new client adapter mirroring `acli.py`.
- **P3.2 Read-only web/TUI viewer** — thin server over `reduce_all_tickets` JSON;
  **read-only** (I-neutral, no write path).
- **P3.3 Notifications hook** — post-write hook / `rebar watch` to file/webhook; no
  committed shared state (I6 clean).
- **P4 long tail** — attachments (git-blob-referenced; watch G3 bloat), due dates /
  milestones / time tracking (Jira-parity driven), label metadata, comment editing.
  Defer absent a concrete user need.

## Suggested execution order & dependencies

```
P1.0 canonical bytes ── PREREQUISITE for anything adding/reordering event content
        │
        ├─ P1.1 query ──┐
        ├─ P1.2 export ─┼─ independent, parallelizable, low risk → land first
        └─ P1.3-interim ┘  (lock-scoped re-read only; full fix = P2.3)
P1.4 gc  ── independent (operator surface)
                         │
P2.1 HLC  ───────────────┤  builds the injectable-clock skewed-clock harness
        ├─ P2.3 tags ────┘  (rides P2.1 harness + SCHEMA_VERSION 2 bump)
        └─ P2.2 identity ── independent of HLC; depends on P1.0 byte format;
                            best reviewed with P2.1 (both touch the write seam)
```

Land **P1.0 first** (every field-touching item depends on its byte gate). P1.1/P1.2
and the P1.3 interim parallelize. Gate **P2.1** on its skewed-clock convergence
regression being merged red-first; **P2.3** and **P2.2** ride P2.1's harness and the
`SCHEMA_VERSION` 2 bump. Each item is its own PR with its own tests.
