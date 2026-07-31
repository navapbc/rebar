# ADR 0056 — Decompose `PydanticAIRunner.run()` along its real seams

**Status:** Accepted (epic 061c follow-on; renumbered 0055->0056, ADR 0055 was
concurrently claimed by the Jira-family sub-seam change) — chronic `runner.py` size pressure)
**Date:** 2026-07-31
**Relation:** Works WITHIN the module-size policy in `AGENTS.md` §"Module-size policy" and
`.github/module-size-limit.txt`. The 800-LOC per-FILE cap is UNCHANGED and still binding. Scope is the
runner cluster only; a per-FUNCTION gate was considered and is explicitly out of scope (see decision 1).

## Context

`src/rebar/llm/runner.py` has been split three times — `c1f4` (2026-07-18, −146 lines), task 2682
(−152), task 3a98 (−35) — and is at 689 lines, or **782** with task cc33's fallback-chain work
pending. Ticket 1484 relieved the same pressure in `gate_dispatch.py` and `orchestrator.py` weeks
earlier. The pattern recurs.

Measured at `97791145f`:

- `PydanticAIRunner.run()` spans lines **219–626 = 408 lines, 59% of the file**.
- Inside it: **227 code, 166 comment (40%), 15 blank**.
- The `try`/`except`/`finally` spine sits at 474 / 507 / 529 / 538 / 582. So the pre-call region is
  lines 220–473 = 254 lines — but that region is **50% comment: 113 code, 129 comment, 12 blank**.
- The `except` spine is 75 lines and is the DENSEST code region in the file: 58 code, 20% comment.

**Why the three prior splits did not hold.** Each extracted a *callee* — `structured_run.py`,
`providers.py`, `capabilities.py`, `anthropic_model.py`. Extracting a callee always satisfies a
per-FILE gate while leaving the calling method untouched, so the method keeps absorbing new work and
the file regrows. We repeatedly split the nouns while the pressure sat on the verb.

**A verified latent defect makes this urgent rather than merely tidy.** `ProviderSession(cfg)` is
constructed at `runner.py:301`; the `try` that guards it begins at **474**, and
`finally: provider_session.close()` at 582. Lines 301–473 are therefore OUTSIDE the guard, and
several of them raise by design (`_check_tool_capability` raises `LLMConfigError` at 325; cc33 builds
N clients before the guard). Anything raising there leaks an opened `httpx.AsyncClient`.
`ProviderSession.__enter__`/`__exit__` exist at `providers.py:295–299` and **are never used** —
`grep -c 'with ProviderSession' runner.py` returns 0 — while `providers.py:21` documents the opposite:
*"`runner.run()` wraps its body in `with ProviderSession(cfg) as session:`"*. The module docstring
describes a caller that does not exist.

**What an adversarial review corrected.** An earlier draft of this proposal claimed that *every* one
of ~20 commits touching `runner.py` was a pre-call "resolution" concern. That is false for at least
five — `8e4d6a46d` and `5144a9009` (usage/cost, post-call), `4291b9a56` (disposition classifier, in
the `except` arm), `3938c7ed2` (gate posture, in `preflight`), `c8aa1ecdd` (refusal fail-closed, in
the call section). The real figure is ~70%, and the correction changes the sequencing: the `except`
spine has a stronger claim to being extracted FIRST than a resolution-first ordering allowed.

That draft also claimed the decomposition would make resolution "pure and cheaply unit-testable".
It would not. The genuinely pure work is ALREADY extracted (`capabilities_for`, `cache_settings_for`,
`provenance_for`, `effective_max_tokens`, `effective_max_iterations`), and what remains inline is the
*impure glue that sequences construction around them*: model building opens HTTP clients, and
`capabilities_for` consumes the built model object. The purity claim is withdrawn.

## Decision

**1. SCOPE: this ADR covers the runner cluster only** — `runner.py` and the modules already split
out of it (`structured_run.py`, `providers.py`, `capabilities.py`, `anthropic_model.py`).

A per-FUNCTION length gate was considered as the durable fix and is **explicitly OUT OF SCOPE**.
The reasoning is recorded because it is the honest diagnosis of why this recurs: the 800-LOC cap is
per FILE, and extracting a callee always satisfies a per-file gate while leaving the calling method
untouched — which is precisely what `c1f4`, task 2682 and task 3a98 each did. A per-function gate
would have blocked `run()` at roughly line 300.

It is out of scope because it is not a runner change. MEASURED across 3,292 functions under
`src/rebar`:

| threshold | functions over it |
| --- | --- |
| 200 | 21 |
| 150 | 38 |
| 120 | 62 |
| 100 | 101 |

The ten worst span six subsystems (`_mcp_writes.register_write_tools` 352,
`plan_review.resign_plan_review` 303, `_mcp_reads.register_read_tools` 298, `_run_plan_review` 294,
`import_ndjson.import_tickets` 273, `review_bot.review_and_vote` 270). Any such gate is therefore a
project-wide policy change affecting 62 functions across the codebase, not a fix to this cluster.

**RESIDUAL RISK, stated plainly:** without it, nothing structurally prevents the decomposed `run()`
from regrowing, and this ADR could become split number four. The decomposition below makes regrowth
slower and more visible — a new concern lands in a themed module with a name, rather than as ten more
lines in a method nobody reads to the end — but it is a mitigation, not a guarantee. If the pattern
recurs a fourth time, the per-function gate is the answer and should be raised as project-wide work.

**2. Wrap the run body in `with ProviderSession(cfg) as session:`** — closing the verified leak and
making `providers.py`'s existing docstring true. This is a correctness fix, not a refactor, and it
lands first.

**3. Extract PLAIN FUNCTIONS with explicit parameters — NOT a frozen carrier object.**

An earlier draft proposed a frozen `ResolvedCall` dataclass threaded through seven resolvers. Rejected:
two of its seven fields (`model_settings`, `provenance`) are mutable dicts that are mutated after
construction, so the freeze is decorative exactly where it matters; and cc33 writes
`provider_provenance["ran_model"]` AFTER the `finally` block, which a frozen pre-call carrier cannot
express. The `replace()` ceremony would cost ~30–40 lines to relocate ~113. Extract instead:

    interpret_failure(exc, *, run_messages, req_limit, eff_max_iter, ...) -> Never
    build_model_settings(cfg, req, caps, resolved, *, model_override) -> dict
    build_usage_limits(cfg, req, UsageLimits) -> tuple
    build_agent_kwargs(cfg, req, tools, toolsets, model_settings, ...) -> dict

Introduce a carrier only if a signature exceeds ~6 parameters — not before.

**4. Ordering is fixed by real data dependencies, not by tidiness.** `assert_gated("agentic
filesystem tools")` (`runner.py:256`) is a fail-closed security check and MUST stay ahead of provider
construction (301); moving tool resolution after it would open a client before the refusal path and
leak it. `_check_tool_capability` (324) is a cross-check consuming BOTH tools and the built model
(and, under cc33, the candidate list) — it is neither a tooling nor an identity stage and keeps an
explicit home in the orchestration body.

**5. `config.py` is OUT OF SCOPE.** It is at 777 (786 after task cb6f) and is genuinely the next
file to breach the cap, but it was never split out of `runner.py` — it is a sibling, and its problem
is a different shape: `LLMConfig.from_env` is 178 lines of FLAT independent env reads with no
ordering coupling and no lifecycle. Recorded here only so the runner's function-extraction pattern is
not generalized onto it; a declarative field table is the likely answer there, and it is separate work.

## Consequences

- Realistic end state is `runner.py` ≈ 380 lines (≈470 on the cc33 base) with `run()` at 60–90 lines
  — not the ~250/~15 an earlier draft projected, which ignored the 19-line telemetry block and the
  13-line `finalize_outcome` call.
- Some extractions are small in CODE terms (`cache_settings` application is 2 executable lines).
  AGENTS.md forbids creating files under 100 LOC by splitting, so extractions are grouped into a
  small number of themed modules rather than one module per concern.
- **"Zero test edits" does not hold.** Six string-keyed `monkeypatch.setattr(runner_mod, …)` targets
  move: `cache_settings_for`, `_pai_model` (×3), `_local_proxy_bypass_base_url`, `ProviderSession`,
  `_pai_structured`. Re-importing a moved name back into `runner` fixes `from … import` callers but
  NOT `setattr` patches, which rebind `runner`'s global while the extracted function resolves through
  its own module globals. Each repoint lands in the same commit as its extraction.
  MEASURED mitigation: neutering the `cache_settings_for` patch makes
  `test_execution_mode_dispatch.py` fail LOUDLY (`ImportError: cannot import name 'ModelHTTPError'`),
  not pass silently — so these breakages are visible, not silent coverage loss.
- `tests/unit/test_llm_timeouts.py:326-334` asserts on `inspect.getsource(runner_mod)`; moving code
  narrows that negative-space guard without turning it red. It must be repointed deliberately.
- `_TOOL_CAPABILITY_CHECKED` / `_TEMPERATURE_WITHDRAWN_LOGGED` are module-level sets mutated via
  `.clear()` through an import alias — those are SAFE across a move and must not be over-corrected.
- This does not fix everything. A new PROVIDER remains inherently cross-cutting (task 2932 touched 11
  files; under this ADR it would touch ~13), and `RunRequest` — 49 lines and 9 fields, grown by ~8 of
  the same commits — is untouched by this decomposition.

## Rejected alternatives

- **Per-provider runner subclasses.** `capabilities.py` exists precisely because task f184 moved AWAY
  from provider-name branching to profile reads; subclassing reintroduces that drift.
- **Middleware/decorator chain.** These concerns compute SETTINGS; they do not intercept the call.
  Wrapping makes assembly implicit and order-sensitive.
- **Keep extracting callees.** The status quo. Failed three times.
- **Raise the 800-line file cap.** Locked deliberately; and a 408-line method is unreadable whatever
  the file total says. The cap surfaced a real problem; it is not the problem.
- **Extract only the `except` spine, then stop (~75 lines).** Cheapest option that clears the
  immediate cap pressure. Rejected as the FULL answer because it leaves the 254-line pre-call region
  intact and that is where ~70% of recent growth landed — but it is the correct de-scope if capacity
  is short, and it is sequenced first below for exactly that reason.
