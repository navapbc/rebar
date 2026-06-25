# joe-janitor playbook — rebar

Durable, reusable commands/patterns tuned for this repo. Append, don't overwrite.

## Repo facts (as of 2026-06-24)
- Python pkg `src/rebar`: ~180 files, ~52K LOC, 376 test files. Full git history (not squashed).
- Module-size policy: target 200–500 LOC/file, soft cap **800**. CI gate at `.github/workflows/test.yml`
  fails when a **new** file > 800 and not in `.github/module-size-allowlist.txt` (strict `>`, so 800 passes).
- Grandfathered over-cap (allowlist): `__init__.py`, `_cli/__init__.py`, `applier.py`, `outbound_differ.py`,
  `reconcile.py`, `_engine_support/reads.py`, `config.py`, `llm/workflow/lint_refs.py`, `mcp_server.py`.
- Serena MCP (Pyright LSP) is configured — prefer `find_symbol`/`find_referencing_symbols` over grep for
  symbol navigation when available.

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

## Known deliberate patterns — DO NOT re-flag
- `acli*.py` (5 files) = one client split across mixins, not competing clients.
- `_attestation.py` = GPG commit verify (not HMAC); `attest.py` reuses `rebar.signing` verbatim.
- Three `config.py` namespaces (root/reconciler/llm) are distinct concerns.
- `plan_review` finding shape ≠ `llm/findings.py` shape — intentional (`passes.py:7-17`).
- `lint_refs.py` (815, allowlisted): natural split seam is sub-100-LOC — defer per policy floor.
