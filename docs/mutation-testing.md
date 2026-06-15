# Mutation testing

Mutation testing measures whether the test suite actually *constrains* behavior:
a tool rewrites small pieces of the source (a `>=` becomes `>`, a `+` becomes `-`,
a string or dict key is altered) and re-runs the tests against each mutant. A
mutant the tests catch is **killed**; one the tests pass anyway **survives** — a
survivor marks a line whose behavior no test pins. The kill rate (the *mutation
score*) is a sharper signal than line coverage: a line can be executed yet have
nothing asserted about it.

rebar uses [mutmut](https://github.com/boxed/mutmut) (3.x). The configuration is
committed in `pyproject.toml` under `[tool.mutmut]`; this document records the
scope, the invocation, and the recorded scores (2026-06-14).

## Scope

mutmut walks every `.py` under `source_paths` and mutates only the files matched
by `only_mutate`. The scope is the highest-value behavioral cores rather than the
whole tree, so a full run finishes in a few minutes:

| Module | Role |
|--------|------|
| `src/rebar/signing.py` | HMAC manifest signing + verdict logic |
| `src/rebar/_engine_support/gates.py` | per-ticket quality gates (clarity / check-ac / quality) |
| `src/rebar/_engine_support/next_batch.py` | conflict-aware next-batch selector |
| `src/rebar/_engine_support/validate.py` | repo-wide tracker-health scoring |
| `src/rebar/reducer/_processors.py` | per-event reducer processors |

The test selection (`pytest_add_cli_args_test_selection`) is scoped to the tests
that exercise these cores, so each mutant is evaluated against a small, fast suite
rather than the full ~5k-test run. The per-module mapping is:

| Module | Selected tests |
|--------|----------------|
| `signing.py` | `tests/unit/test_signing.py`, `tests/interfaces/lifecycle/test_signature.py` |
| `reducer/_processors.py` | `tests/scripts/reducer/`, `tests/interfaces/store/test_reducer_single_source.py` |
| `next_batch.py` | `tests/interfaces/queries/test_next_batch_compute.py`, `tests/interfaces/queries/test_next_batch_behavior.py` |
| `validate.py` | `tests/interfaces/queries/test_validate_compute.py` |
| `gates.py` | `tests/interfaces/lifecycle/test_gate_rubric_consistency.py`, `queries/test_ws5d_quality_fileimpact.py`, `lifecycle/test_close_gate_story_epic.py` |

## Invocation

From the repo root, with the dev venv interpreter:

```sh
/tmp/rebar-dev/bin/mutmut run        # mutate + score every mutant in scope
/tmp/rebar-dev/bin/mutmut results    # list non-killed mutants (survived / no-tests / timeout)
/tmp/rebar-dev/bin/mutmut show <id>  # the diff for one mutant
```

mutmut copies the project into a `mutants/` working directory (gitignored) and
runs the selected tests from there. The runner is configured fail-fast and
deterministic — `-x -q -p no:randomly --basetemp=/tmp/rebar-pytest-mut` — so a
mutant is declared killed at the first failing test, the run order is stable for
debugging, and the macOS temp-dir reuse race is avoided with a fixed basetemp.

To score a subset, narrow `only_mutate` (and the matching test selection) to the
module(s) of interest before `mutmut run`.

## Score interpretation: mapped vs no-tests

mutmut attributes each mutant to its covering tests using a coverage map built
**in-process**. Several of the cores are additionally exercised by interface
tests that drive the CLI in a **subprocess**; coverage collected in the parent
process cannot attribute those lines, so mutmut files them as **no tests** even
though the behavior is pinned by the subprocess assertions. The honest score for
the in-process test contract is therefore *killed / (killed + survived +
timeout)* — the **mapped** mutants — with no-tests reported separately rather than
folded into the denominator.

## Scores (2026-06-14)

Full-scope run: 3796 mutants total, 22.9 mutants/second.

| Module | Killed | Survived | No-tests | Timeout | Mapped score |
|--------|-------:|---------:|---------:|--------:|-------------:|
| `signing.py` | 378 | 3 | 110 | 0 | 99.2% |
| `reducer/_processors.py` | 774 | 5 | 0 | 0 | 99.4% |
| `next_batch.py` | 713 | 6 | 208 | 0 | 99.2% |
| `gates.py` | 557 | 0 | 376 | 0 | 100.0% |
| `validate.py` | 443 | 0 | 223 | 0 | 100.0% |

The large no-tests counts on `gates.py` and `validate.py` reflect that their
behavior is characterized through subprocess-driven interface tests
(`test_validate_compute.py`, the gate-rubric/close-gate tests), which the
coverage map cannot attribute; the mapped mutants those modules *do* expose
in-process are fully killed.

### Remaining survivors are equivalent or cosmetic

The handful of survivors left are mutants that cannot change observable behavior,
and adding a test for them would only pin an implementation detail:

- **`signing.py` (3)** — `encoding="utf-8"` → `encoding="UTF-8"`/`None` (codec
  aliases of the default), and `SigningError(...)` message-text edits reached only
  on an OS-error path that needs an unreadable-yet-existing key file. The message
  string is not part of the certify/verdict contract.
- **`next_batch.py` (6)** — all in `render_conflict_matrix`: column-width
  arithmetic (`+1` → `+2`/`-1`), `ljust` → `rjust`, and `i < j` → `i <= j` (the
  diagonal pair shares no files, so the overlap set is unchanged). These alter
  whitespace alignment in a human-readable matrix, not its meaning.
- **`reducer/_processors.py` (5)** — `process_verify_commands(state, None, data)`
  (the `event` parameter is unused, so passing `None` is provably equivalent), the
  `or ""` reason default (mutating an empty string to an empty string), and the
  `alert_uuid` fallback key in `process_bridge_alert`, which is only consulted when
  `resolves_uuid` is absent — a branch no realistic resolve event takes.

## Change-detector tests

A *change-detector* test fails when the implementation changes but not when the
behavior breaks — it asserts too little (only that a rich object is non-None or a
collection is non-empty), asserts a literal it just set (tautology), or asserts
private structure / exact log strings rather than the observable result. Mutation
survivors over an *executed* line are the signature of one: the line runs, but the
assertions don't constrain it.

The catalogue found by this pass, and the rewrite applied:

- **`tests/interfaces/queries/test_next_batch_compute.py::test_library_and_mcp_shape`** —
  *assertion-light.* The only in-process driver of the next-batch `compute`
  asserted just `batch_size == 2` plus schema validity, leaving the field mapping,
  the output key set, the per-candidate values, and the skip-bucket classification
  unpinned (160 `compute` + 38 `to_json_dict` survivors traced to this gap). Now
  asserts the resolved epic id/title, the exact selected ids, the full batch-item
  dict (`id`/`title`/`priority`/`type`/`files`/`files_likely_read`) with values,
  and the blocked-story skip entry — while keeping the MCP schema validation.

The byte-level CLI goldens those modules also carry (`test_next_batch_compute.py`,
`test_validate_compute.py`) are *not* change-detectors — they pin observable
stdout/stderr/exit contracts — but they run in a subprocess, so they cannot kill
in-process mutants. The remedy is added in-process behavioral coverage, not the
removal of the goldens.

## Tests added to kill high-value survivors

- **`tests/interfaces/queries/test_next_batch_behavior.py`** — drives `compute` /
  `to_json_dict` / `render_text` / `render_conflict_matrix` in-process against
  hand-built trackers and asserts observable output: the exact JSON key set and
  values, per-candidate field mapping, the blocked-story / in-progress /
  needs-planning / design-awaiting skip buckets (object and JSON projection), the
  file-overlap decision and conflict record, the `limit` default (unlimited) and
  an explicit cap, the conflict matrix, and the tombstone-status override that
  unblocks a task whose deleted dependency does not count as a blocker. (`next_batch.py`
  mapped score 62.7% → 99.2%; survivors 236 → 6.)
- **`tests/scripts/reducer/test_reducer_record_fields.py`** — reduces REVERT,
  BRIDGE_ALERT, and VERIFY_COMMANDS events through `reduce_ticket` and asserts the
  exact compiled record (every field name and value): the revert entry and its
  empty-reason default, the unarchive-on-revert-of-ARCHIVED behavior, the
  bridge-alert reason-normalization order, the resolve-matches-existing and
  resolve-with-no-match-appends branches, and the verify-commands replace /
  null-becomes-empty-list semantics. (`reducer/_processors.py` survivors 19 → 5,
  mapped score → 99.4%.)
