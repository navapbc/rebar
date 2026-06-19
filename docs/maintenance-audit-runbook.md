# Maintenance / code-health audit runbook

A repeatable recipe for the periodic "principal-engineer maintenance audit" of
rebar: detect code debt, smells, architectural decay, separation-of-concerns
violations, doc drift, and unit/directory size growth. This documents the tools,
the commands, and the **multi-perspective subagent** method so the next run is a
re-run, not a re-invention.

> **Method.** Run the deterministic scans below ONCE into a scratch dir, then
> fan out one subagent per *perspective* (size, duplication, architecture,
> complexity, docs, dead-code, tests) — each reads the shared scan artifacts and
> digs into its lane only. Interpretation (essential vs accidental complexity,
> parallel-by-design vs real duplication) is the subagents' job; the scans are
> just signal. Finally, spot-verify every High/Critical finding first-hand before
> relaying it — subagents occasionally over-claim.

## Tools used (all confirmed available 2026-06-18)

| Tool | Version | Install | What it surfaces |
|------|---------|---------|------------------|
| `ruff` | 0.15.6 | (project dev dep) | Lint + the **extended advisory ruleset** below (complexity, magic values, boolean traps, dead args, suppressible excepts) |
| `radon` | 6.0.1 | `.venv/bin/pip install radon` | Cyclomatic complexity (`cc`) + maintainability index (`mi`) |
| `vulture` | 2.16 | `.venv/bin/pip install vulture` | Dead code (unused vars/functions/imports) |
| `ast-grep` (`sg`) | 0.43.0 | preinstalled | Structural pattern search (duplication shapes, mutable defaults, broad excepts) |
| `semgrep` | 1.165 | preinstalled | Generic structural patterns (fallback for ast-grep) |
| `jscpd` | via `npx` | `npx jscpd` | Token-level copy-paste clone detection |
| Serena MCP | — | project-configured | LSP-backed `find_referencing_symbols` — authoritative "is this symbol used?" (beats grep for dead-code confirmation) |

`mypy`/`pyright` are NOT installed locally (mypy is a declared dev dep but absent
from `.venv`); the project gates a scoped mypy run in CI only. Type-coverage was
not audited this run — add `mypy` to the venv if a type-health pass is wanted.

## The deterministic baseline scans

Run from repo root. Dump everything into a scratch dir so the subagents share it.

```bash
OUT=/tmp/rebar-audit   # or $CLAUDE_JOB_DIR/tmp in a job
mkdir -p "$OUT"

# 1. LOC per file (the module-size policy metric — matches the CI size report's `wc -l`)
find src -name '*.py' -exec wc -l {} + | sort -rn > "$OUT/loc.txt"
# over the 800 soft cap:
find src -name '*.py' -exec wc -l {} + | awk '$1 > 800 && $2 != "total"' | sort -rn

# 2. files per directory (junk-drawer detector)
find src -type d | while read d; do n=$(find "$d" -maxdepth 1 -name '*.py' | wc -l); echo "$n  $d"; done | sort -rn

# 3. cyclomatic complexity (C+ blocks) and the worst (D/E/F) ranked
.venv/bin/radon cc src/rebar -s -n C --total-average > "$OUT/radon_cc.txt"
grep -E ' - [DEF] \(' "$OUT/radon_cc.txt" | sort -t'(' -k2 -rn   # worst first

# 4. maintainability index (anything below A)
.venv/bin/radon mi src/rebar -s | grep -vE ' - A '

# 5. dead code
.venv/bin/vulture src/rebar --min-confidence 80 > "$OUT/vulture.txt"   # high-confidence
.venv/bin/vulture src/rebar --min-confidence 60                        # wider, more FPs

# 6. extended advisory lint (NOT the project gate — interpret, don't enforce)
ruff check src --select C90,SIM,PERF,RUF,PLR,PLW,TRY,TID,ARG,N,A,DTZ,RET,PTH,FBT --statistics > "$OUT/ruff_extended.txt"
ruff check src --select C901 2>&1 | grep -oE 'src/[^:]+' | sort | uniq -c | sort -rn   # complexity by file

# 7. duplication (token clones)
npx jscpd src/rebar --min-tokens 50 --min-lines 8 --format python --reporters json --output "$OUT/jscpd"

# 8. tech-debt markers, broad excepts, churn hotspots
grep -rnE '# *(todo|fixme|xxx|hack|workaround)' -i src --include='*.py'
grep -rn 'except Exception' src --include='*.py' | wc -l
git log --pretty=format: --name-only -200 | grep -E '^src/.*\.py$' | sort | uniq -c | sort -rn | head -20
```

### Useful ast-grep / semgrep patterns

```bash
ast-grep -l python -p 'except Exception:' src                  # broad excepts (then classify pass/log/raise)
ast-grep -l python -p 'def $F($$$ =[]$$$)' src                 # mutable default args (was 0 — keep clean)
ast-grep -l python -p 'urllib.request.Request($$$)' src/rebar/_engine/rebar_reconciler/   # repeated transport shape
grep -rn 'def _rebar_env\|def _env_int\|_SEVERITY_RANK = {' src/rebar/   # known copy-paste helpers
```

## Caveats learned this run (don't repeat these mistakes)

- **`RUF100` (unused-noqa) is an artifact** when you run a *narrower* `--select`
  than the project's gate (`E,F,W,I,UP,B`). 122 showed up and were all false —
  ignore RUF100 unless running the project's exact select set.
- **The project tree is clean for its own gate** (`ruff check src tests` passes
  for `E,F,W,I,UP,B`). The extended ruleset above is *advisory* — findings are
  smells to weigh, not violations.
- **`vulture` flags MCP/config getattr-dispatched fields as dead** (e.g.
  `McpConfig.readonly` read via `getattr` in `_mcp_gate`). Confirm with Serena
  `find_referencing_symbols` + grep over `src/` AND `tests/` before believing any
  dead-code claim. Properties and `@register_step`-decorated functions are common
  false positives.
- **Many `except Exception` are best-effort by design** (push, capability shims)
  and commented as such. Classify by disposition (pass/continue vs log vs raise);
  only `pass`/`continue` that mask *wrong answers* (not degraded ones) are bugs.
- **PLR0913 (too-many-args) is mostly false** here — the wide signatures are
  keyword-only filter façades mirroring the CLI surface. Look for *mutable
  accumulator out-params* instead (the real arg smell).
- **Run an adversarial validation pass before publishing.** A second set of
  subagents that try to *refute* each finding (read the cited lines, check the
  failure trigger actually fires) is high-value: in the 2026-06-18 run it held
  the *structural* findings (graph reimplements the write path; optionality tests
  always skip; the duplication clusters) but corrected several *interpretation*
  findings. The pattern: claims of the form "this `except` produces a WRONG (not
  merely degraded) answer" or "this global RACES under the MCP server" are the
  ones most often overstated — verify the exact trigger (does `ready_to_work`
  even traverse cycles? is `set_cli_overrides` ever called from `mcp_server.py`?
  — both were "no", collapsing two findings). Severity-correct: doc-only drift is
  Medium, not High, unless it breaks installs/behavior.

## Baseline numbers (2026-06-18, for drift comparison next run)

- 150 `.py` files in `src/`, ~40,879 LOC; 327 test files, ~67,772 LOC (1.66:1).
- **8 files over the 800 soft cap** (docs table documented only 4 — see audit).
- Worst complexity: `reconcile_once` F(82), `compute_mutations` F(61),
  `next_batch.compute` F(55), `_apply_inbound_update` F(52), `fsck_recover_cli`
  F(44). 102 `C901` hits.
- Dead code: only 2 vulture-100% items; ~10 genuinely-unreferenced functions
  (mostly `bridge_fsck.enumerate_*` dead parallel impl + reconciler stubs).
- `except Exception`: 135 sites, 0 real bare `except:`, 0 mutable default args.
- Default test suite collects 2,483 tests; coverage floor 50% vs ~61% measured.
</content>
</invoke>
