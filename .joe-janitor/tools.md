# joe-janitor playbook — rebar

Durable, reusable commands/patterns tuned for this repo. Append, don't overwrite.

## Repo facts (as of 2026-07-02)
- Python pkg `src/rebar`: **240 files, ~70K LOC**, 486 test files (was 180/52K/376 on 06-24 — +34% in 8 days).
  Full git history (not squashed), 736 commits over 24 days.
- Module-size policy: target 200–500 LOC/file, soft cap **800**. CI gate at `.github/workflows/test.yml:106-133`
  recomputes ALL `src/rebar/**.py` > 800 EVERY run and `comm -23`s vs the allowlist — so an EXISTING file that
  grows past 800 fails (strict `>`, 800 passes). "New" = new *offender*, not new-to-git. **The only blind spot is
  the allowlist itself: allowlisted files have NO growth ratchet** (attest.py grew 268→821 unchecked in 8 days).
- Allowlist now 6 entries (the 06-24 splits removed `_cli/__init__.py`, `applier.py`, `outbound_differ.py`,
  `_engine_support/reads.py`): `__init__.py` (1113), `reconcile.py` (1485), `config.py` (1310),
  `llm/plan_review/attest.py` (821), `llm/workflow/lint_refs.py` (816), `mcp_server.py` (1024).
- Baseline sha for 06-24 audit = `3040aa0cf`. **numstat undercounts moves/renames** — for true per-file deltas use
  `git show 3040aa0cf:<path> | wc -l` vs current, not `git log --numstat` sums (attest.py: numstat +418, real +553).
- Serena MCP (Pyright LSP) is configured — prefer `find_symbol`/`find_referencing_symbols` over grep for
  symbol navigation when available.
- **Always verify findings adversarially** (a second agent re-derives from source; reproduce logic bugs with a
  `python -c` snippet under the venv). The 06-24→07-02 run refuted 0 but downgraded 2 bug severities on re-check.

## Size & growth
- `tokei src --sort lines` (install: `brew install tokei`).
- Largest files: `find src -name '*.py' -not -path '*__pycache__*' -exec wc -l {} + | sort -rn | head -25`.
- Longest functions per file: small python `ast` script over `FunctionDef`/`AsyncFunctionDef` end_lineno-lineno.
- Dir bloat: `find src -name '*.py' | sed 's|/[^/]*$||' | sort | uniq -c | sort -rn`.

## Temporal (git)
- Touch-count hotspots: `git log --numstat --format='' | awk 'NF==3 {c[$3]++} END{for(f in c) print c[f],f}' | sort -rn | head -20`
- Recent-frontier (last N): `git log --numstat --format='' -n 50 | awk 'NF==3{c[$3]+=$1+$2}END{for(f in c)print c[f],f}' | sort -rn | head -20`
- Churn ratio: aggregate `git log --numstat` added+deleted per file ÷ current `wc -l`.
- Refactor/deletion ratio: `git log --numstat --format='COMMIT' -n 100` then sum adds vs deletes (healthy here: 0.14–0.21).
- New-then-deleted: `git log --diff-filter=D --name-only --format=''`.

## Competing-implementation hunts (this repo's cross-session smell)
- Canonical JSON / hashing: `rg -n "json.dumps.*sort_keys|content_hash|sha256" src/rebar`.
  Canonical home is `src/rebar/_store/canonical.py` (`ensure_ascii=False`). Drift seen in `mutation.py` (`ensure_ascii=True`).
- Retry/backoff: `rg -n "max_retries|RETRY|backoff|range\(3\)|_MAX_RETRIES" src/rebar`. 7+ variants found.
- Timestamps: `rg -n "isoformat|strftime|now\(timezone" src/rebar` — `.isoformat()` (+00:00) vs `strftime+Z` drift.

## Security / AI-code
- `rg -n "shell=True|yaml.load|pickle|eval\(|exec\(|hashlib.md5|random\." src/rebar` (all currently benign — see report).
- HMAC correctness: confirm `hmac.compare_digest` (not `==`) — `src/rebar/signing.py`.
- Phantom deps: extract top-level imports, diff vs `pyproject.toml` deps+extras+stdlib. KNOWN ISSUE 2026-06-24:
  `jsonschema` is `dev`-only and `referencing` undeclared, but imported by `schemas/__init__.py`, `llm/findings.py`,
  `llm/workflow/interpreter.py`. Re-check whether moved to a runtime extra.
- `semgrep --config p/python --config p/secrets src/rebar` — NOT yet run; reasonable future confirmation pass.

## Bare-except sweep
- `ruff check --select=BLE src/rebar` (or `rg -n "except Exception|except BaseException" src/rebar`). ~99 unguarded as of this run.

## Docs drift
- Architecture over-cap table `docs/architecture.md:194-204` drifts vs real `wc -l` — re-verify each row.
- `docs/exit-codes.md:19` cites stale `ticket_txn.py` (now `_commands/txn.py`).
- README CLI block: verify against `src/rebar/_cli/__init__.py` SOURCE (the installed pipx `rebar` binary LAGS the checkout).

## New hunts that paid off (2026-07-02)
- **Gate ALLOW_LLM bypass**: `has_llm_steps` (`llm/workflow/runs.py:285-308`) doesn't recurse v3 bare-array
  `branch.then/else` / `loop`/`map` bodies → returns False for BOTH shipped gate YAMLs → MCP `run_workflow`
  skips the `_allow_llm()` fence. Reproduce: load `has_llm_steps`, run on `gates/*.yaml`. (CRITICAL, empirical.)
- **Allowlist ratchet**: `for f in $(rg -l ...); wc -l` allowlisted files vs their 06-24 size — unbounded growth.
- **Import SCC**: AST graph incl. function-local imports + Tarjan. Largest SCC ~69 modules (convention-dependent:
  34 submodule-precise / 69 pkg-`__init__` / 140 full-runtime). Recommend an import-linter grandfather+ratchet gate.
- **Private cross-module imports into gates**: `rg "from rebar.llm.completion import _" src/rebar/llm/workflow` —
  gate_ops imports `_child_closure_findings`/`_deterministic_child_failure`/`_reconcile` (mandatory gate, no contract).
- **Fail-open vs fail-closed asymmetry**: completion child-cert (`completion.py:73-76` fail-OPEN) vs
  `attest._attested_delivered:668-677` (fail-CLOSED) on the same store-read error class.
- **Truthy-convention outliers**: `_map_legacy_env` (`config.py:876-879`) — `REBAR_NO_SYNC` inverts vs `_as_bool`.
- **Editor XSS**: `rg "json.dumps" src/rebar/llm/workflow/editor.py` — inline `<script>` interpolation, no `</script>` escape.
- `semgrep --config p/python --config p/secrets src/rebar --timeout 120` — RAN this time; 4 hits, all FP
  (md5 throttle-marker, 3 restrictive chmods). Clean.

## Known deliberate patterns — DO NOT re-flag
- `acli*.py` (5 files) = one client split across mixins, not competing clients.
- `_attestation.py` = GPG commit verify (not HMAC); `attest.py` reuses `rebar.signing` verbatim.
- Three `config.py` namespaces (root/reconciler/llm) are distinct concerns.
- `plan_review` finding shape ≠ `llm/findings.py` shape — intentional (`passes.py:7-17`).
- `lint_refs.py` (816, allowlisted): natural split seam is sub-100-LOC — defer per policy floor.
- `review_kernel/` = clean shared extraction consumed by both gates (import dir consumer→kernel), NOT a 3rd impl.
- `attest.py`/`pass1.py` bare `json.dumps` for signature basis: single-sourced + self-consistent; migrating to
  `canonical_str` would INVALIDATE existing attestations (byte-compat pinned at `attest.py:44-51`). Converge NEW sites only.
- BLE001 sweep DONE (epic `ring-gun-jot`, closed): 252/258 `except Exception` carry justified noqa; enforced tree-wide.
- Phantom deps CLEAN: `jsonschema`/`referencing` now core deps; full AST import-diff = zero undeclared.
- 57 zero-assert tests = assert-by-raising schema idioms, not smelly.
