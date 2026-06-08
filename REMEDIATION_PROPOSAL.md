# rebar — Remediation Proposal

Addresses the deep-review findings (maintainability, reliability, fitness for
LLM-agent use) and the competitive gaps vs `beads`/`tk`. **Concurrency across
environments and clients is a first-class, non-negotiable requirement**: it is
mostly provided by the event-sourced + git model, and every workstream below is
gated by the Concurrency Doctrine (§0) and executed under the strict-TDD protocol
(§TDD). Completion is gated by the validation protocol (§V).

---

## §TDD. Strict TDD protocol (governs all product-code changes)

No live-code change to fix a bug or add behavior may precede its test. Every change
follows this exact order:

1. **Confirm experimentally.** Reproduce the gap/bug with a concrete command and
   capture the evidence (output + exit code). A defect is *observed*, then fixed —
   never "suspected → fixed".
2. **Write a RED test** that exercises the specific symptom and fails *for the right
   reason*, against unmodified code. Run it; show it RED.
3. **Make the minimal change to GREEN.** No opportunistic edits in the same step —
   the RED→GREEN delta must stay auditable.
4. **Confirm GREEN** and run the surrounding suite **plus the concurrency-regression
   test (§V)** to check for regressions.
5. Record a one-line **§0 doctrine-compliance note** for the change.

**Change-class routing** (which TDD shape applies):
- **Product bug** (e.g. WS3 force-reset data loss, the I3a cache leak) → RED-first
  product-code sequence (steps 1–5), with the RED test capturing the exact failure.
- **Intentional contract change** (e.g. WS5d MCP surface additions, WS5b new relation,
  next-batch-style schema) → express the *new* desired behavior in a test first
  (RED against current code), then change code to GREEN.
- **Behavior-preserving rename/extraction** (WS1 rename, WS2 heredoc→module,
  WS4 deletions) → **no new RED**; these are refactors. The existing suite
  (the ~862 reconciler unit cases + interface parity tier + the §V concurrency-regression test) is
  the **characterization gate** — it must stay GREEN at every intermediate step, and
  the refactor is invalid the moment it goes red. Add characterization tests *first*
  where current coverage is thin (e.g. the previously-untestable transition critical
  section) so the refactor has a real net to catch it.
- **Test-only / infra fixes** → applied as test changes (their "RED" is the corrected
  test now exercising real behavior).

**Concurrency-specific rule.** Any workstream touching the write/sync/lock paths
(WS2, WS3, WS5c) MUST add or extend the §V concurrency-regression test **before** the
change and keep it GREEN after — the regression harness is written first (WS3) so the
later write-path changes are characterized against it.

---

## §0. Concurrency Doctrine (gates every change)

rebar's concurrency-safety comes from a small set of structural invariants, not
from locks-in-the-large. The model: **every mutation is a new, globally-unique,
append-only event file; state is a pure deterministic replay; clients converge by
git merge-as-union plus optimistic concurrency.** Any added or modified
functionality MUST preserve all of the following invariants.

- **I1 — Append-only.** Never modify or delete an existing event file. The only
  exception is compaction, which runs under the write lock and writes a SNAPSHOT
  event + renames folded files to `*.retired` (an operation git represents as
  adds/removes, still merge-as-union).
- **I2 — Globally-unique event filenames.** Every new event is
  `${timestamp_ns}-${uuid}-${TYPE}.json` (`ticket-lib.sh:85`). Two independent
  clients writing concurrently therefore never collide; git merges the two new
  files as a union with no conflict. New event kinds MUST use this scheme.
- **I3 — Reads are side-effect-free except local, rebuildable caches.** The only
  read-side write is the per-ticket `.cache.json` (content/size-keyed,
  tmp-then-rename, `_cache.py:24`). No feature may introduce a **committed** shared
  mutable file (it would create cross-client merge conflicts). **Sub-rule
  I3a (gap found in review — must fix):** `.cache.json` is currently *not* in the
  tracker `.gitignore` (`ticket-init.sh` gitignores `.env-id`/`.closure-key`/`.state-cache`/`.scratch/`
  but not `.cache.json`), and several maintenance scripts use `git add -A` on a
  ticket subdir (`ticket-compact.sh:269`) — so a stray cache *can* be committed
  today. Remediation requires: add `.cache.json` (and any WS5a local index file) to
  the committed tracker `.gitignore`, and a test asserting no tracker `git add -A`
  path ever stages a local cache/index.
- **I4 — State-dependent mutations use optimistic concurrency.** Any op whose
  correctness depends on current state (transition, and any new compound op such
  as `claim`) MUST re-read the relevant state under the write lock and reject with
  **exit 10** on mismatch, surfaced as `ConcurrencyError` uniformly across
  library/CLI/MCP (`ticket-transition.sh:394`, `__init__.py:110`).
- **I5 — Single locked write path.** All writes go through the existing
  flock-guarded append+commit path (atomic tmp-then-rename + `git add <event>` +
  commit under `.ticket-write.lock`). No side-channel writes.
- **I6 — No NEW global/cross-client lock; no shared mutable index.** Cross-client
  coordination is *only* git-merge-as-union + optimistic concurrency. No feature
  may require a lock spanning clients/machines, nor a committed index/aggregate
  that concurrent clients would both rewrite. **Sanctioned exception (existing):**
  the reconciler's `.reconciler-pass-lock` (`_advisory_lock.py:62`) *is* a committed,
  tickets-branch, cross-client advisory lock — it is single-writer-by-design (only
  one reconciler should run at a time) and is the one allowed exception. No *new*
  cross-client lock may be added; the reconciler lock is grandfathered, not a precedent.
- **I7 — Derived/aggregate data is computed from replay or stored local-only.**
  Search indexes, memory stores, counters, etc. are either recomputed from the
  event log on demand or cached local-and-rebuildable (gitignored/uncommitted).
- **I8 — Cross-client event ordering is best-effort under clock skew; only STATUS
  fork resolution is skew-independent.** Replay orders events by the
  `${timestamp_ns}` filename prefix; with skewed client clocks, COMMENT/EDIT
  interleaving across clients is best-effort. STATUS forks are resolved
  deterministically and skew-independently by the event's own UUID
  (`_processors.py:107-137`). Any new state-dependent merge logic MUST resolve forks
  by UUID (or another skew-independent key), never by timestamp alone.
- **I9 — Compaction is safe against concurrent remote appends.** Compaction (under
  the per-clone lock) writes a SNAPSHOT that folds the events it retires; a remote
  clone appending a *new* (unique-named) event merges as a union, and the SNAPSHOT
  must have already folded any event its result depends on. New compaction-like
  operations MUST preserve this: never retire an event whose content a not-yet-folded
  state could still need, and never assume the per-clone lock excludes remote writers.

**Doctrine compliance is a required section in every workstream below, and a
required gate in code review.** A change that cannot satisfy I1–I9 is redesigned,
not merged.

A companion concurrency-regression test (added first, in WS3) asserts the
load-bearing properties end-to-end: two independent clones writing disjoint and
overlapping events, fetch/merge, and replay-converges to one deterministic state.
It is the executable form of this doctrine and the characterization gate every
later write/sync change runs against.

---

## Workstreams

Priority key: **P1** = correctness/maintainability debt that compounds; **P2** =
high-value fitness/agent gaps; **P3** = docs/hygiene.

### WS1 — Complete the DSO → rebar internal rename (P1, maintainability)
**Problem.** Decoupling left DSO identity load-bearing: the reconciler package is
`dso_reconciler`; `__main__.py:30-31` seeds `sys.modules` under literal
`plugins.dso.scripts.dso_reconciler.*` keys (the old plugin path is wired into the
module loader for test-patch compatibility); 35+ `DSO_*` env vars form a public
contract (`DSO_TICKET_CLI`, `DSO_CLI`, `DSO_COMPACT_SCRIPT`, …).

**Approach (staged, behavior-preserving).**
1. Introduce `REBAR_*` env names as the canonical contract; accept the legacy
   `DSO_*` names as deprecated aliases for one release (read `REBAR_X` then fall
   back to `DSO_X`). Centralize this in `rebar-config.sh` (already the alias hub).
2. Rename the Python package `dso_reconciler` → `rebar_reconciler`. **Both**
   `sys.modules` loader-key schemes must be renamed in lockstep (review finding):
   `plugins.dso.scripts.dso_reconciler.*` via `_load_sibling_keyed`
   (`__main__.py:30-31`) **and** the `dso_reconciler.<name>` scheme via `_try_load_step`
   (`__main__.py:81`, load-bearing for Py3.14 dataclass module resolution). Update the
   reconciler conftests that seed those keys. Grep-gate post-rename for zero residual
   `plugins.dso` / `dso_reconciler.` keys. Riskiest step — one atomic change, full
   reconciler suite (~862 unit cases across 131 files) as the gate, with a revert plan ready.
3. **Python-side env aliasing (review finding):** `rebar-config.sh` aliases only ~7
   of ~30 `DSO_*` vars, and the Python reconciler reads several **directly** from
   `os.environ` (`DSO_ENV_ID`, `DSO_AUTHOR`, `DSO_DSO_ID_GUARD_MODE` in `applier.py:615-1610`;
   `DSO_RECONCILER_VERBOSE` in `outbound_differ.py:456`), bypassing the shell hub.
   Add a small Python config shim that reads `REBAR_X` then falls back to `DSO_X`,
   and route the reconciler's env reads through it — otherwise the "deprecated alias
   for one release" promise is only half-true.
4. Update `_engine.py`/`_native.py`/`cli.py` reconcile routing
   (`python -m rebar_reconciler`).

**Concurrency impact.** None — pure renaming of internal symbols/env names; the
event-log, lock path, and sync are untouched (I1–I7 unaffected). The reconciler's
own advisory pass-lock and write path are renamed, not redesigned.

**TDD.** The ~862 reconciler unit cases (131 files) + interface parity tier are the gate; no
behavior change permitted. Add an aliasing test (REBAR_* preferred, DSO_* still works).

### WS2 — Extract safety-critical heredocs; retire `DSO_TICKET_LEGACY` (P1, reliability+maintainability)
**Problem.** The optimistic-concurrency check (`ticket-transition.sh:343-555`) and
write-commit logic live as Python-inside-bash heredocs — the most safety-critical
paths are the least unit-testable. `DSO_TICKET_LEGACY` keeps a *parallel* write
implementation alive, doubling the write surface.

**Critical correction (review finding).** The earlier framing — "bash does the
git add/commit while the helper computes" — is **factually wrong for the transition
path** and would introduce a TOCTOU/lost-update window. Verified: in
`ticket-transition.sh:346-555` a **single python3 process** opens the lock fd
(`os.open` + `fcntl.flock`), reduces+verifies the current status (exit 10 on
mismatch), writes the event, **and runs `git add` + `git commit` itself**, all
before `os.close(fd)`. The lock, the optimistic re-read, the write, and the commit
are **one process / one critical section**. Splitting "compute+write in a helper,
then return, then commit in bash" would release the lock (helper process exit) before
the commit and re-acquire for the commit — between which another client can interleave,
defeating exit-10.

**Approach (corrected).** Extract the transition logic into a Python module
(`ticket_txn.py`) that is itself the **lock-holding, committing entrypoint** — it
takes the flock, reduces+verifies, writes the event, and commits, all in one process,
exactly as the current heredoc does — then the bash arm becomes a thin
`exec python3 -m ...rebar_txn transition <args>`. The critical section is *preserved
intact*; the only change is heredoc → importable, unit-testable module. Do **not**
move the commit out of the locked process.
1. Move the transition heredoc verbatim into `ticket_txn.py::transition(...)`
   (lock + verify + write + commit in-process); bash arm execs it. Behavior identical.
2. Apply the same pattern to the generic write-commit path: the *generic* path
   (`ticket-lib.sh:340-352`) genuinely does have bash hold the flock and do the
   commit — that one MAY keep bash as the lock+commit owner with a Python helper for
   event composition only. Be explicit that the two write paths have **different**
   lock-ownership models and keep each intact.
3. Retire `DSO_TICKET_LEGACY`: delete the legacy parallel Python write path and the
   gate, leaving one write implementation per path.

**Concurrency impact (I4/I5).** Net behavior unchanged: the optimistic re-read and
the commit remain in the same single locked critical section (transition: the Python
process; generic: the bash subshell). Exit-10 and the uniform `ConcurrencyError`
surface are preserved and re-asserted by the parity tier. The win is testability, not
a structural change to locking.

**TDD.** New unit tests for `ticket_txn.transition` driving the verify→write→commit
path in-process (incl. the forked/stale-status → exit-10 path, previously untestable).
Existing `test-ticket-transition*.sh`, `test-ticket-write-commit-event.sh`,
`test-ticket-subprocess-count.sh`, and the parity tier must stay green. Add a
**concurrent-transition race** test (two processes claim the same open ticket; exactly
one wins, the other gets exit 10, store shows one transition) to prove the critical
section stayed intact across the refactor.

### WS3 — Harden git sync/reconvergence; codify the concurrency model (P1, reliability)
**Problem.** Push is best-effort: a successful local commit with a failed push
silently diverges; the once-a-minute fetch/reset **force-resets local to
`origin/tickets` on unrelated histories** (`rebar:175-188`), a data-loss risk if
merge-base detection misfires. This is the cross-environment concurrency path, so
it is doctrine-critical.

**Root cause (verified).** The force-reset guard checks the **branch ref**
(`git log --oneline origin/tickets..tickets`, `rebar:110,181`) but the tracker
worktree commits to **detached HEAD** (`_push_tickets_branch` pushes `HEAD:tickets`
precisely because "commits advance HEAD but not the local branch ref",
`ticket-lib.sh:699-703`). So when HEAD is ahead of a stale `tickets` ref,
`origin/tickets..tickets` is empty → the code force-resets (`rebar:107,112,177,183`)
→ **HEAD-only local commits are lost**. This is the bug.

**Approach (corrected).**
1. **Detect local-ahead by HEAD, not the branch ref.** Switch the guard at all reset
   sites (`rebar:107,110,112,177,181,183`) to `git rev-list origin/tickets..HEAD`.
   If non-empty, do **not** reset.
2. **Reconverge with MERGE-as-union, not rebase (review finding).** The existing
   `_push_tickets_branch` deliberately chose **merge over rebase** (`ticket-lib.sh:710`,
   bug 637b): interrupted rebase strands picks as dangling commits, and compaction
   `*.retired` renames + SNAPSHOT cause rebase conflicts where merge-as-union does
   not. WS3 MUST align the reconvergence path with merge-as-union (the proposal's
   earlier "rebase-replay" wording was wrong and is retracted). On a merge conflict,
   surface `fsck_needed`/reschedule (exit 75) rather than resetting.
3. **Make push failure observable.** Return a non-fatal sync-status signal the
   library/CLI can report; `fsck` surfaces "local ahead of origin, push pending"
   instead of silent divergence.
4. Tighten the unrelated-histories force-reset to fire *only* when
   `origin/tickets..HEAD` is empty (no local-only commits) — i.e. the genuine
   stale-orphan / fresh-auto-init case — never when local work exists.
5. **Codify the model** as `docs/concurrency.md`: I1–I9, the event-filename
   contract, replay/fork determinism, the lock mechanisms, and the merge-as-union
   sync/reconvergence algorithm.

**Concurrency impact.** *Strengthens* I1/I6/I7/I9: guarantees merge-as-union is the
reconvergence path and removes the lossy reset except in the provably-empty-local
case. No global lock introduced; aligns with the codebase's existing merge (not
rebase) decision.

**TDD / concurrency-regression test.** New `tests/integration` test: clone the
tracker into two working dirs, have each append disjoint events (create + comment
on different tickets) and overlapping events (concurrent transitions on the same
ticket), fetch/merge both ways, and assert: (a) all events present (union), (b)
replay yields one deterministic state on both clones, (c) the concurrent-transition
case resolves via the lexical-UUID fork tie-break identically on both. **Plus the
specific trigger (review finding):** a **detached-HEAD-local-ahead** test — commit to
HEAD while the `tickets` branch ref lags, run `_ensure_initialized`, and assert the
local-only commit is NOT discarded (the existing `test-ticket-sync-preserves-local.sh`
only covers the on-branch case). Plus: a failed push never drops a local-only commit.

### WS4 — Remove dead rollout/migration artifacts; packaging hygiene (P2, maintainability)
**Problem.** DSO-rollout scripts shipped as dead weight (`rollback-bridge-cutover.sh`,
`dryrun-bridge-rollback-in-worktree.sh`, `pre_cutover.py`, the bridge-canary
scripts, `ticket-migrate-*` one-shots); committed `__pycache__/` in `_engine/`;
the engine-as-package-data design relies on an on-disk path (`_engine.py:26`) and
breaks under a zipped/zipimport wheel.

**Approach.** Audit each rollout/migration script for any remaining reference
(dispatcher arm, reconciler import, test); delete the genuinely-orphaned ones
(carrying their tests). Keep the generic schema migrations (`closure-checks-v1`,
`file-impact-v1`, `schema-hardening`). Remove committed `__pycache__/` and add it
to `.gitignore` under the engine. Document the "engine must install to a real
directory (no zip import)" constraint in pyproject/README, and add a
`build`-time/import-time assertion that `engine_dir()` is a real path.

**Concurrency impact.** None (removes unused code; no event/lock/sync change).

**TDD.** Full suite green after each deletion; verify no import/dispatcher
references remain (grep gate).

### WS5 — Agent-fitness features closing competitor gaps (P2, fitness)
Each sub-item is independently shippable and doctrine-checked.

**WS5a — Full-text search (closes the tk/beads searchability gap).**
- `rebar search <query> [--status/--type/...]` → matches titles, descriptions,
  comments, tags; returns the usual JSON/LLM-format list. MCP tool `search`.
- **Concurrency (I3/I3a/I7):** implemented as a **replay-derived** query — it reduces
  tickets and matches in-process (reusing `reduce_all_tickets`), optionally backed
  by a **local, gitignored, rebuildable** inverted-index cache. **No committed index**
  (would violate I6). If an index file is added it MUST be added to the tracker
  `.gitignore` (per I3a) and excluded from every `git add -A` maintenance path; it is
  per-clone, rebuilt from the event log, never synced.
- TDD: parity test (search returns identical results via library/CLI/MCP);
  index-vs-no-index equivalence test; **a test that the index file is gitignored and
  is never staged by any tracker `git add`** (closes the I3a leak for both the index
  and the existing `.cache.json`).

**WS5b — `discovered-from` relationship (closes beads' emergent-work provenance).**
- Add `discovered_from` to the canonical relation set (`_links.py:17`), non-blocking
  and never cycle-inducing (treat like `relates_to` in `_graph.py:185`). Surfaced in
  `show`/`deps`; CLI `link <a> <b> discovered_from`; MCP via existing `link_tickets`.
- **Concurrency (I1/I2):** it is just another append-only LINK event — concurrency-
  safe by construction; no new mechanism.
- TDD: link/cycle-awareness unit tests; parity.

**WS5c — Atomic `claim` (closes beads `--claim`).**
- `rebar claim <id> [--assignee X]` → in one locked critical section: re-read status
  (must be `open`), then append STATUS(in_progress)+EDIT(assignee) events; reject
  with **exit 10 / ConcurrencyError** if not `open` (someone else claimed it).
- **Concurrency (I4/I5 — the central case):** this is *the* primitive parallel
  agents need; it MUST reuse the optimistic-concurrency + flock path (re-read under
  lock, exit 10 on mismatch). Implemented on top of the WS2-extracted, lock-holding,
  committing `ticket_txn` entrypoint so the compound op shares one verified critical
  section. **Both the STATUS(in_progress) and EDIT(assignee) events are written and
  committed before the lock is released** (single commit preferred) so no concurrent
  reader on any clone ever observes `in_progress` without the assignee; both are new
  UUID-named files, so merge-as-union (I2) holds. Library raises `ConcurrencyError`;
  CLI exits 10; MCP surfaces a tool error — asserted by parity.
- TDD: two-claim race test (second claim rejected, store shows first winner);
  parity across interfaces.

**WS5d — Expose quality gates + file-impact over MCP (closes rebar's own MCP gap).**
- Add MCP tools: `clarity_check`, `check_ac`, `quality_check`, `validate` (read-ish),
  and `set_file_impact`/`get_file_impact`, `set_verify_commands`/`get_verify_commands`
  (writes gated by `REBAR_MCP_READONLY`). This lets MCP agents self-check quality and
  record the file-impact that `next-batch` consumes for conflict-aware scheduling.
- **Concurrency:** writes go through the existing locked path (I5); reads are replay
  (I3). No new mechanism.
- TDD: extend `test_surface.py` (RED for the new tools first), parity for the
  read/write tools, readonly-gating test.

**WS5e — `create` returns id + alias; minor ergonomics.**
- Make `create` return both id and alias so agents don't need a second `show`. In the
  library, return a small dict (or keep returning id but add `create_ticket(...,
  return_alias=True)`); harden the "last stdout line" scrape (`__init__.py:97`) by
  having the engine print a single machine-readable final line (e.g. `ID<TAB>ALIAS`).
- Add `reopen` as a thin convenience over `transition` (closed→open) — still
  optimistic-concurrency (I4).
- **Concurrency:** unchanged (create is already an append-only CREATE event).
- TDD: parity (create returns alias on all interfaces); reopen race test.

**Deliberately NOT adopted (with rationale):**
- **Committed shared search/memory index** — violates I6; rejected in favor of
  replay-derived/local-rebuildable (WS5a).
- **beads-style project "memory" (`remember`/`prime`)** — deferred; if pursued it
  must be modeled as append-only events on a reserved "project" pseudo-ticket
  (I1/I2), not a mutable shared file. Listed as a future item, not in scope now.
- **Background daemon / SQLite index (beads legacy)** — rejected; conflicts with the
  stdlib-only, git-as-source-of-truth, no-daemon design and adds a second source of
  truth that breaks I3/I6.

### WS6 — Documentation (P3)
`docs/concurrency.md` (from WS3), `docs/event-schema.md` (event types, filename
contract, replay/fork rules, compaction), `docs/architecture.md` (engine + 3
interfaces + reconciler), and a `CLAUDE.md`/agent-guide describing the MCP tool set
and the ready/next-batch/claim workflow. No concurrency impact.

---

## Sequencing & gates
1. **WS3 first** (codify + harden the concurrency model and add the
   concurrency-regression test) — it establishes the doctrine's executable guard
   that later workstreams run against.
2. **WS2** (extract heredocs, retire legacy) — needed before WS5c builds `claim` on
   the shared transition critical section.
3. **WS1** (rename) — large, mechanical; gated by the full reconciler + parity suite.
4. **WS4** (dead-code/packaging) — low risk, any time.
5. **WS5** (fitness features) — after WS2/WS3 so they inherit the verified write path
   and the concurrency-regression harness.
6. **WS6** docs throughout.

**Every PR gates on:** full pytest (incl. interface parity tier) ≥ current baseline
(1191/1/2), the shell suite (88 + 1 known-RED), the new concurrency-regression
test, and an explicit §0 doctrine-compliance note in the description.

## §V. Validation

### Per-workstream completion gate
A workstream is **done** only when all of the following hold (in addition to its TDD
RED→GREEN cycle): full pytest ≥ the then-current baseline (the count grows as tests
are added — never regress it), shell suite 88 + 1 known-RED, the §V
concurrency-regression test GREEN, and a recorded §0 doctrine-compliance note. Plus
the WS-specific checks:

| WS | Workstream-specific completion checks |
|----|----------------------------------------|
| WS1 | `git grep` returns **zero** residual `plugins.dso` / `dso_reconciler.` keys; the REBAR_*-preferred / DSO_*-fallback alias test passes (bash **and** Python surfaces); the ~862 reconciler unit cases (131 files) GREEN; the pass-lock cross-client CAS (`_advisory_lock.py`) still behaves identically post-rename; reconcile still routes (`python -m rebar_reconciler`). |
| WS2 | `ticket_txn` unit tests + the concurrent-transition race test GREEN; `test-ticket-subprocess-count.sh` unchanged; `DSO_TICKET_LEGACY` fully removed (`git grep` zero); exit-10 parity re-asserted across all 3 interfaces. |
| WS3 | detached-HEAD-local-ahead test GREEN; failed-push-keeps-local test GREEN; two-clone union+deterministic-replay test GREEN; reconvergence uses merge (not rebase); `docs/concurrency.md` present and matches code. |
| WS4 | `git grep` zero references to each deleted script; `__pycache__/` removed from the tree and gitignored; the `engine_dir()` real-path assertion is in place and tested. |
| WS5 | each sub-feature parity-GREEN across library/CLI/MCP; **WS5a** index-file-gitignored + never-staged-by-`git add` test (also covers `.cache.json`, closing I3a); **WS5c** two-claim race test GREEN; `test_surface.py` updated for the new MCP tools; readonly-gating test GREEN. |
| WS6 | docs present; the event-schema/concurrency docs are cross-checked against code (filenames, event types, lock mechanisms) and cite real `file:line`. |

### Final validation (after all workstreams, before merge to `main`)
1. **Clean-checkout full run.** From a fresh checkout: full pytest (incl. interfaces)
   + the complete shell suite; record counts; only the documented pre-existing RED
   (`test-ticket-delete-unlink-scan-fastpath.sh`) may fail.
2. **Fresh-venv wheel install.** Build the wheel, install into a clean venv, and
   verify the engine resolves to a **real on-disk dir** (no zipimport), exec bits +
   wordlist intact, and CLI / library / MCP each run end-to-end (mirrors the original
   extraction's fresh-venv verification).
3. **Cross-environment + cross-interface validation.** Run the two-clone
   concurrency-regression test against the *installed* package, plus a 3-interface
   coherence smoke (write via one interface, read via the other two) — proving
   concurrency holds across both clients/environments and interfaces post-change.
4. **Doctrine audit (I1–I9), automated where possible.** Assert: every event filename
   is `${ns}-${uuid}-${TYPE}.json` and unique; no committed `.cache.json`/index in the
   tracker; all writes go through the locked path; exit-10 surfaces uniformly; no
   *new* cross-client lock or shared mutable committed file was introduced (only the
   grandfathered reconciler pass-lock), and that the pass-lock's cross-client
   read-tip→commit→update-ref CAS still behaves identically after the WS1 rename
   touched `_advisory_lock.py`.
5. **DSO-residue gate.** `git grep` confirms no functional DSO-plugin residue remains
   (post-WS1: package/keys/env all renamed; only intentional, documented items, if any).
6. **Final opus diff review** for maintainability / reliability / concurrency before
   the branch merges to `main`.

## Risk summary
- Highest-risk step is the `dso_reconciler` package + `sys.modules` key rename (WS1.2)
  — atomic change, full reconciler suite as gate, revert plan ready.
- WS2/WS3 touch the locked write + sync paths; they are constrained to *preserve*
  the critical section and *strengthen* reconvergence, with the new
  concurrency-regression test as the backstop.
- WS5c (`claim`) is the only new *state-dependent* mutation; it reuses the verified
  optimistic-concurrency path rather than inventing one.
