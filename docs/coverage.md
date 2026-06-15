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
| 2026-06-14 | `-m "not integration and not external"` | **61%**          |

(1758 passed, 7 skipped at the time of measurement.)

## Subprocess caveat

The measured number **understates** the code actually exercised. Many tests drive
rebar as a subprocess — the CLI adapter shells out to `python -m rebar.cli`, and
several CLI/exit-code tests do the same — so the work those subprocesses do is not
attributed to the parent process's in-process coverage. The real exercised fraction
is higher than 61%; treat 61% as a floor on what the in-process measurement can
see, not a ceiling on what the suite covers.

## Recorded target

A conservative floor of **`fail_under = 50`** is recorded in
`[tool.coverage.report]` — comfortably below the measured in-process 61% so it
documents a minimum without failing the existing report-only CI coverage step
(which runs the identical selection). Raise the floor deliberately as in-process
coverage climbs; do not ratchet it above the measured number.

## Behavior × interface matrix

Coverage is line-number-blind to whether each behavior is exercised through all
three facades. This matrix pins the load-bearing behaviors and contracts against
{CLI, library, MCP}; every cell names a test (or notes where one facade does not
apply). The parity suite (`tests/interfaces/test_parity.py`) drives one behavior
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

Jira link/relation round-trips are intentionally absent: link sync is not yet a
reconciler capability (tracked as a separate story), so a round-trip test would
be premature.
