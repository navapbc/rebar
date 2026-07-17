# Janitor patterns — starter playbook

Reusable commands/patterns for codebase-health audits. Copy what's useful into the audited repo's
`.rebar-janitor/tools.md` and tune thresholds per project. Prefer read-only invocations.

## Tool install (macOS)

```sh
brew install ast-grep semgrep tokei cloc ripgrep
# semgrep alt: pipx install semgrep
```

- **ast-grep (`sg`)** — fast structural/AST search & lint across many languages. Best for syntax-aware smells.
- **semgrep** — rule-based static analysis; large community ruleset (`--config auto`/`p/ci`).
- **tokei / cloc** — line/file counts per language and per directory (size & growth).
- **ripgrep (`rg`)** — text scans (comments, TODOs, hardcoded strings).

## Size & growth (quantify first)

```sh
tokei --sort lines                                  # totals by language
cloc --by-file --quiet . | sort -k5 -n -r | head    # biggest files by code lines
# Largest source files:
find . -type f \( -name '*.ts' -o -name '*.py' -o -name '*.go' -o -name '*.java' \) \
  -not -path '*/node_modules/*' -not -path '*/.git/*' -exec wc -l {} + | sort -rn | head -30
# Directories with the most files (fan-out / god-package signal):
find . -type d -not -path '*/.git/*' -not -path '*/node_modules/*' \
  -exec sh -c 'echo "$(ls -1 "$1" | wc -l) $1"' _ {} \; | sort -rn | head -20
```

Suggested starting thresholds (tune & state in report): file > 400 LOC, function > 50 LOC,
class > 300 LOC, params > 5, nesting depth > 4, files-per-dir > 25.

## Code debt (ripgrep)

```sh
rg -n --no-heading -i '\b(TODO|FIXME|HACK|XXX|WONTFIX|DEPRECATED)\b'
rg -n --no-heading '^\s*//.*\b(if|for|while|return|function)\b'   # commented-out code (C-like)
rg -n --no-heading '@deprecated|Deprecated\('                      # deprecated API decls
```

## Code smells (ast-grep examples — adapt language/pattern)

```sh
# Long parameter lists (JS/TS): functions with 6+ params
sg --lang ts -p 'function $F($P1,$P2,$P3,$P4,$P5,$P6,$$$) { $$$ }'
# console.* left in source (TS/JS)
sg --lang ts -p 'console.$M($$$)'
# Empty catch blocks (swallowed errors)
sg --lang ts -p 'try { $$$ } catch ($E) { }'
# Python bare except
sg --lang py -p 'try:
    $$$
except:
    $$$'
# Magic numbers in conditions (TS) — review hits, expect noise
sg --lang ts -p 'if ($X $OP $N) { $$$ }'
```

semgrep for broad coverage:

```sh
semgrep --config auto --quiet --error-on-findings=false .   # community rules, no fail
semgrep --config p/secrets --quiet .                         # hardcoded secrets
```

## Architecture / separation of concerns

```sh
# Cross-layer imports (example: views importing the db layer directly)
rg -n --no-heading "import .*(db|database|repository)" --glob '**/views/**' --glob '**/components/**'
# Business logic in controllers (heuristic — review hits)
rg -n --no-heading "(SELECT |INSERT |UPDATE |fetch\(|axios\.)" --glob '**/controllers/**'
# Dependency cycles (JS/TS): npx madge --circular src
# Python import graph: pydeps / import-linter contracts
```

## Documentation accuracy

```sh
rg -n --no-heading "TODO|FIXME" --glob '*.md'                 # debt in docs
# Public symbols missing docstrings (Python): use ruff D rules or interrogate
interrogate -v .                                             # docstring coverage
# Undocumented env vars: diff referenced env vars vs documented ones
rg -o --no-heading "process\.env\.[A-Z_]+|os\.environ\[['\"][A-Z_]+" | sort -u
```

## Duplication

```sh
# jscpd works across many languages
npx jscpd --min-lines 10 --min-tokens 70 --reporters console .
# semgrep also has clone-ish patterns; ast-grep for known repeated shapes
```

## AI-generated-code & security smells (concern 7)

Backed by recent research on agentic-development decay — see "Sources" below.

```sh
# Phantom / hallucinated dependencies (slopsquatting): imports not in the lockfile/manifest.
# JS/TS — list imported bare packages, then diff against package.json deps:
rg -o --no-heading "from ['\"]([^.'\"][^'\"]*)['\"]" -r '$1' --glob '*.ts' --glob '*.js' \
  | sed 's#/.*##' | sort -u
# Python — imported top-level modules vs requirements/pyproject:
rg -o --no-heading "^\s*(?:import|from)\s+([a-zA-Z0-9_]+)" -r '$1' --glob '*.py' | sort -u
# Then confirm each third-party name resolves in the lockfile AND the real registry
# (npm view <pkg> / pip index versions <pkg>). Unresolvable + believable name = Critical.

# Security CWEs / secrets (weight toward recently-changed files from the temporal pass):
semgrep --config auto --quiet --error-on-findings=false .
semgrep --config p/secrets --quiet .
rg -n --no-heading -i "(api[_-]?key|secret|password|token)\s*[=:]\s*['\"][^'\"]+['\"]"
sg --lang js -p 'Math.random()'            # weak randomness (CWE-330) in security context

# Smelly / missing tests — assertion-light test files:
rg -n --no-heading -c "(expect\(|assert|\.should)" --glob '**/*{test,spec}*' \
  | awk -F: '$2<2 {print}'                  # test files with <2 assertions
```

## Temporal decay pass (concern: trends — needs git history)

Empirically the strongest decay signal. See CodeScene's hotspot model and GitClear's churn data.

```sh
# Most-changed files over recent history (change frequency):
git log --since='12 months ago' --name-only --pretty=format: | sort | uniq -c | sort -rn | head -30
# Hotspots = high change-frequency AND high LOC (complexity proxy). Cross the list above with `wc -l`.
# Churn proxy — lines added vs deleted per file (high deletes-soon-after-adds = thrash):
git log --since='3 months ago' --numstat --pretty=format: | \
  awk 'NF==3 {a[$3]+=$1; d[$3]+=$2} END {for (f in a) print a[f], d[f], f}' | sort -rn | head -20
# Refactor vs add ratio — moved/renamed lines (needs rename detection):
git log --since='3 months ago' -M -C --numstat --pretty=format: | head
# Authors per file (bus-factor / knowledge silo signal):
git log --pretty=format:'%an' -- <file> | sort -u | wc -l
```

If history is squashed/shallow (`git log --oneline | wc -l` is tiny), report that and degrade to
snapshot-only.

## Sources (why these checks exist)

Cite these in `.rebar-janitor/tools.md` so future runs know the rationale. Verify arXiv IDs before
quoting in any deliverable — some are very recent.

- Fowler & Beck, *Refactoring* — canonical code-smell catalog (God Class, Long Method, Duplicated Code).
- Lehman's Laws of Software Evolution — increasing complexity / declining quality over time.
- Li, Liang, Avgeriou et al., "Symptoms of Architecture Erosion in Code Reviews" — arXiv 2201.01184.
- GitClear, "AI Copilot Code Quality 2025" — ~4× clone growth, falling refactor ratio, rising churn:
  https://www.gitclear.com/ai_assistant_code_quality_2025_research
- Fu, Liang, Tahir et al., "Security Weaknesses of Copilot-Generated Code" — ~30% snippets w/ CWEs;
  arXiv 2310.02059.
- "Vibe Coding in Practice: Flow, Technical Debt…" — flow-vs-debt tradeoff; arXiv 2512.11922.
- Socket.dev, "Slopsquatting" — hallucinated-package supply-chain attacks:
  https://socket.dev/blog/slopsquatting-how-ai-hallucinations-are-fueling-a-new-class-of-supply-chain-attacks
- "AI-Generated Smells" (volume-quality bloat, cross-session inconsistency) — arXiv 2605.02741.
- Prior art for this skill's shape: CodeScene (hotspots, CodeHealth) and the `tech-debt-skill`
  Claude Code skill (github.com/ksimback/tech-debt-skill — living-doc memory, "looks bad but is fine").

## Notes on tuning

- Expect false positives from heuristic `rg`/`sg` patterns — verify each before reporting.
- Record in `tools.md` which patterns had high signal for THIS repo and which were noisy, plus the
  thresholds you settled on. That's the value that compounds across runs.
