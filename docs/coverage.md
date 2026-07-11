# Coverage baseline

rebar measures line + branch coverage with `coverage.py` (via `pytest-cov`),
configured in `pyproject.toml` under `[tool.coverage.run]` (`source = ["rebar"]`,
`branch = true`). CI reports the number for visibility; the documented floor below
is conservative and not intended to gate routine work.

## Invocation

```
pytest -m "not integration and not external" \
  --cov=rebar --cov-branch --cov-report=term-missing:skip-covered -q
```

This is the same test selection CI's coverage step uses.

## Measured baseline

| Date       | Scope                                   | In-process total |
|------------|-----------------------------------------|------------------|
| 2026-06-27 | `-m "not integration and not external"` | **77%**          |
| 2026-06-14 | `-m "not integration and not external"` | 61%              |

(2026-06-27: 3540 passed, 38 skipped — the in-process total rose from 61% to 77%
as the suite grew. 2026-06-14: 1758 passed, 7 skipped.)

## Subprocess caveat

The measured number **understates** the code actually exercised. Many tests drive
rebar as a subprocess — the CLI adapter shells out to `python -m rebar.cli`, and
several CLI/exit-code tests do the same — so the work those subprocesses do is not
attributed to the parent process's in-process coverage. The real exercised fraction
is higher than 77%; treat 77% as a floor on what the in-process measurement can
see, not a ceiling on what the suite covers.

To make the subprocess work *count* (and measure the true number), the standard
coverage.py recipe is `[tool.coverage.run] patch = ["subprocess"]` (coverage ≥ 7.10,
auto-enables `parallel = true`) plus a `coverage combine` step before the report.
It is deliberately NOT enabled today — see "Future: subprocess coverage" below.

## Recorded target

A floor of **`fail_under = 70`** is recorded in `[tool.coverage.report]` — below the
measured in-process **77%** (7-pt headroom) so it guards against a large regression
without failing the report-only CI coverage step (which runs the identical
selection). Raise the floor deliberately as in-process coverage climbs; do not
ratchet it above the measured number.

## Future: subprocess coverage

Capturing child-process coverage (the CLI shells out to `python -m rebar.cli`) would
let the floor track the *true* exercised fraction rather than the in-process
undercount. The modern recipe is small — `[tool.coverage.run] patch = ["subprocess"]`
+ a `coverage combine` step — and since rebar's subprocesses run in the **same**
environment as the tests (no throwaway venvs), it needs no `[tool.coverage.paths]`
remapping. It is a deliberate, optional follow-up: measuring subprocess coverage is
standard for process-spawning tools (pip, tox, nox, virtualenv) but many mature CLIs
(pdm, hatch, pipx, pre-commit) accept the in-process undercount with a conservative
floor — which is the documented stance here until the recipe is adopted.

## Behavior × interface matrix

Coverage is line-number-blind to whether each behavior is exercised through all
three facades. This matrix pins the load-bearing behaviors and contracts against
{CLI, library, MCP}; every cell names a test (or notes where one facade does not
apply). The parity suite (`tests/interfaces/facades/test_parity.py`) drives one behavior
through all three adapters at once.

| Behavior / contract                  | CLI                                  | Library                              | MCP                                  |
|--------------------------------------|--------------------------------------|--------------------------------------|--------------------------------------|
| create / show / list+filter         | `test_parity` (parametrized)         | `test_parity`                        | `test_parity`                        |
| transition / claim happy path       | `test_parity`                        | `test_parity`                        | `test_parity`                        |
| stale transition → concurrency       | `test_exit_codes` (exit 10)          | `test_transition_exit10` (raises)    | `test_mcp` (tool error identity)     |
| claim already-claimed → concurrency  | `test_parity`, `test_store_concurrency` | `test_transition_exit10`          | `test_mcp` (tool error identity)     |
| reopen non-closed → concurrency      | `test_exit_codes`                    | `test_transition_exit10`             | `test_mcp`                           |
| deps graph contract                  | `test_parity` (behavioral)           | `test_parity`                        | `test_parity`                        |
| concurrent writer storm / locking    | `test_store_concurrency`             | (drives CLI)                         | n/a                                  |
| deterministic replay + fork tiebreak | `test_concurrency_regression`        | `tests/scripts/reducer/*`            | (shared read path)                   |
| output-schema conformance            | `test_schema_outputs`                | `test_schema_outputs`                | `test_mcp_output_schema_coverage`    |
| sign / verify-signature result shape | `test_schema_outputs`                | `test_schema_outputs`                | typed output + completeness guard    |
| Jira field round-trips (description, status, label, parent, comment) | n/a | `test_reconcile_roundtrip` (pure differs) | n/a |
| Jira live dry-run (non-destructive)  | n/a                                  | `tests/external/test_reconcile_live` | n/a                                  |

MCP completeness is enforced mechanically: `test_mcp_output_schema_coverage`
sources the tool set from `list_tools()` and asserts a **total, disjoint
partition** over the classification sets, so a new tool returning structured data
with no `outputSchema` fails the guard rather than slipping through.

Jira link/relation round-trips ARE covered: link sync is a reconciler capability
(story 25ae — the reconciler maps a local ticket's `deps` links to Jira
relationships via `client.set_relationship`), and the round-trips are exercised by
`tests/integration/rebar_reconciler/test_link_sync.py`. (This entry previously said
link sync was "not yet a reconciler capability"; that is stale — it is implemented
and tested.)
