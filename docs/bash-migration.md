# Bash→Python strangler-fig migration plan

> **STATUS: COMPLETE.** All tiers (A–E) are done — the bash engine is deleted and
> rebar is one Python implementation behind three facades (CLI / library / MCP)
> with zero bash dependency. This document is retained as the historical record of
> how the migration was executed; for the current architecture see
> [architecture.md](architecture.md).

Story: `adult-oxide-slave` (8784-c2d7-c395-4478) · parent epic `nervy-hold-dip`
(Audit 2026-06-09) · source analysis: Rec 1 + Rec 9 of the 2026-06-09
architecture review (risks R3/R4/R5/R8/R9).

This is the committed, durable plan: tier ordering, kill-switch discipline,
the per-command porting protocol, and the validation matrix that prevents
regression — with cross-session (same clone, many processes) and
cross-environment (many clones, one remote) concurrency as the headline
constraint. `docs/architecture.md` § Module-size policy points here; each tier
spawns its own child tickets from this plan when started.

## 0. Current state

Done already (the proven playbook):

- **Tier A (reads) is complete** — `rich-glare-sake` closed. CLI/library/MCP
  reads all go in-process through `src/rebar/_reads.py` →
  `rebar._engine_support.reads` → `rebar.reducer`; the bash read shims and
  their heredocs are deleted; the read-freshness policy (≤1/min best-effort
  fetch, `--no-sync` / `REBAR_NO_SYNC` opt-out) is uniform across interfaces.
- **The kill-switch lifecycle is proven in-repo**: `REBAR_NATIVE_READS`
  (introduced 7d53bc5b, removed a93885ed) — port behind a switch, pin parity
  (`tests/interfaces/test_native_read_parity.py`, 26 cases), flip the default,
  soak, then delete the switch *and* the parity test in one commit. The test
  harness is the durable artifact; the switch is temporary governance.
- **Already Python, stays put**: `ticket_txn.py` (transition/claim critical
  section), `rebar.reducer` (pure replay), `rebar.graph`, the reconciler, the
  schema/output layer (`ticket_output.py` — parsing lives once, no bash dup).
- **CI exists** (`.github/workflows/test.yml`): pytest tiers + warn-only
  module-size report. Every step below rides a green pipeline.

What remains (~13.1k bash LOC in `src/rebar/_engine/`, ~30 embedded
`python3` heredocs):

| File | LOC | Heredocs | Tier |
|------|----:|---------:|------|
| `ticket-lib-api.sh` | ~2,370 | 1 | B (command bodies) |
| `ticket-lib.sh` | ~2,025 | 5 | D (write/sync core) + B callers |
| `ticket-next-batch.sh` | ~954 | 2 | C |
| `validate-issues.sh` | ~945 | 10 | C |
| `rebar` (dispatcher) | ~580 | — | E (deleted last) |
| `ticket-link.sh` | ~532 | 2 | B |
| `ticket-transition.sh` | ~486 | 2 | B (shell wrapper; core already `ticket_txn.py`) |
| `ticket-list-epics.sh` | ~453 | — | C |
| everything else (`ticket-create.sh`, `ticket-comment.sh`, fsck, compact, scratch, migrate-*, …) | ~4,800 | ~8 | B/C/D as listed below |

## 1. Non-negotiables (every tier, every command)

1. **No store or schema changes.** The event log format
   (`docs/event-schema.md`), the `tickets` orphan-branch layout, and the
   locking design are ratified sound and explicitly out of scope. A port
   reproduces behavior; it never "improves" semantics in the same change.
2. **Invariants I1–I9** (`docs/concurrency.md`) hold at every commit —
   append-only events, globally-unique filenames, side-effect-free reads,
   optimistic concurrency (exit 10), single locked write path, no new
   cross-client locks, rebuildable derived data, skew-tolerant ordering.
3. **Exit-code contract** (`docs/exit-codes.md`) is part of parity: 10 =
   optimistic-concurrency mismatch, 75 = rebase/merge guard, etc. Agents
   script against these.
4. **Output contract**: structured outputs conform to the canonical JSON
   Schemas (`src/rebar/schemas/`); human/text outputs are byte-pinned during
   each command's dual window.
5. **One locked write path at all times** (I5). During any dual-impl window,
   both implementations must serialize on the *same* lock (§6 interop rule).
6. **Module-size policy** applies to the new Python: 200–500 LOC target,
   800 soft cap, split only on real call-graph seams, no <100 LOC shards.
7. **Default stays bash until parity is green + dogfood soak passes.** Flips
   are deliberate, per-tier, reversible-by-env-var for one release.

## 2. The per-command protocol (repeat for every port)

This is `REBAR_NATIVE_READS` generalized. Each ported command goes through
all seven steps; no step is skipped because a command "looks trivial":

1. **Characterize.** Identify the command's bash test suite(s) under
   `tests/scripts/test-*.sh`; if coverage has holes (an output branch, an
   error path, an exit code with no assertion), extend the *bash* suite
   first, against the *bash* impl. The bash suite is the spec.
2. **Port.** Implement in `rebar._commands.<name>` (new package), reusing
   `rebar.reducer` / `rebar.graph` / `rebar._engine_support.{resolver,output}`
   in-process. Writes go through the single event-append seam (§4).
   Error-message strings come from one place — never hand-mirrored between
   bash and Python (the Tier A lesson).
3. **Switch in.** Route through the tier's kill-switch (§3) at every
   interface: the dispatcher arm checks it for the CLI; `rebar/__init__.py`
   checks it for library/MCP. Default: `bash`.
4. **Dual-run parity gate.** CI runs the command's bash suite against BOTH
   values of the switch; both must pass, and a golden-capture harness pins
   stdout/stderr/exit code byte-identical across impls for a fixed scenario
   matrix (text and `--output json`). The interface-parity and
   schema-conformance tiers stay green throughout.
5. **Flip.** Change the default to `python` in one commit. The switch
   remains as the rollback lever.
6. **Soak.** Dogfood on this repo's own store (this repo tracks its work in
   rebar, so the flip is exercised immediately): the soak window is the rest
   of the release cycle for Tiers B/C commands, one FULL release for Tier D.
   Soak exit criteria: `rebar fsck` clean, no `PUSH_PENDING` anomalies, no
   exit-code regressions in CI or live use, perf gates (`tests/perf/`) green.
7. **Retire.** In one commit (the a93885ed pattern): delete the switch, the
   bash implementation, its heredocs, and the dual-run wiring; translate the
   bash suite's assertions to pytest and delete the `.sh` suite (Rec 9). A
   parity test never outlives its second implementation; a bash suite is
   never deleted before its command's switch is gone.

## 3. Kill-switch design

One switch per tier (not per command — per-command switches multiply the
test matrix without adding rollback value; within a tier, commands flip
individually by being routed under the switch only once ported):

| Switch | Tier | Values | Initial default |
|--------|------|--------|-----------------|
| ~~`REBAR_LEAF_WRITES`~~ | B | `bash` \| `python` | retired 2026-06-11 |
| ~~`REBAR_COMPUTE`~~ | C | `bash` \| `python` | retired 2026-06-12 |
| `REBAR_WRITE_CORE` | D | `bash` \| `python` | `bash` |

(Tier E has no switch — it deletes the dispatcher after D's switch is gone.)

- **Parsing idiom**: the `REBAR_PUSH` idiom — case-insensitive,
  whitespace-stripped; unrecognized values fall back to the current default
  with a one-line warning to stderr (never a hard failure: an env typo must
  not take down an agent fleet).
- **Single source of truth**: a tiny `rebar._switch` helper owns the parse on
  the Python side; the dispatcher's bash parse is pinned to it by a parity
  test (same inputs → same resolution). When a tier's switch dies, both
  sides go with it.
- **A command not yet ported ignores the switch entirely** (always bash), so
  `REBAR_LEAF_WRITES=python` mid-tier is safe — it selects Python only where
  Python exists.
- **Lifecycle**: introduced with the tier's first port; default flipped per
  command (B/C) or once for the whole core (D); deleted one release after
  the tier's last flip. The deletion commit is the tier's done-marker.

## 4. Tier B — leaf writes

> **Status: DONE — Tier B retired (2026-06-11).** All eleven leaf-write commands
> (`comment`, `set-file-impact`, `set-verify-commands`, `tag`, `untag`, `archive`,
> `create`, `edit`, `link`, `unlink`, `revert`) are implemented in
> `rebar._commands` and reached in-process by the dispatcher (→
> `ticket-commands.py`) and `rebar.__init__` (library/MCP); writes route through
> the single seam (`ticket-append-event.sh`). After the soak (full dual-run parity,
> 229-test interface tier, 77/77 live full-surface probe, fsck clean) the default
> flipped to python (step 5) and the switch + the 9 bash `ticket_*` leaf functions
> in `ticket-lib-api.sh` (~1260 LOC) + the dual-run wiring were deleted (step 7).
> **Python is now the sole leaf-write implementation.** The leaf `.sh` suites stay
> as python characterization tests (they exercise the dispatcher, now python); the
> dead standalone `ticket-*.sh` wrappers + the "zero standalone shell harness"
> end-state are deferred to Tier E (per §7). The reconciler's own bash event-write
> path (Rec 7a) is separate from these dispatcher/library leaf writes.


**Commands**: `comment`, `tag`/`untag`, `set-file-impact`,
`set-verify-commands`, `archive`, `scratch set|get|clear`, then the larger
event-composers: `create`, `edit`, `link`/`unlink`, `revert`. (`transition`/
`claim`/`reopen` keep their existing `ticket_txn.py` critical section; Tier B
only ports their bash argument/output wrappers.)

**Shape**: each command becomes a function in `rebar._commands/` that
(1) parses/validates args, (2) resolves ids via
`rebar._engine_support.resolver`, (3) composes the event JSON in Python,
(4) appends it through **one narrow seam**:

- **The event-append seam.** Tier B does NOT port the locked write path.
  Add a thin, stable bash entrypoint `ticket-append-event.sh
  <ticket_id> <staged-event-file> <commit-msg>` wrapping the existing
  `write_commit_event` → `_flock_stage_commit` (`ticket-lib.sh`). Python
  Tier B commands subprocess this seam. This keeps exactly one locked write
  path (I1/I2/I5 untouched) while all the parsing/validation/composition
  bash above it is deleted. The same seam is what Rec 7a routes the
  reconciler's direct event writes through — one fix serves both.
- When Tier D lands, the seam's *interior* swaps from bash to
  `rebar._store.event_append` under `REBAR_WRITE_CORE`; Tier B commands
  don't change again.

**Per-command gates**: the command's bash suite dual-run (step 4 of §2);
`tests/interfaces/` parity (CLI = library = MCP over one store); schema
conformance for `--output json`; for `link`, the hierarchy-promotion cases
(promotion + `REDIRECT:` note emission) are part of the golden set.

**Concurrency gate for the tier**: a mixed-impl writer storm on one clone —
N parallel writers split across `REBAR_LEAF_WRITES=bash` and `=python`
processes → event count exact, `fsck` clean, no lost commits. This is cheap
insurance even though both sides share the bash core in this tier.

**Exit criteria**: `ticket-lib-api.sh` (~2,370 LOC) and the per-command
`ticket-*.sh` wrappers deleted; `REBAR_LEAF_WRITES` deleted after one
release of flipped defaults; each retired suite translated to pytest.

## 5. Tier C — compute-heavy read-side bash

> **Status: DONE — Tier C retired (2026-06-12).** All three compute commands are
> Python: `list-epics` retired via deprecation (worm-burr-fly); `next-batch`
> (sure-beech-taunt) is `rebar._engine_support.next_batch` (selection/render/CLI) +
> `next_batch_files` (the text→file-path heuristic, split on its own seam);
> `validate` (gawk-grove-site) is `rebar._engine_support.validate` (normalize →
> score → render → CLI) + `validate_checks` (the nine checks, each returning an
> ordered `Finding` stream so the severity buckets reproduce byte-for-byte). All
> reuse `rebar.reducer`/`rebar.graph` + the shared `list_states` read in-process —
> the library/MCP's last subprocessing reads are gone.
>
> The lifecycle ran the full §2 protocol: ported behind `REBAR_COMPUTE`, dual-run
> byte/semantic-identical (text/terse/verbose stderr + exit byte-pinned; JSON by
> schema+semantic per §1.4), **flipped** to python (step 5), **soaked** — the
> dogfood full-surface probe passed 141/141 isolated AND live (store verified
> unchanged), live cross-checks byte/semantic-equal, `fsck` clean — then **retired**
> (step 7, gawk-grove-site child 785b-7c9f): deleted `ticket-next-batch.sh` +
> `validate-issues.sh` (~1,900 LOC, 12 heredocs), the `REBAR_COMPUTE` switch + the
> dispatcher `_compute_python` helper + both dual-impl arms + the `ticket_next_batch`
> /`ticket_validate` bash wrappers, in one commit. The dual-run parity suites were
> translated to python-only characterization (`test_next_batch_compute.py`,
> `test_validate_compute.py`); the `.sh` characterization suites stay (they now
> exercise the python dispatcher) and the "zero standalone shell harness" end-state
> is deferred to Tier E, mirroring Tier B.
>
> **One surfaced behavior change, intentional:** the bash `next-batch`
> `conflict_file`/`conflict_with` *diagnostic* was non-deterministic (it iterated an
> unordered `set`, so on a >1-file overlap the named file coin-flipped per run). The
> port iterates `sorted()`, making the diagnostic byte-stable — the selector's
> documented "deterministic" contract. Batch **composition** was never affected.

**Commands**: `next-batch` (`ticket-next-batch.sh`, ~954 LOC),
`validate` (`validate-issues.sh`, ~945 LOC), `list-epics`
(`ticket-list-epics.sh`, ~453 LOC); riders at the same shape:
`list-descendants`, `summary`, `clarity-check`/`check-ac`/`quality-check`.

These are read-only compute over replayed state — orchestration around
`rebar.reducer` + `rebar.graph` with formatting on top. Porting them:

- Reuses the Tier A read plumbing (in-process replay, uniform freshness
  policy, `--no-sync` opt-out) — `next_batch()` in the library currently
  still subprocesses bash; after the flip, MCP's `next_batch`/`validate`
  become in-process like every other read.
- Must reproduce **ordering and tie-breaking exactly**: bash `sort` vs
  Python `sorted` locale/stability differences, jq number formatting vs
  `json.dumps`, and `validate`'s finding bucketing/severity order are the
  known parity traps — the golden matrix pins them.
- `next-batch`'s conflict-aware scheduling over recorded file-impact is the
  one place agents' *parallel dispatch* depends on output determinism: the
  dual-run gate includes fixture stores with crafted file-impact overlaps
  and asserts identical batch composition byte-for-byte.

**Exit criteria (met 2026-06-12)**: the three big scripts deleted (~2,350 LOC, 12
heredocs — `list-epics` under worm-burr-fly, the two compute scripts under
gawk-grove-site), `REBAR_COMPUTE` retired, suites translated. The architecture.md
offender table lost both bash compute entries.

## 6. Tier D — the write/sync core (the crux; LAST before E)

> **Status: DONE — Tier D retired (2026-06-12).** The bash write/sync core
> (`_flock_stage_commit`, `write_commit_event`, `_push_tickets_branch`,
> `_reconverge_tickets`, `_check_no_rebase_in_progress`) is now `rebar._store`:
> `lock.py` (the ONE fcntl+mkdir dual-leg lock + 637b guard), `event_append.py`
> (canonical commit, exit 0/1/75), `push.py` (REBAR_PUSH modes + non-FF merge/stash
> dance + async), `sync.py` (reconverge). ALL writers share `rebar._store.lock`: the
> leaf-write seam (in-process), `ticket_txn` (transition/claim/reopen), compaction,
> and the reconciler-inbound writer — closing the `stiff-mop-lane` fcntl-vs-mkdir gap
> on every platform class. Read reconverge runs in-process; the dispatcher's one bash
> freshness caller delegates via a thin `ticket-sync.sh` shim.
>
> Lifecycle (full §2 protocol): ported behind `REBAR_WRITE_CORE`, dual-run
> byte/semantic-identical (committed bytes == `jq -S -c`; exit codes; push/sync
> behaviour), **flipped** to python, **soaked** (full-surface probe 141/141 isolated
> + live, store unchanged, fsck clean, 285+ interfaces/integration green), then
> **retired** (chilly-hyena-jet): deleted the bash core + the `ticket-append-event.sh`
> seam + `rebar._switch` (the last kill-switch — Tiers B/C/D all retired) + the
> bash-internal test suites (~5,240 LOC net), in one commit. Gates: `rebar._store`
> unit (15), the mixed-impl/all-python writer storm + claim storm, the two-clone
> concurrency-regression, push-policy (incl. async). Full non-integration suite
> 1555/0.
>
> **One deliberate deviation from the plan below:** the dual-leg **mkdir lock is
> KEPT** (not dropped at retirement). A gradual-upgrade fleet still has old bash-core
> peers (mkdir/flock); dropping it now could re-expose stiff-mop-lane against an old
> peer on a mkdir host. It converges on plain `fcntl` once the ecosystem has fully
> migrated — a release-level follow-up, not a code flip. (`REBAR_FORCE_MKDIR_LOCK`
> died with the bash core; the Python lock always takes both legs in the window.)

**Scope**: port `_flock_stage_commit`, `write_commit_event`,
`_push_tickets_branch`, `_reconverge_tickets`, and
`_check_no_rebase_in_progress` (`ticket-lib.sh` / `ticket-sync.sh`) into a
new `rebar._store/` package (`lock.py`, `event_append.py`, `push.py`,
`sync.py` — each well under the cap), behind `REBAR_WRITE_CORE=bash|python`.
`ticket_txn.py`'s lock acquisition merges into `rebar._store.lock` so the
whole system has ONE lock implementation when this tier completes.

**Semantics to preserve exactly** (each is a pinned test, not a comment):

- Lock: `LOCK_EX` on `.tickets-tracker/.ticket-write.lock`,
  `FLOCK_STAGE_COMMIT_TIMEOUT` (default 30s) × 2 retries; lock released only
  after commit.
- Rebase/merge guard (bug 637b): refuse with **exit 75, non-retriable**,
  staged temp cleaned up.
- Atomic same-filesystem rename of the staged event; `git add` +
  `git commit -q --no-verify` under the lock; `gc.auto=0`;
  failure exit codes 2/3 and their stderr strings.
- Push: `REBAR_PUSH=always|async|off` (default `always`), best-effort —
  a push failure never fails the write; ≤3 attempts; **merge-as-default** on
  non-fast-forward (fetch → guard check → `merge --no-edit` →
  stash/retry/pop on "would be overwritten"); `async` detaches and survives
  parent exit; `fsck` reports `PUSH_PENDING` when ahead of origin.
- Reconverge: ≤1/min throttle via the shared `/tmp/.ticket-sync-<repo_md5>`
  marker; merge-as-union; UUID-deterministic STATUS fork resolution
  (replayed identically by every clone, I8).

### The dual-window lock-interop rule

The platform matrix today: bash `_flock_stage_commit` uses util-linux
`flock(1)` where available, else an **mkdir lock** (`.ticket-write.lock.d`);
`ticket_txn.py` uses **`fcntl.flock`**. `fcntl.flock` contends correctly
with `flock(1)` (same `flock(2)` syscall, same file) but does **not**
contend with mkdir locking — already a live gap between bash writes and
`ticket_txn.py` on flock(1)-less macOS (bug `stiff-mop-lane`, fixed no
later than this tier).

Rule for the window where bash-core and python-core processes coexist on
one clone:

> **The Python core acquires BOTH mechanisms, in a fixed order:**
> `fcntl.flock(LOCK_EX)` on `.ticket-write.lock` first, then the mkdir lock
> `.ticket-write.lock.d`; release in reverse (mkdir leg in a `finally`).
> Bash holds at most ONE mechanism (flock(1) *or* mkdir), so no
> hold-and-wait cycle exists and deadlock is impossible, while mutual
> exclusion holds against bash on every platform class.

After `REBAR_WRITE_CORE` is deleted (no bash writers left), the mkdir leg
is dropped and the system converges on plain `fcntl.flock` everywhere.
`REBAR_FORCE_MKDIR_LOCK` keeps working against the bash side throughout the
window so the interop is testable on Linux CI, and dies with the bash core.

### Tier D validation matrix

Same-clone, cross-process (sessions):

- Mixed-impl writer storm: N parallel writers, half `REBAR_WRITE_CORE=bash`,
  half `=python`, plus concurrent `ticket_txn.py` claims → exact event
  count, `fsck` clean, zero `index.lock` failures, every claim storm yields
  exactly one winner + (N−1) exit-10 losers. Run on Linux (flock(1) path)
  AND with `REBAR_FORCE_MKDIR_LOCK=1` on the bash side (mkdir⊕fcntl
  interop), AND on macOS CI.
- Crash safety: `kill -9` mid-critical-section under each impl → fcntl lock
  auto-releases; the mkdir-leg staleness behavior matches bash today
  (timeout, manual recovery via `fsck-recover`) — preserved, not "fixed",
  in the port.
- `tests/scripts/test-mkdir-lock-stress.sh` extended to drive the Python
  core.

Cross-clone / cross-machine (environments):

- Extend `tests/integration/test_concurrency_regression.py`: two clones of
  one bare remote, **one clone on bash core, the other on python core**,
  interleaved writes + pushes → both converge to identical replayed state;
  STATUS forks resolve to the same winner on both; `PUSH_PENDING` surfaces
  and clears identically.
- The same two-clone harness across `REBAR_PUSH=always|async|off` on each
  side independently (9 combinations, sampled), including the
  non-fast-forward merge-retry and stash-dance paths under contention.
- Mixed *versions*: an old pipx-installed release (bash core) sharing a
  store with the new python core — same harness, because real fleets
  upgrade gradually. This is the strongest argument for byte-level
  semantics preservation: the remote store is the compatibility surface.

**Soak**: one FULL release dogfooding on this repo's own store with the
default flipped, concurrency suites green in CI continuously, before the
switch (and the bash core, ~2,000 LOC) is deleted.

## 7. Tier E — delete the dispatcher ✅ DONE

`cli.py` is a thin facade over `rebar._cli` — a real argparse CLI over the same
`rebar._commands` functions the library exports (three facades, one
implementation). Help / usage / error output is byte-pinned by goldens anchored in
`rebar._cli/_help`. Deleted in the cutover:

- The bash dispatcher (`_engine/rebar` + the `ticket` alias) and the dispatcher
  arm of `engine_env()` (the `REBAR_TICKET_CLI` default now points at the
  in-process CLI; `engine_env()`'s `PYTHONPATH` survives, scoped to the reconciler
  + Jira-probe subprocess launches).
- All `_engine/*.sh`, the `ticket-*.py` helpers, the compat shims
  (`ticket_reducer/`, `ticket_graph/`, `ticket_reads.py`, `ticket_resolver.py`,
  `ticket_output.py`), and the relocated write core (`ticket_txn.py` →
  `rebar._commands.txn`).
- The **jq prerequisite** and the **flock(1)/mkdir discovery** machinery. The
  unpacked-disk constraint (`engine_dir()` zipimport assertion) is **kept** — the
  reconciler + probe still exec as real files.
- The standalone shell test harness (`tests/lib/suite-engine.sh` + `assert.sh` +
  the collector); the suites are translated to in-process pytest.

End state reached (the story's AC): one implementation + three facades; zero
standalone shell tests; no embedded heredocs; no `_engine/*.sh`.

## 8. Sequencing, tickets, and rollback

```
B (leaf writes, one command at a time; event-append seam first)
   └─▶ C (next-batch → list-epics → validate; independent of B internally)
          └─▶ D (write core, after B has funneled all writes through the seam)
                 └─▶ E (dispatcher deletion)
```

B before C is convention, not dependency — C can start once the dual-run
harness (built for B's first command) exists. D strictly follows B (the
seam must be the only writer entry). E strictly follows D's switch
deletion. Bug `stiff-mop-lane` lands with D at the latest.

- **Child tickets**: each tier opens with a kickoff ticket that spawns one
  child per command (B/C) or per module (D), each carrying its bash suite
  name as the characterization gate and `set_file_impact` covering the
  `.sh` it retires — so `next-batch` keeps mixed-tier work from colliding.
- **Rollback levers**, strongest first: (1) the tier env-var, instant,
  per-process; (2) revert the default-flip commit; (3) the bash impl is
  still in-tree until step 7, so reverting the retirement commit restores
  it whole. After a tier's retirement commit, rollback is a release
  rollback — which is why retirement waits out the soak.

## 9. Risks and their pins

| Risk | Pin |
|------|-----|
| Byte-parity drift in text/error output | Golden matrix per command (stdout/stderr/exit), dual-run in CI until retirement |
| Ordering/locale (`sort` vs `sorted`), jq vs `json.dumps` number/escape formatting | Explicit fixture cases in the golden set (Tier C especially) |
| Mixed-impl lock escape on flock(1)-less platforms | §6 interop rule + `REBAR_FORCE_MKDIR_LOCK` storm test (bug `stiff-mop-lane`) |
| Mixed-version fleets on one remote store | Two-clone old-release × new-core harness (§6); store semantics never change |
| Push/merge edge paths (non-FF, stash dance, async orphan) regress silently | Contention harness exercises them deliberately; `PUSH_PENDING` asserted via `fsck` |
| Parity tests rot after their second impl dies | Retirement commit deletes switch + bash + parity wiring together (a93885ed pattern); review checklist item |
| New Python recreates god-units | Module-size CI report; `_store/` pre-split into lock/append/push/sync |
| Soak skipped under schedule pressure | Default-flip and retirement are separate commits; retirement PR must link the soak evidence (fsck/CI history) |
