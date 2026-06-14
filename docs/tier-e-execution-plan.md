# Tier E execution plan + handoff (delete the bash dispatcher — the LAST tier)

Story: **adult-oxide-slave** (`8784-c2d7-c395-4478`). Tier E kickoff: **lad-cipher-arena**
(`4387-8fae-9c8b-444b`). Canonical durable plan: `docs/bash-migration.md` §7. This file is the
opus-reviewed cluster decomposition + the live handoff state. End-state AC: **one implementation +
three facades (lib/CLI/MCP); zero standalone shell tests; no embedded heredocs; no `_engine/*.sh`.**
Tier E has NO kill-switch — it is a structural cutover; rollback is `git revert`.

## STATUS (2026-06-14, session 2) — read this FIRST

**Done this session, committed to local `main` (NOT pushed — awaiting user direction):**

| Cluster | Commit | What |
|---|---|---|
| E5 | `fd94e4b1` | `bridge-status` + `purge-bridge` in-process (`_engine_support/bridge.py`, `_commands/purge_bridge.py`); 10 byte-parity tests. |
| E5b | `fb1b7fa7` | reconciler rewired off bare `event_append`/`ticket_reducer` → `rebar._store`/`rebar.reducer`; reconcile launch `python3`→`sys.executable`. **LIVE-DIG probe PASSED** (DIG-5764 create→validate-all-fields→inbound→delete, zero residue). Opus-reviewed APPROVE-WITH-NITS (fixes applied). |
| E5c | `c7b36bd0` | deleted the bare `_engine/event_append.py` + its obsolete test. |
| E6a | `b3cbfe6a` | rewired the rebar PACKAGE off the shims (`reducer/_processors.py`→`_engine_support.resolver`; interface/reconciler tests→`rebar.graph`/`rebar.reducer`). |
| E6.5a (6/6) | `11c0784e`…`656958a1` | ALL in-process→engine subprocess deps severed: alias-compute, alias-resolve, link `--dry-run`, un-archive, bridge-fsck in-process; bridge-probe + reconcile launch via `sys.executable`. **`rebar._run` + `_cli._passthrough` are now DEAD** (every CLI subcommand runs in-process; dispatcher fully bypassed). Two live-DIG probes passed (DIG-5764 field round-trip; bridge-probe 6-step), zero residue. |
| E8 batch 1 | `f7c6f5d8` | 4 GAP bash suites → in-process pytest (cache-gitignored, push-policy e2e, help-overview drift, fsck PUSH_PENDING); 11 tests green. |

Full non-integration suite green at **1805 passed / 0 failed** (E6.5a milestone,
clean — no false positive). NB: the `_no_repo_commits` guard trips if a `git commit`
runs DURING a background full-suite run; do not commit while one is in flight.

### RESUME HERE → E8 batch 2, then E7 (atomic cutover), then E9

**E8 batch 2 (translate the remaining GAP suites, additive, before E7):**
- `test-ticket-list-has-tag.sh` — only the `detected_by:`∩bug-type rule if not already in `test_list_filters.py`.
- `tests/test-reconciler-scratch-exclude.sh` — reconciler `--dry-run-enumerate` excludes `.scratch/` (launch `sys.executable -m rebar_reconciler` with `engine_env`; reconciler STAYS).
- `test-format-ticket-id-symlink.sh` — resolve/format with a symlinked tracker.
- `tests/test-ticket-init-idempotent.sh` — re-init idempotent `.git/info/exclude` upgrade (no dup lines).
- `test-ticket-transition-open-children-perf.sh` — CORRECTNESS only, BOUNDED (~20-30 children), drop wall-clock asserts (flaky).
- `tests/integration/ticket-id-collision/run.sh` — `@pytest.mark.integration`, BOUNDED (~500-1000 ids), id/alias uniqueness + deterministic replay.

**E7 (ONE atomic commit — see the grounded plan below):** delete `_engine/rebar`+`ticket`+all 36 `.sh`+the dispatcher-only `.py` helpers (`ticket-reducer.py`/`ticket-graph.py`/`ticket-reads.py`/`ticket-commands.py`/`ticket-delete-unlink-scan.py`/`ticket-alias-*.py`/`ticket-list-descendants.py`/`ticket-unblock.py`)+the now-shim `ticket-bridge-fsck.py`+`ticket_txn.py`+the 5 compat shims; SAME commit delete ALL `tests/scripts/test-*.sh` + the few elsewhere + `tests/lib/*.sh` + `tests/scripts/test_bash_suites.py` collector + the 15 `test_eN_*.py` parity tests + `test_e3_txn_bytes.py` + machinery tests (`test_engine_dir.py`, `tests/_engine_path.py`, `test_ticket_txn.py`) + the conftest engine-`sys.path` inserts. KEEP: `_engine/rebar_reconciler/` + `jira-capability-probe.py` + `resources/ticket-wordlist.txt` (genuine tools the reconciler/probe/alias still use via `sys.executable`+engine_env). In `_engine.py`: `engine_dir`/`engine_env`/`wordlist_path` must SURVIVE (the reconciler + bridge-probe subprocess launches + `reducer._alias` need them); delete only `dispatcher()`/`run()`. Verify full pytest green (pytest-only, zero `tests/**/*.sh`). **opus-review the E7 cutover.**

**E9:** docs (architecture offender table→empty; bash-migration §7 DONE; help golden now `_cli/_help`-anchored) + exhaustive dogfood + close `adult-oxide-slave` then `nervy-hold-dip`.

⚠ A worker subagent crashed mid-draft on E8 batch 2 (socket error); redo batch 2 fresh.

### CRITICAL — E6.5a prerequisites (grounded 2026-06-14; MORE than the original E6/E7 text)

Before ANY engine deletion (E7), every in-process `rebar.*` path that still
subprocesses the engine MUST be severed, or deleting the engine silently breaks
production. Grounded list (grep of `src/rebar` excl. `_engine/`+reconciler):
1. ✅ `composer._compute_alias` → `reducer._alias.compute_alias` (byte-parity verified).
2. ⬜ `_engine_support/resolver.py` → still subprocesses `ticket-alias-resolve.py`; port the alias/jira_key scan in-process.
3. ⬜ `composer.link_cli` `--dry-run` → subprocesses `ticket-link.sh --dry-run` (normal link is already in-process via `link_core`); port or drop the dry-run preview.
4. ⬜ `transition._unarchive` (`archived→open`) → subprocesses `ticket-revert.sh`; port the REVERT-latest-ARCHIVED logic in-process (reuse `rebar._commands` revert core).
5. ⬜ `rebar.bridge_fsck()` (+ MCP) → `_run(["bridge-fsck"])` → dispatcher → `ticket-bridge-fsck.py`; port to `rebar._engine_support.bridge_fsck`, add a `_cli` `bridge-fsck` arm.
6. ⬜ `bridge-probe` (`_cli._passthrough`) → dispatcher → `jira-capability-probe.py`; port to `rebar._commands.bridge_probe`, add a `_cli` arm. **LIVE-DIG gate binds (now user-authorized).**
   Plus: `validate._raw_tickets` subprocesses `$TICKET_CMD list` ONLY when `TICKET_CMD` is injected (a TEST seam; production is in-process `list_states`) — update those tests in E8. `reads.py`/`transition._short_head`/`_init.py` subprocess only `git` (benign, stays).

### Then (opus-grounded ordering): E8 → E7 → E9
- **E8 (before E7):** write the ~11 TRANSLATE pytest tests (in-process) for the confirmed coverage GAPs BEFORE deleting bash sources (fsck PUSH_PENDING; has-tag `detected_by`∩bug; reconciler `.scratch/` exclusion; init exclude idempotent upgrade; close O(open-children); id-collision opt-in; symlink-resolve; cache-gitignored; help-drift; push-policy residue) + 1 `sync`-invariant regression test. ~56 suites are DELETE-redundant (covered by `test_eN_*`/reducer/graph/interface tiers), ~17 DELETE-machinery.
- **E7 (atomic, one commit):** delete `_engine/rebar`+`ticket`+all 36 `.sh`+dispatcher-only `.py` helpers+the 5 shims+`ticket_txn.py`; SAME commit delete all `.sh` suites + `tests/lib/*.sh` + the `test_bash_suites.py` collector + the 15 `test_eN_*.py` parity tests + machinery tests (`test_engine_dir.py`, `_engine_path.py`, `test_ticket_txn.py`) + the conftest engine-`sys.path` inserts + `engine_env`/`engine_dir`/`dispatcher`/`run` (keep/relocate `wordlist_path` for `_alias`).
- **E9:** docs (architecture offender table→empty; bash-migration §7 DONE; help golden now `_cli/_help`-anchored) + exhaustive dogfood + close `adult-oxide-slave` then `nervy-hold-dip`.

---

## STATUS (2026-06-13) — prior session

**Done, green, pushed to `origin/main` (HEAD `482bbed8`):**

| Cluster | Commit | What |
|---|---|---|
| E0 | `3a53e20` | argparse CLI package `rebar._cli` (help/overview/error byte-pinned via package-data goldens) + auto-init/freshness middleware (`_ensure_initialized` port) + `tests/golden/cli_help/`. No cutover. |
| E1 | `c64c889` | `cli.py:main` cut over to `rebar._cli.main`. Dispatcher now reachable only via the transitional category-B passthrough. |
| E2.1 | `a3a7eb2` | `get-file-impact`, `get-verify-commands` → `rebar._engine_support.field_reads`. |
| E2.2 | `ee29e91` | `exists`, `resolve`, `format` → `rebar._engine_support.lookups`. |
| E2.3 | `d81650a` | `list-descendants` → `rebar._engine_support.descendants`. |
| E2.4 | `482bbed` | `clarity-check`, `check-ac`, `quality-check`, `summary` → `rebar._engine_support.gates`. |
| CI fix | `9760b2b` | Pre-existing CI: bash-suite REPO_ROOT `.tickets-tracker` leak (3 arity tests sandboxed) + macOS tmp-cleaner race (pinned `--basetemp`). |

E2 (read-ish/quality) is COMPLETE and closed. Checkpoint full suite: **1660 passed, 0 failed**.

**In-process now (argparse routes these to `rebar.*`, library rewired off `_run`):** all category-A
(reads + leaf writes) + `get-file-impact`, `get-verify-commands`, `exists`, `resolve`, `format`,
`list-descendants`, `clarity-check`, `check-ac`, `quality-check`, `summary`. The matching bash stays
as a **parity-pinned second impl** (see Sequencing below).

**Still bash (category-B passthrough to the dispatcher) — REMAINING WORK:** `transition`, `reopen`,
`claim`, `compact`, `compact-all`, `scratch`, `delete` (→ **E3**); `init`, `fsck`, `fsck-recover`
(→ **E4**); `bridge-status`, `purge-bridge` (→ **E5**). Plus reconciler rewire (E5b), `ticket_txn`
relocation (E5c), shim deletion (E6), dispatcher deletion (E7), suite translation (E8), close (E9).

### RESUME HERE → E3 (write/lifecycle), coupled to E5c

`transition`/`reopen`/`claim` wrap `ticket_txn.py` (already Python). But `ticket_txn.py` lives in the
engine dir and does a **bare `import event_append`** (the standalone `_engine/event_append.py`). It
**cannot** be imported in-process by adding the engine dir to `sys.path` — that violates the
`test_engine_dir.py::test_library_path_exposes_no_generic_top_level_engine_names` guard (Tier D
invariant). So **E3 is coupled to E5c**: relocate `ticket_txn.py`'s critical section into
`rebar._commands`/`rebar._store` (using `rebar._store.event_append`/`lock` directly) so it's
importable as `rebar.*`, then port `transition`/`reopen`/`claim` in-process over it. **This touches the
single locked write path + the optimistic-concurrency exit-10 contract — do the opus-reviewed sub-plan
FIRST (per the working pattern) and re-run the concurrency matrix** (writer storm, claim storm,
two-clone cross-clone convergence: `tests/integration/test_concurrency_regression.py`, `-m integration`).

## The established per-command pattern (followed for E0–E2; keep doing this)

1. **Characterize** the bash arm empirically (run the dispatcher over a fixture store; capture exact
   stdout/stderr/exit for success/miss/arity/empty + both `--output` modes).
2. **Port** to `rebar.*` reusing `rebar.reducer`/`rebar.graph`/`rebar._commands`/`rebar._store`/
   `rebar._engine_support` (resolver, output, reads). Never re-implement; error strings from one place.
3. **Route** argparse in-process (add a `frozenset` routing set in `rebar/_cli/__init__.py`; pick the
   init policy to match the dispatcher arm: full / `--init-only` / none) and **rewire the library**
   function off `_run` to the in-process helper.
4. **Dual-run parity test** (`tests/interfaces/test_e2_*.py`): compare `rebar._cli.main(argv)`
   (capsys) vs `bash dispatcher(argv)` (subprocess, `engine_env`) byte-for-byte over one store. This is
   the no-kill-switch equivalent of the Tier B/C/D dual-run; the bash is the pinned second impl.
5. **Gate:** fast interfaces tier + live-binary smoke; **commit green**; record a per-command rebar
   child ticket (claim → set_file_impact → comment → close). Full suite at cluster boundaries.

**NO bash is deleted in E2–E5.** The `.sh` suites invoke `$DISPATCHER` (`_engine/ticket`) DIRECTLY, not
the argparse `rebar`, so a port does not redirect them — the bash impl stays live + tested until the
dispatcher AND its suites retire together. Deletion consolidates in **E7** (dispatcher + machinery +
`ticket-lib*.sh` + standalone `.sh`) and **E8** (`.sh` suites → pytest), sequenced so the dispatcher
and the suites that call it retire together. Each parity test is deleted with its bash second-impl.

## Cluster decomposition (each = one child ticket under the kickoff)

- **E3 — write/lifecycle:** `transition`/`reopen`/`claim` (needs E5c `ticket_txn` relocation first),
  then `compact`/`compact-all` (port `ticket-compact*.sh` over `rebar._store`), `scratch` (filesystem),
  `delete` (destructive: STATUS(deleted)+tombstone+UNLINK events + atomic commit through the write
  core; `--user-approved` guard; children guard; reuses `ticket-delete-unlink-scan.py` logic). Write
  commands re-run the concurrency matrix.
- **E5c — relocate `ticket_txn.py`** into `rebar._commands`/`rebar._store`; stop `import event_append`
  (bare); delete `_engine/event_append.py` only after the reconciler (E5b) + `ticket_txn` stop
  importing it. Sequence with E3.
- **E4 — `init`/`fsck`/`fsck-recover`** (HIGHEST RISK; isolated soak each): orphan-branch + worktree
  bootstrap, index.lock cleanup, dangling-commit/interrupted-rebase recovery. No in-process
  predecessor. Byte-parity goldens + recovery/concurrency tests. The auto-init middleware (§1a, already
  in `rebar._cli._init`) calls the ported `init` once it lands (transitionally subprocesses
  `ticket-init.sh` today).
- **E5 — `bridge-status`/`purge-bridge`** (bridge surface; `bridge-fsck`/`bridge-probe` already py).
- **E5b — rewire `rebar_reconciler`** off the bare `ticket_reducer` → `rebar.reducer` and bare
  `event_append` → `rebar._store.event_append` (drop the `sys.path` dances in `inbound_translate.py`),
  and off `engine_env`. Decide reconcile's launch (in-process entry preferred). Update `cli.py`/library
  `reconcile`. **⚠ LIVE-DIG JIRA GATE binds here (and E5):** before close, run a live end-to-end probe
  against the real Jira **DIG** project (`JIRA_PROJECT=DIG`; `JIRA_URL`/`JIRA_USER`/`JIRA_API_TOKEN`
  are in the env), validating EVERY field bidirectionally (outbound rebar→Jira via
  `apply_outbound`/`outbound_differ`/`adf`; inbound Jira→rebar via
  `apply_inbound`/`inbound_differ`/`inbound_translate`; field list from `jira_fields.py`), all edge
  cases, create→validate→delete throwaway DIG issues with cleanup verified. PAUSE for the user's
  go-ahead before the first live DIG writes.
- **E6 — delete the compat shims** (`ticket_reducer/`, `ticket_graph/`, `ticket_reads.py`,
  `ticket_resolver.py`, `ticket_output.py`) after rewiring every importer (engine `.py` helpers,
  `reducer/_processors.py`, `_engine_support/output.py`, the graph/interface tests, the
  `ticket-id-concurrency/run.sh` integration helper) to `rebar.*`. Assert-gone test per shim.
- **E7 — delete the dispatcher + machinery:** `_engine/rebar`, `engine_env()`/PYTHONPATH machinery,
  `engine_dir()` zipimport assertion, jq prerequisite, flock(1)/mkdir discovery. Surviving genuine py
  helpers (`jira-capability-probe.py`, `ticket-bridge-fsck.py`) get an in-process invocation path.
  Retire the machinery tests in the SAME commit (`test_engine_dir.py`, `tests/_engine_path.py`,
  `test_ticket_txn.py`, `test_schema_coverage.py` engine refs). Assert `_engine/*.sh` count == 0.
- **E8 — translate the bash harness to pytest:** ~107 `.sh` total (94 under `tests/scripts/` + 13
  elsewhere incl. `tests/integration/ticket-id-concurrency/run.sh`). Delete
  `tests/lib/{suite-engine,assert}.sh` + the collector. Gate: zero `tests/**/*.sh`; full pytest green.
- **E9 — docs + close:** `docs/architecture.md` offender table → empty + engine-import-boundary note;
  `docs/bash-migration.md` §7 → DONE; exhaustive live-dogfood validation (probe isolated + PROBE_LIVE,
  full suite + integration matrix + mixed-version, all three interfaces, fsck clean, perf, goldens);
  soak-evidence comment; close adult-oxide-slave.

## Environment / gotchas (heed all)

- **Live build:** on-PATH `rebar` is the editable working tree (`pipx install --editable . --force`;
  the `.pth` points at `src/`). Revert with `pipx install nava-rebar --force`.
- **Dev pytest venv:** `/tmp/rebar-dev` (`python3 -m venv /tmp/rebar-dev && /tmp/rebar-dev/bin/pip
  install -e '.[dev]'`). Run `pytest -m "not integration"`; integration matrix needs `-m integration`.
- **Always** pass a fixed `--basetemp=/tmp/rebar-bt-X` (macOS tmp-cleaner race). `pytest | tee` reports
  tee's exit — read the actual `N passed, M failed`. Never `git stash` with uncommitted migration work.
- Committed event bytes: `json.dumps(ensure_ascii=False, separators=(',',':'), sort_keys=True)` no
  newline (== `jq -S -c`). Preserve through any write-path change.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Commit to
  `main` directly is allowed; push/tag/publish follow the user's direction.

## Per-cluster gates + risks

Gates: characterize → port (reuse, don't re-implement) → byte-parity (stdout/stderr/exit, text +
`--output json`) → cut over → dogfood soak → retire in clean green commits. Preserve I1–I9, the
exit-code contract (10/75/…), the single-reducer `show==list==search` shape (f026), JSON-schema
conformance, ANSI/text byte-pins. Module-size: 200–500 target, 800 cap, no <100-LOC shards; test files
<1000 LOC.

Top risks: (1) reconciler shim + `engine_env` ImportError vector (E5b — rewire before E6/E7);
(2) `ticket_txn`/bare `event_append` half-migrated hybrid (E5c — relocate, delete bare module last);
(3) init/fsck bootstrap parity (E4 — isolated soak); (4) compat-shim deletion ordering (E6 — grep-gated
assert-gone, rewire before delete); (5) test undercount — 107 `.sh`, not 83.
