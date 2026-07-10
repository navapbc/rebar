# Phase 1 — Discovery (work product: a finding + evidence pool, NO severity)

> Read this at the start of Phase 1. The orchestrator's operating principles remain in force —
> especially: findings carry **no severity and no confidence** (Verification assigns those), and
> every finding needs a `path:line` citation.

## Phase 0 — Orient (do this first, ~1 min)

- Identify languages, frameworks, build system, and the rough module/directory layout.
- Read the repo's agent guidance (`CLAUDE.md`, `CONTRIBUTING.md`) — you'll need it in Phase 5 to
  infer how this project tracks work (its tracker, ticket vocabulary, conventions).
- Locate any prior audit artifacts at `.rebar-janitor/`; if present, read `tools.md` and the last
  report to reuse known-good queries and track regressions/progress.
- Confirm whether `ast-grep` (`sg`), `semgrep`, `cloc`/`tokei` are installed. If not and they'd help,
  offer to install them (`brew install ast-grep semgrep tokei`). Don't block — fall back to `rg`/`grep`.
- Check for `.git`. If present, the temporal pass below is available; if absent, skip it and say so.

## Fan out — one concern per subagent (parallel, single message)

Give each agent: the repo root, its single concern, the Discovery output schema (below), and
authorization to run read-only analysis tools. Tell each to return findings only — not file dumps —
and **not to rate severity or confidence** (Phase 2 does that independently).

1. **Code debt & dead code** — TODO/FIXME/HACK debt, commented-out blocks, deprecated APIs still in
   use, unreachable/unused code, duplicated logic, stale feature flags, pinned-but-outdated deps.
2. **Code smells** — long functions, deep nesting, long parameter lists, god objects, primitive
   obsession, feature envy, shotgun surgery, magic numbers, boolean-trap params, copy-paste blocks.
3. **Architectural decay** — dependency cycles, layering violations (UI↔data direct calls), modules
   importing across boundaries they shouldn't, abstraction leaks, missing seams that force ripple
   change. **Include cross-session inconsistency** (AI-specific): multiple competing implementations
   of one capability from different agent runs — 3 HTTP client wrappers, 2 date utilities, mixed
   error-handling/naming for one concern.
4. **Separation of concerns** — business logic in controllers/views, I/O mixed with pure logic,
   config/secrets hardcoded, cross-cutting concerns (logging/auth/validation) scattered, single files
   doing many unrelated jobs.
5. **Documentation completeness & accuracy** — missing/stale READMEs, undocumented public APIs,
   comments contradicting code, drifted architecture docs, undocumented env/config, broken setup
   steps. Verify docs against code — flag drift, not just absence.
6. **Size & growth** — files/functions/classes over reasonable thresholds, directories with too many
   files, modules with too many responsibilities. Quantify with `cloc`/`tokei`/`ast-grep`.
7. **AI-generated-code & security smells** (see `references/patterns.md` for sources): **phantom /
   hallucinated dependencies** (imports unresolvable in lockfile/registry — slopsquatting); **security
   CWEs** (weak randomness, injection, XSS, insecure deserialization, hardcoded secrets), weighted
   toward recently-changed regions; **smelly/missing tests** (weak/absent assertions, no coverage on
   changed files); **volume-quality bloat** (large verbose additions with no refactor follow-up). Use
   `semgrep --config auto` / `p/secrets` and lockfile cross-checks.
8. **Spent optionality (future changeability)** — where the codebase's capacity to absorb the *next,
   still-unknown* change cheaply has been spent. Frame by **future change-cost**, not ugliness —
   "when the likely next change here arrives, how expensive is it, and why?" Two-sided:
   - **Under-structured (rigidity)** — one-way doors / baked-in choices expensive to reverse (a
     format/schema/protocol/vendor assumption threaded through many call sites); missing seams where
     the likely next change forces a ripple edit; structure and behavior so entangled no *safe*
     structural change is possible (Beck's asymmetry: **structural changes are reversible, behavioral
     ones are not** — so entanglement is what makes changeability un-recoverable; watch for
     modules/commits chronically interleaving the two).
   - **Over-structured (speculation)** — speculative flexibility added "just in case" that no caller
     uses: unused config knobs, premature plugin/strategy/DI/interface layers, a generic framework
     wrapping a single use. These spent optionality on a guess; flag them too.
   **The economic test (Beck):** the value is in the *option*, not the structure — early structure
   "throws away the time value," a loss *even if the guess turns out correct*. For over-structured
   findings ask not "is it used yet?" but "did this commit to a specific future *before* the
   information to choose it arrived?" **Gate on evidence of likely change:** intersect with the
   temporal hotspots below. Rigidity in stable, rarely-touched code is **not** a finding.

### Temporal decay pass (only if `.git` exists)

Run inline or as one more discovery agent — its output feeds the `likelihood` impact attribute in
Phase 2 and gates concern #8. Strongest decay predictors (GitClear 2025; arXiv 2605.02741):

- **Hotspots** — files BOTH complex AND frequently changed (`complexity × change-frequency`); the
  highest-leverage targets.
- **Churn** — share of lines rewritten ~2 weeks after being added (rising = thrash).
- **Clone / duplication trend** — is duplicated-block prevalence growing over recent history?
- **Refactor ratio** — share of changed lines moved/restructured vs added/copied (falling = decay).
- **Recently-changed regions** — files touched most in the last N commits.

Use `git log`/`git log --numstat`, `--since`, rename detection. See `references/patterns.md`. If
history is shallow/squashed, say so and report what's computable.

## Discovery output schema (per finding — NO severity, NO confidence, NO fix)

- `finding` — the problem stated as a **claim to verify** (not a verdict): what is wrong and where.
- `concern` — which of the eight concerns above.
- `location` — the file/symbol/region the finding is about.
- `evidence` — `path:line` citation(s) and/or a metric (LOC, param count, cycle, duplication count),
  or, for an absence finding, the rationale for why X is genuinely missing.
- `scenarios` — where this bites (the situation in which the harm shows up).
- `why_it_matters` — the maintainability/risk/changeability consequence if left unaddressed.

Do **not** include severity, a confidence score, or a suggested fix — those belong to Phase 2
(scoring) and Phase 3 (remediation).

**Gate to Phase 2:** a pooled, deduped list of finding+evidence objects across all concerns. Then
read `phases/verification.md`.
