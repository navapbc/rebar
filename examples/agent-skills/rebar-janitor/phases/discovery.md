# Phase 1 — Discovery (work product: a finding + evidence pool, NO severity)

> Read this at the start of Phase 1. The orchestrator's operating principles remain in force —
> especially: findings carry **no severity and no confidence** (Verification assigns those), and
> every finding needs a `path:line` citation.

## Phase 0 — Orient (do this first, ~1 min)

- Identify languages, frameworks, build system, and the rough module/directory layout.
- Read the repo's agent guidance (`AGENTS.md`, `CONTRIBUTING.md`) — you'll need it in Phase 5 to
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
   **When the repo is a rebar store, also run `rebar metrics --since <date> --until <date>
   --output json`** and cite its `code_health`-lens values as durable, historical evidence —
   module-size distribution, oversized-module count and the module-size trend vs the locked
   cap, short-term churn, the refactor-to-addition ratio, and cap-change events — the trend
   complement to the one-shot `cloc`/`tokei`/`ast-grep` pass, not a replacement. A metric that
   reports `unavailable` has no accrued data yet: **treat it as "no data", never as zero**, and
   fall back to the one-shot tools for that dimension.
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

9. **Unwired & partially wired functionality** — the highest-yield concern in practice: values that
   are *accepted and then silently dropped*. A parameter declared and never read; a CLI flag parsed
   into a variable no caller consumes; a request field the callee omits from the object it builds; a
   `pass  # accepted, no-op`; a keyword arg placed after `**kwargs` so it can never reach the callee;
   a capability protocol with zero implementers; a module whose sole entry point is gated behind a
   file with no producer. These are *worse* than dead code, because the surface advertises a promise
   the code does not keep — docstrings, `--help` text and ADRs go on asserting it.
   - Enable the linter's unused-argument rule **in a scratch config, for detection only** (Ruff
     `ARG*`, or equivalent) and read the violation list as a candidate pool. Do not propose turning
     it on permanently without evidence — survey what comparable projects actually do.
   - For each candidate, resolve the full path from surface to consumer. A value that reaches *a*
     struct is not wired; check the struct is read. The strongest finding shape is: the field exists,
     a consumer reads it, and the producer omits it — the value is *actively discarded*, not merely
     unimplemented.
   - Cross-check every finding against docs, `--help` text, MCP tool descriptions, and ADRs. A false
     promise in documentation is what converts a dormant parameter into a trust problem.
   - Also sweep for **dead surfaces that are honestly documented** — they are real but low value.
     Rank them last rather than dropping them silently.

10. **Comment quality & rationale placement** — comments have an important but narrow role: proximate
    just-in-time context, in clear concise language. Everything else has a better home. The taxonomy
    that makes each call decidable:

    | Content | Belongs in |
    |---|---|
    | history, "why we changed this", incident references | the ticket system |
    | architectural decisions and their rationale | an ADR |
    | logic and control flow | the code itself |
    | data shapes, field semantics, validity rules | schema |

    Measure first: comment lines as a share of source, and **comment growth rate vs code growth
    rate** — the second is the decay signal. Find ≥20-line rationale blocks that cite no ADR; a block
    that is a complete ADR written inline is the clearest possible finding.
    - **Shard by subsystem.** One agent cannot triage a large tree's commentary. Give each shard a
      directory scope and a line budget.
    - **Every shard's most valuable output is its `do_not_move` list**, not its removal list.
      Measured external-API facts, replay-compatibility constraints, security invariants and
      anti-refactor warnings cannot be re-derived from code and must survive verbatim. Make
      "`do_not_move` list appended to the playbook **before** any edit lands" an explicit AC on each
      shard ticket — a protection record that arrives after the deletion is worthless.
    - Verify staleness claims rather than assuming: a comment saying "STUB, bodies to be implemented"
      next to a symbol imported by 13 modules is a finding; a comment that merely *looks* dated is
      not.
    - Honest counterweight to report: comment triage often does **not** rescue the files you most
      want rescued, because their bulk is exactly the load-bearing kind. Say which files it will not
      help.

11. **Test-suite health** — audit the suite as a system, not for coverage percentage. Ask:
    - **Wasteful duplication** — several tests asserting one identical behavior or contract. Measure
      the clone rate in tests and compare it to source; a large gap is the signal.
    - **Change-detector tests** — tests that fail on any edit while proving no behavior; assertions
      pinned to incidental output, formatting, or call order.
    - **Justified regression confidence** — is the confidence the suite implies actually earned?
      Look for tests exercising code paths that are themselves dead, and for fixtures synthesized
      solely to reach an unreachable branch.
    - **Optimizations that cut maintenance or wall-clock without cutting coverage** — where coverage
      means behavioral, contractual, integration and regression coverage, not a line-coverage number.
    - Check whether the suite has any size, duplication, or skip gate at all. A suite that grew to
      multiples of source with no gate is the finding.
    - **Shard this one too**, by test-tree region, and validate findings as they arrive rather than
      in one batch at the end (see Verification).

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
- `concern` — which of the eleven concerns above.
- `location` — the file/symbol/region the finding is about.
- `evidence` — `path:line` citation(s) and/or a metric (LOC, param count, cycle, duplication count),
  or, for an absence finding, the rationale for why X is genuinely missing.
- `scenarios` — where this bites (the situation in which the harm shows up).
- `why_it_matters` — the maintainability/risk/changeability consequence if left unaddressed.

Do **not** include severity, a confidence score, or a suggested fix — those belong to Phase 2
(scoring) and Phase 3 (remediation).

**Gate to Phase 2:** a pooled, deduped list of finding+evidence objects across all concerns. Then
read `phases/verification.md`.
