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
2. **Writers and byte format — see P1.0 first.** Event serialization is **badly
   scattered**: a sweep for `json.dump(... ensure_ascii=False)` over event writes
   finds **15+** sites, only **one** canonical (`_store/event_append.py:56-61`,
   `json.dumps(sort_keys=True, separators=(",",":"))`). Many are **retired/dead**
   bash leaf scripts (`ticket-create.sh`, `ticket-edit.sh`, `ticket-comment.sh`,
   `ticket-link.sh`, `ticket-lib.sh:275/374`) no longer on the live dispatch path;
   the rest write plain non-canonical bytes. There is **no** byte-parity test (the
   one cited in `_store/event_append.py:15`'s docstring does not exist). **Because
   four review rounds each surfaced another writer, this plan does not treat any
   hand-enumerated list as complete.** P1.0 routes every *live* event write through
   one helper, deletes-or-conforms the dead writers, and — authoritatively — adds a
   **structural guard** that scans Python *and bash heredocs* so any future
   non-canonical event write fails CI. Every later field-touching item (P2.1, P2.2's
   detached signature, P2.3) depends on it.
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
   `tests/interfaces/contracts/test_event_schema_forward_compat.py`.

---

## Experimental validation & convergence with proven art

Before committing to the items below, each surviving change was **prototyped and
measured** (scripts under a scratch repo, results reproduced here) and checked
against how **proven OSS projects** solve the same problem (git-bug, Riak,
Akka, Automerge/Yjs, TUF/sigstore). The experiments confirmed feasibility; the
proven-art review changed two designs materially (P2.3 simplified; P1.0's
parity claim corrected) and added concrete gotchas to the rest.

### Scorecard (all prototyped; ✅ = validated)

| Item | Experiment result | Decisive gotcha surfaced |
|------|-------------------|--------------------------|
| **P2.1 HLC** | ✅ 2400 concurrent ticks via flock'd `next_tick()` — all unique/monotonic/19-digit (EXP4); and with **no cache file** a tick still exceeds `max(prefix)`, 19-digit (EXP4b) | `time_ns()` is 19 digits until **~year 2286**; string sort only breaks across *differing* widths → **stay 19-digit** |
| **P2.1 / P1.0 `jq`** | ⚠️ **`json.dumps==jq -S -c` is NOT portable-safe** | jq parses numbers as float64; the 19-digit ns timestamp is **>2⁵³**. jq-1.7 preserves *literals* but **any arithmetic rounds it** (`.t+0`→`…655000`), and **jq ≤1.6 (default macOS) rounds on parse** → never let jq touch the event bytes |
| **P1.0 canon** | ✅ re-serializing changes bytes but parsed dict is identical → replay-safe; AST guard caught all 7 live `.py` writers | bash linters can't see into heredocs → need a **regex prong** + allowlist the canonical helper |
| **P2.3 tags** | ✅ the chosen delta ops converge over all merge orders; the OR-Set variant (validated but **rejected**) also converged — tombstone-by-tag order-independent, deterministic `seed:<tag>` avoids duplication | **git-bug uses delta-replay-order, not an OR-Set** → adopt the simpler proven design (no tombstones/seed) (below) |
| **P1.4 gc** | ✅ discarded commit survives `gc --prune=14.days.ago`, dies at `--prune=now` | must use a conservative window on **both** `reflog expire` and `gc --prune`; never `--prune=now` |
| **P1.1 query** | ✅ predicates + `OR` + negation + degrade-to-substring in ~40 lines | unknown `field:` must fall back to literal substring (no crash) |
| **P2.2 identity** | ✅ gpg detached sign/verify/tamper round-trip over canonical bytes works | `ssh-keygen` may be **absent**; this env even force-signs commits → signing must be **opt-in/advisory** |

### Proven-art convergence & the resulting refinements

- **P2.1 clock — matches git-bug's shipped design, with one robustness rule.**
  git-bug persists a single `uint64` Lamport counter in a **local file**
  (`clocks/<name>`), advances it monotonically, and — critically — treats that
  file as a **disposable cache**: the authoritative value rides *inside git*
  (as tree-entry names) and is **re-seeded by witnessing `max` over history**
  (`entity/dag/clock.go` `Witnesser`/`ReadAllClocks`; `util/lamport/*`). rebar's
  authoritative clock value is *already* the filename prefix in the shared log,
  so **`.rebar/hlc.state` must be a pure cache**: `next_tick()` issues
  `max(hlc.state, max(prefix of the target ticket's events after sync),
  time_ns()) + 1`. The per-ticket `max(prefix)` witness is what gives cross-clone
  causal correctness (git-bug's witness-on-merge); it makes the local file
  corruption-/race-proof (git-bug shrugs off the same persistence race for this
  reason). Keep the UUID tiebreak — git-bug confirms "logical-time, then
  lexicographic content-id." *Divergence:* rebar uses an HLC (wall-aligned)
  where git-bug uses pure Lamport + display-only wall-clock; justified for
  human-readable filenames, but ordering stays driven by the integer + UUID
  tiebreak, never by wall-clock alone.
  [git-bug model](https://github.com/git-bug/git-bug/blob/master/doc/design/data-model.md)

- **P2.3 tags — SIMPLIFIED to git-bug's proven delta-replay model; drop the OR-Set.**
  The closest analogue, git-bug, does **not** use an OR-Set for labels: its
  `LabelChangeOperation` carries explicit `Added`/`Removed` lists and resolves a
  concurrent add+remove of the same label by **whichever op replays last in the
  deterministic order** (Lamport clock, then op-id) — *not* CRDT add-wins
  ([op_label_change.go](https://github.com/git-bug/git-bug/blob/master/entities/bug/op_label_change.go)).
  Once rebar has HLC+UUID total ordering (P2.1), this comes for free and it
  **fixes the original whole-field-clobber bug** (concurrent `TAG x`‖`TAG y` →
  both survive) without any OR-Set machinery. The full OR-Set (observed-remove +
  per-add UUID tags) is **over-engineered for advisory tags** and carries three
  hazards the research surfaced: unbounded tombstone growth (Riak/Akka bound it
  with version-vector "dots", not tombstones —
  [riak_dt_orswot](https://github.com/basho/riak_dt/blob/develop/src/riak_dt_orswot.erl));
  a **causal-stability requirement for safe compaction** (collapsing an OR-Set at
  a non-stable point silently loses a concurrent add or resurrects a removed
  element — Akka's documented "old data merged after marker expiry → value not
  correct"); and a **seeding-divergence trap** (independently minted seed tags
  duplicate — the Yjs "initial content duplicated" bug, fixed by *deterministic*
  seed identity). rebar has **no causal-stability detector**, so the OR-Set would
  force its SNAPSHOT compaction to exempt tag tombstones indefinitely. The
  delta-replay-order model has **none** of these problems (no tombstones, normal
  compaction). Adopt it; cite ORSWOT/Radicle-Automerge as the upgrade path **only
  if** true concurrent add-wins semantics are ever required (they aren't for
  advisory tags). *(Validated experimentally: delta ops converge across all merge
  orders; the legacy whole-field EDIT must still be ignored once a delta exists —
  that rule is unchanged.)*

- **P1.0 / P2.2 — one Python serializer; sign literal bytes (TUF/sigstore lesson).**
  The "`json.dumps == jq -S -c`" parity assertion is **unsafe** (jq float64
  rounding of the >2⁵³ timestamp, version-dependent). The TUF/sigstore ecosystem's
  hard-won lesson is "**canonicalization is a footgun; sign the literal bytes**"
  ([sigstore DSSE](https://docs.sigstore.dev/about/bundle/),
  [python-tuf #457](https://github.com/theupdateframework/python-tuf/issues/457)).
  So: route **every** event write through **one Python `canonical_bytes` helper**
  (bash heredocs call `python3 -m rebar._store.canonical`), drop jq from the
  event-write path entirely, and have P2.2's optional signature cover the **exact
  canonical bytes** of the event (excluding the signature field). With a single
  serializer the cross-impl canonicalization problem disappears. The guard is
  Semgrep (`.py`, keyed on the `json.dump(s)` call, helper allowlisted) **plus** a
  pre-commit `pygrep` regex for `.sh` heredocs (linters don't parse heredocs).
  [Semgrep shell](https://semgrep.dev/blog/2021/scanning-shell-scripts-with-semgrep/),
  [pre-commit pygrep-hooks](https://github.com/pre-commit/pygrep-hooks).

- **P2.2 identity — matches git-bug (optional, detached, advisory); note rotation limit.**
  git-bug records author as a reference and makes signing **optional** and
  **detached** (OpenPGP over the commit), verifying against keys the identity
  declared **valid at that Lamport time** (`ValidKeysAtTime`,
  `entity/dag/operation_pack.go`). rebar's "in-event recorded identity + optional
  detached signature, advisory verification" matches the simple end; the one
  thing it **cannot** express without versioned, time-anchored identities is **key
  rotation** — document that limit and the forward-compat path (identity versions)
  now, since git-bug needed a migration tool to retrofit it.

- **P1.4 gc — exact recipe.** `git reflog expire --expire=<window>
  --expire-unreachable=<window> --all` then `git gc --prune=<window>.ago`, with a
  conservative default (14 days). Akka's pruning teaches the same discipline (gate
  discard on dissemination + a TTL marker, never wall-clock alone) — here the
  window must exceed the worst-case offline/un-pushed interval, not just the
  ≤1/min sync cadence.

The item write-ups below incorporate these refinements.

### Implementation de-risking against the REAL code & tools (EXP-R*)

The conceptual prototypes above were re-run against the **installed rebar package,
a live `.tickets-tracker` store, and the actual tooling** to verify syntax,
command/API behavior, and integration seams before finalizing — the moving parts
most likely to bite during implementation.

| # | Validated against real code/tools | Result |
|---|-----------------------------------|--------|
| **EXP-R1** | the **real reducer** (`rebar.reduce_ticket`) on two concurrent whole-field tag EDITs | **bug reproduced**: clone A's `gamma` survives, clone B's `delta` is silently clobbered — P2.3's target bug is real, not hypothetical |
| **EXP-R5** | the delta fix on the real reduced state | both concurrent adds survive — the fix resolves EXP-R1 |
| **EXP-R2** | `rebar.reducer._sort.event_sort_key` | ts segment is a **`str`** today; int-key order **==** string-key order on real filenames; malformed names (`.cache.json`) take the fallback → the P2.1 int comparator change is safe |
| **EXP-R3** | the real `_store.event_append._canonical_bytes` vs plain `json.dumps` | bytes differ (P1.0 needed); parsed dicts identical (re-serialize is **replay-safe**) |
| **EXP-R4** | the real reducer on an injected unknown `TAG` event | **preserve-and-ignored** — ticket stays readable, no crash; `TAG ∉ KNOWN_EVENT_TYPES` today → P2.3's wire-compat path works |
| **EXP-R6** | the P1.4 gc recipe on the **real orphan-branch worktree** | 26→0 loose objects packed; `rebar show` still reads correctly afterward |
| **EXP-R7** | `python3 -m rebar._store.<submodule>` (the bash-calls-Python seam) | works; emits canonical sorted output — P1.0's heredoc→helper mechanism is sound |
| **EXP-R9/R9b** | the **zero-dependency** structural guard (`docs/experiments/event_write_guard.py`) | flags **exactly 7 Python + 7 bash** event writers (== the plan's live-writer set), **0 false positives** across 73 read/output `json.dumps`; the committed artifact runs against the real tree |
| **EXP-R10** | real `search_states` / `apply_ticket_filters` signatures | confirmed the exact P1.1 integration points (`search_states(states, query, *, status, ticket_type, has_tag)`) |
| **EXP-R11** | the real test harness | `pytest` needs the `[dev]` extra; once installed, **31 reducer/sort/filter/search tests pass in 2.5 s** → the new convergence/guard/sort tests have a fast, working home |

**Three new implementation gotchas surfaced (folded into the items):**

1. **No `semgrep`/`ast-grep`/`pre-commit` in the environment** → the P1.0 guard must
   be the **stdlib-only** pure-Python AST scanner + a bash regex prong (committed as
   `docs/experiments/event_write_guard.py`), **not** semgrep. EXP-R9 proves it is
   precise (0 false positives). *(Supersedes the earlier "Semgrep + pygrep"
   recommendation — semgrep is simply absent here.)*
2. **The bash prong is a false-positive factory if naive.** ~10 read/output `.sh`
   (`issue-summary.sh`, `ticket-clarity-check.sh`, `ticket-scratch-*.sh`,
   `ticket-fsck.sh`, …) contain `json.dump` but write **no event** — so the bash
   prong must scope on **`json.dump` co-occurring with an `'event_type'` dict**, plus
   the dead/migration allowlist (EXP-R9b: this cleanly isolates the 7 live writers).
3. **The environment force-signs commits** (`commit.gpgsign=true`, `gpg.format=ssh`),
   and rebar's `git commit --no-verify` does **not** bypass `-S`. rebar writes still
   *succeeded* here (the env's signing server handled it, with stderr noise), so it's
   a **latent** portability risk, not a blocker — but P2.2 should note that *ambient*
   commit signing already happens regardless of rebar, and rebar may want to pin
   `-c commit.gpgsign=false` on its internal per-event commits so a misconfigured
   signer can't fail writes.

The item write-ups below incorporate these refinements.

---

## P1.0 — Prerequisite: unify the canonical event-byte format (enables P1.3/P2.x)

**Goal.** One event byte format and an executable parity gate, so later items
that add/reorder event content rest on a real guarantee (closes review finding #6).

**Seams — the LIVE writers (illustrative, NOT asserted complete; the guard is
authoritative).** Factor the canonical serializer into one shared, **lock-free**
helper (e.g. `_store/event_append.canonical_bytes`) and route every *live*
event-file write through it. Known live writers (the guard, not this list, is the
backstop):
- `_store/event_append.py:56-61` — **canonical** (the target form);
- `_engine/event_append.py:123` — reconciler inbound events (batched commit);
- `rebar_reconciler/batch_dispatch.py:221-232` — **BRIDGE_ALERT**, a *separate*
  live reconciler writer with its **own** plain `json.dumps` directly to
  `{ts}-{uuid}-BRIDGE_ALERT.json` (does **not** go through
  `_engine/event_append`); reachable on the live reconcile path. (This omission —
  found only by sweeping for the `event_type` dict shape, not by following
  `event_append` callers — is precisely why the **guard, not this list, is the
  backstop**.)
- `ticket_txn.py:219` — transition STATUS; `:351` — claim STATUS; `:372` — claim
  EDIT (all rename+commit inline at `:236-243`, **not** via `stage_and_commit`, so
  the helper must not pull in that lock);
- `graph/_links.py:145` — LINK events (own inline `fcntl.flock` + `git add`/commit
  at `:148-176`, bypassing `_store/event_append` entirely);
- `ticket-delete-unlink-scan.py:149` — UNLINK delete-cascade events;
- **`ticket-lib-api.sh:994` — STATUS(deleted); `:1017` — ARCHIVED** (the live
  `delete` command, dispatcher `rebar:534-540` → `ticket_delete`; both raw
  `json.dump` in inline `python3` heredocs, committed with the UNLINK cascade +
  tombstone at `:1045-1046`);
- **`ticket-comment.sh:90-91` — COMMENT, still LIVE** via `ticket-transition.sh:439`,
  which runs it to write the `--force-close=<reason>` audit comment on every
  force-close transition (`--force-close` is a live, documented flag,
  `ticket-transition.sh:33,102-111`). Raw `json.dump`, non-canonical;
- **`ticket-revert.sh:170-174` — REVERT, still LIVE** via `ticket-transition.sh:179`,
  which `exec`s it on the `archived → open` un-archive seam (same pattern as
  comment↔force-close). Raw `json.dump` to the **direct** final name (no temp+
  rename), committed at `:190-192`. (The `revert` *CLI command* itself is canonical
  — it dispatches to Python `revert_core` → `append_event`, `rebar:419-425`; only
  this transition-driven bash path is raw.)
- `ticket-compact.sh:274` — SNAPSHOT (bash inline `python3` heredoc).

**Retire the dead write *paths* — but three bash scripts stay live.** The
event-write branches in `ticket-create.sh:274`, `ticket-edit.sh:266`,
`ticket-lib.sh:275/374`, and `ticket-link.sh:226/389` are off the live
committed-write path (creates/edits/links flow through `_commands/_seam.py` /
`graph/_links.py`). Only `ticket-create.sh` and `ticket-edit.sh` have **no**
remaining live caller and are safe to delete. (The `PRECONDITIONS` writers at
`ticket-lib.sh:503/648` are a separate **millisecond**-prefixed, externally-scanned
family with no dispatcher/CLI caller — out of scope for the ns-HLC change, but still
subject to the byte guard.) **Three are NOT deletable and must be canonicalized in
place, not removed** — note the recurring trap: `ticket-transition.sh` delegates to
"retired-looking" leaf scripts on its seams:
- `ticket-comment.sh` — **live**: `ticket-transition.sh:439` invokes it for the
  `--force-close` audit comment (see the live-writers list above). Conform its
  `:90-91` serializer to `canonical_bytes` and include it in the parity set.
- `ticket-revert.sh` — **live**: `ticket-transition.sh:179` `exec`s it on the
  `archived → open` un-archive seam. Conform its `:170-174` raw `json.dump` to
  `canonical_bytes`; it's a **direct-name** writer (guard bucket below).
- `ticket-link.sh` — still invoked by `composer.link_cli` for the `link --dry-run`
  *preview* (`_commands/composer.py:414-424`); that path passes `--dry-run` and
  writes **no** event, so its `:226` serializer never runs live. Keep the script
  (or migrate the preview to Python); it can't trip the guard since it writes no
  event file on that path.
The one-shot `ticket-migrate-*.sh` writers are exempt (run-once, pre-canonical
history). Also fix the false guarantee in `_store/event_append.py:15`'s docstring
once the real test lands.

**Canonical form: one Python helper; no jq (validated).** The single canonical
serializer is `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)`
(EXP2: byte-equal to `jq -S -c` **only** with `ensure_ascii=False` — `世界`
diverges otherwise). But **do not assert Python↔jq parity** and keep jq out of the
event path entirely: EXP-jq shows jq parses the >2⁵³ timestamp as float64 (jq ≤1.6
rounds on parse; jq-1.7 rounds under arithmetic), which would both break parity and
**corrupt the ordering key**. This matches the TUF/sigstore industry lesson —
"canonicalization across implementations is a footgun; standardize on one." So all
bash heredoc writers call `python3 -m rebar._store.canonical` (or the importable
helper) rather than inline `json.dump`/`jq`. Re-serialization is replay-safe
(EXP-canon: bytes differ, parsed dict identical).

> **Subprocess-cost caveat (decide at code time).** A `python3 -m
> rebar._store.canonical` per event is a heavier subprocess than the current inline
> `python3 -c` (it imports the `rebar` package, not just stdlib `json`). The bash
> writers that emit **multiple** events in one command — the `delete` path
> (STATUS + ARCHIVED + N×UNLINK) and a bulk `gc --compact-first` over many tickets —
> should serialize **all** their events in a **single** helper invocation (pass the
> event array on stdin, get canonical lines back), not one subprocess per event, so
> the unification doesn't regress those paths' latency. Correctness is unaffected
> either way (identical bytes).

**Tests — the gate is structural, scanning Python AND bash.**
- A parity test driving one event dict through **every live producer** (incl. the
  bash `delete`/SNAPSHOT paths via subprocess) asserting byte-identical output
  **against the one Python helper** (Python↔Python, not Python↔jq).
- Add `tests/scripts/test-ticket-write-commit-event.sh` (the name ground rule 2
  previously *assumed*). Include a fuzz case with a >2⁵³ integer + non-ASCII to pin
  the gotchas EXP-jq/EXP2 surfaced.
- **A structural guard** that asserts no event write uses a raw `json.dump(s)`.
  **Use the stdlib-only scanner already prototyped and committed at
  `docs/experiments/event_write_guard.py`** — NOT semgrep/ast-grep, which are
  **absent** from this environment (EXP-R9; adding them would be new CI deps for a
  pytest-based repo). It must scan **both** `*.py` (AST) **and** the inline
  `python3` heredocs inside `*.sh` (regex), because a Python-only AST scan would
  miss the `ticket-lib-api.sh:994/1017` / `ticket-comment.sh:91` /
  `ticket-compact.sh:274` class. EXP-R9/R9b proved this scanner flags **exactly the
  7 Python + 7 bash** live writers with **zero** false positives across 73
  read/output `json.dumps` sites. **Key the guard on the serialized value being an
  event** — a dict literal carrying an `event_type` key — **not** on the
  destination filename: nearly every writer serializes to a `.tmp-*` path and
  *then* renames to the final `*-<TYPE>.json` (e.g. `ticket-comment.sh` →
  `.tmp-comment-*`, `ticket_txn.py:218/350/371` → `.tmp-transition/claim-*`,
  `ticket-compact.sh:273` → `…json.tmp`), so a filename-scoped guard would catch
  only the **direct-name** writers (`graph/_links.py:144`,
  `ticket-delete-unlink-scan.py:148`, `ticket-revert.sh:170-174`, and the delete
  STATUS/ARCHIVED heredocs at `ticket-lib-api.sh:992-994/1015-1017`) and miss the
  temp-then-rename rest. For the **bash** prong specifically (EXP-R9b), scope on
  `json.dump` **co-occurring with an `'event_type'` dict** — a bare `json.dump`-in-
  `.sh` grep false-positives on ~10 read/output scripts (`issue-summary.sh`,
  `ticket-clarity-check.sh`, `ticket-scratch-*.sh`, `ticket-fsck.sh`, …) that emit
  JSON but write no event. Exempt read/output/cache `json.dumps`, the canonical
  helper, and the one-shot migration scripts via an explicit allowlist.
Folded/`*.retired` bytes are read, not re-serialized, so existing committed data is
unaffected.

**Risk.** Low-medium: changes committed bytes for reconciler/STATUS/LINK/UNLINK/
ARCHIVED/SNAPSHOT events. *Mitigation:* only key order/whitespace changes
(semantically identical JSON); the reducer parses by key, not bytes, so replay and
existing data are unaffected; land standalone before any field additions.

**Effort.** ~2–2.5 days (live call sites across Python + bash heredocs, dead-script
cleanup, and the dual-language guard test).

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
1. `git reflog expire --expire=<window> --expire-unreachable=<window> --all`
   (**both** flags — EXP6 showed an unreachable discarded commit is governed by
   `--expire-unreachable`/`--prune`, not the default reflog expiry);
2. `git gc --prune=<window>.ago` (**never `--prune=now`** — EXP6: the discarded
   commit survives `--prune=14.days.ago` but is destroyed by `--prune=now`);
   with a **conservative default** `gc.reflog_window` = 14 days that must exceed the
   worst-case offline/un-pushed interval (Akka's pruning teaches the same: gate
   discard on dissemination + a TTL, never wall-clock alone), not merely the ≤1/min
   sync cadence;
3. report bytes reclaimed.
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
  - `ticket_txn.py:187-190` (transition) **and** `ticket_txn.py:318` (claim) — both
    compute `parent_status_uuid` via a **bare `sorted()`** over full filenames
    (whole-string lexical), *not* a `split("-")[0]` prefix. **Behavior note:** fork
    *resolution* is UUID-keyed and skew-independent (unaffected), and the
    `sorted(...)[-1]` "most recent STATUS" pick agrees between string and integer
    order while names stay 19 digits (~year 2286), so this is safe in practice; move
    both to integer-prefix ordering for correctness and verify against STATUS-chain
    advancement.
  Factor a single `int`-prefix comparator so all five sites share one impl.
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
**The clock file is a disposable CACHE, not the source of truth (git-bug-validated
robustness rule).** git-bug treats its persisted `clocks/<name>` file as a
reconstructable cache — the authoritative value rides in the shared git history,
re-seeded by *witnessing `max` over it*. rebar's authoritative clock value is
*already* the filename prefix in the shared event log, so **never trust
`.rebar/hlc.state` alone**: each `next_tick()` issues
`max(hlc.state, max(prefix of the TARGET TICKET's events after sync),
time_ns()) + 1`. Witnessing the ticket's own `max(prefix)` is what gives cross-
clone causal correctness (clone B that pulled clone A's event sorts after it) and
makes the local file corruption-/race-proof — if `hlc.state` is missing, stale, or
lost to a concurrent-process write race (git-bug documents the same race and
shrugs it off for exactly this reason), the result is still correct because it is
re-derived from the durable log. So the local-lock RMW is a fast path, not a
correctness dependency. *(EXP4: 2400 concurrent flock'd ticks, all unique/
monotonic/19-digit; EXP4b: with no cache file, the tick still exceeds the ticket's
`max(prefix)` and stays 19-digit.)*

**Deliberate asymmetry — global cache, per-ticket witness.** `.rebar/hlc.state` is
**one per-clone** integer (a union high-water-mark across *all* tickets), while the
witness floor is the *target ticket's* `max(prefix)`. This is intentional: the
global cache only ever moves the counter forward (so another ticket's activity
advancing it is harmless to ordering), and the per-ticket witness supplies the
cross-clone causal floor that a global cache alone would miss right after a fetch.
A future implementer must **not** "fix" the apparent mismatch by making the cache
per-ticket — that would weaken the monotonicity the single local lock provides.

**Injectable clock** for tests: the physical source reads an override
(`REBAR_HLC_NOW`) so the skewed-clock harness (below) can drive it — this injection
point does not exist today and is part of this item's scope (finding #10).

**Width invariant (validated).** `time_ns()` is 19 digits until ~year 2286
(EXP1), and equal-width integers sort identically as strings and ints; string sort
only diverges across *different* widths. The HLC must therefore **stay 19 digits**
— which `max(physical_ns, last+1)` does in practice (the `+1` floor only advances
beyond wall-clock by the number of sub-nanosecond-spaced events, never ~10⁹×). New
clones compare prefixes as ints (skew-immune); old clones still string-compare
correctly because the width is unchanged. **Corollary (critical, from EXP-jq):**
the prefix is a >2⁵³ integer, and `jq` parses numbers as float64 — jq ≤1.6 rounds
it on parse and even jq-1.7 rounds it under any arithmetic. **No bash/jq step may
ever read, re-emit, or compute on the timestamp**; this is a second reason P1.0
routes all serialization through the one Python helper (jq never touches events).

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
  **detached signature over the event's canonical bytes with the signature field
  excluded** (i.e. sign `canonical_bytes({event without "sig"})` — covering
  `author`, `timestamp`, `uuid`, and `data`) in an additive optional field. This
  is the OCI/sigstore "sign the literal bytes" model (EXP-gpg: ed25519 detached
  sign/verify/tamper round-trip works), and it's safe **because P1.0 makes the
  canonical bytes single-impl** — verifiable independent of git commits and
  preserved through reconciler batching. git-bug validates the shape: author is a
  reference, signing is optional and detached.
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
  *Real-env note (EXP-R8):* **ambient** commit signing already happens regardless of
  rebar when the host sets `commit.gpgsign=true` (this environment does, via SSH +
  a signing server) — rebar's `git commit --no-verify` does **not** bypass `-S`.
  Writes still succeeded here, but rebar should pin `-c commit.gpgsign=false` on its
  internal per-event commits unless `REBAR_SIGN` is set, so a misconfigured host
  signer can't fail an otherwise-valid write.
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

**Key-rotation limitation (state it; git-bug's lesson).** git-bug's most valuable
trick is `ValidKeysAtTime(editTime)` — verifying against the keys the author's
*versioned* identity declared valid as of that logical time, so key rotation is
causally sound. rebar's flat in-event identity **cannot express rotation**: a
rotated/revoked key can't be distinguished from a forgery at a past time. For
advisory verification this is acceptable; **document it**, and note the forward-
compat path (versioned identity entities with HLC-stamped key sets) so it can be
retrofitted — git-bug needed a dedicated migration tool to add this later, so flag
it now.

**Risk.** Medium — key management / platform variance. *Mitigations:* fully opt-in;
advisory verification (never blocks); falls back to recorded-identity when signing
unavailable (EXP3: `ssh-keygen` may be absent, and some environments force commit
signing — so signing must never be assumed present); default install unchanged.
**Effort.** ~3–4 days (incl. extending the SNAPSHOT payload + `process_snapshot`).

---

### P2.3 — Tag convergence via delta events + deterministic replay order (re-homed from P1.3)

**Goal.** Concurrent tag add/remove on two clones converge deterministically
instead of clobbering the whole field (the real fix the false-premise P1.3
promised).

> **Design changed after proven-art review (see the validation section).** The
> earlier draft proposed a full **OR-Set** (observed-remove, per-add UUID tags).
> The closest battle-tested analogue, **git-bug**, deliberately does **not** do
> that for labels — and the OR-Set's tombstone-growth + causal-stability-compaction
> + seeding-divergence hazards are real and a poor fit for rebar (no causal-
> stability detector). **Adopt git-bug's simpler, proven model instead.**

**Design — delta events resolved by the existing total order.** Introduce `TAG` /
`UNTAG` delta events (mirroring git-bug's `LabelChangeOperation` `Added`/`Removed`
lists). The reducer applies them as **mutations of the current `state["tags"]`
list in replay order**: `TAG t` adds `t` (set-union, dedup), `UNTAG t` removes `t`.
Because P2.1 gives a deterministic total order (HLC prefix, UUID tiebreak), every
clone converges:
- concurrent `TAG x`‖`TAG y` → **both survive** (the original clobber bug is fixed —
  each op is a delta, not a whole-field replace);
- concurrent `TAG c`‖`UNTAG c` → the one that **sorts last wins**, deterministically
  on every clone (git-bug's exact semantics; fine for advisory tags).
Reroute `_commands/leaf.tag/untag` and `edit(tags=)` to emit deltas; add
`process_tag`/`process_untag` to `reducer/_processors.py`; add the types to
`KNOWN_EVENT_TYPES`/`EVENT_TYPES`. **No OR-Set, no observed-remove tombstones, no
seed minting, no special compaction handling** — deltas fold under the normal
SNAPSHOT path because there is no causal metadata to preserve.

**No seeding trap — but the first-delta BOUNDARY is load-bearing (get this exact).**
Because `TAG`/`UNTAG` mutate the *current* replayed `state["tags"]`, pre-existing
tags are carried forward automatically with no synthetic seed ops — *provided the
reducer applies EDIT.tags up to, and only up to, the first delta.* The precise rule
(validated by EXP5b's `started` boundary) is: **in replay order, EDIT.tags are
applied while no `TAG`/`UNTAG` has yet been seen; the first delta freezes the base
and every EDIT.tags from then on is ignored.** So `EDIT(tags=[a,b])` → base
`[a,b]`, then `TAG(c)` → `[a,b,c]`. ⚠️ **Do NOT implement the rule as "globally
ignore EDIT.tags if the ticket has any delta"** — that pre-scan would wipe the
pre-delta `[a,b]` base and yield `[c]`, **re-introducing the exact tag-loss bug**
this item exists to prevent. The boundary is defined by the deterministic
`event_sort_key` order, so it resolves identically on every clone. This also
sidesteps the Yjs/Automerge "independent-seed duplication" trap (no per-replica
tags are minted at all). *(The OR-Set variant needed a deterministic `seed:<tag>`
step; the delta-mutation model folds the same idea into the boundary rule.)*

**Wire/schema + forward-compat cost (unchanged from the OR-Set plan).** New event
types → **`SCHEMA_VERSION` bump 2** and preserve-and-ignore: an older clone treats
`TAG`/`UNTAG` as unknown → preserved but not applied, so newer-clone tag changes are
invisible there until upgrade. Tags are advisory, so acceptable.

**Dual-write transition — the same boundary rule covers it.** For one transition
release the writer *also* emits the legacy whole-field `EDIT` so old clones still
see tags. The boundary rule above already handles this: any legacy `EDIT.tags` that
sorts **after** the first delta is ignored on v2 (it would otherwise let
`process_edit`'s wholesale `state["tags"]` assignment,
`reducer/_processors.py:283-302`, clobber the delta result), while serving old
clones normally. A legacy `EDIT` that sorts **before** the first delta correctly
contributes to the base. Retire the legacy EDIT the release after.

**Tests.** Two-clone convergence (add `x`‖add `y` → both; add `c`‖remove `c` →
deterministic last-writer-in-order, identical on both clones), reusing P2.1's
skewed-clock harness; pre-existing-EDIT-then-delta carries tags forward; mixed-
version dual-write (old clone still sees tags; v2 ignores the legacy EDIT once
delta-owned); forward-compat pinned by `test_event_schema_forward_compat.py`.

**Risk.** Low–medium (wire change + reducer mutation rule), **lower than the OR-Set
design** it replaces (no tombstones/seeding/compaction-stability). *Mitigations:*
dual-write transition; advisory field; convergence test as the gate; rides P2.1's
total order. **Effort.** ~1.5–2 days (down from ~2–3; the OR-Set machinery is gone).

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
