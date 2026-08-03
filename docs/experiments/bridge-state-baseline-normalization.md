# Bridge-state baseline normalization corpus

Ticket: `5fa1-aab2-e6b5-4fad` (A3)

Date: 2026-08-03

## Result

A3's scalar baseline projection passed its repository-level forward, rollback,
size, and collateral-field gates over every baseline in the authoritative
`origin/tickets` corpus.

| Measurement | Result |
|---|---:|
| Store commit | `da937e47068898ed3fc6feae09acdfea963e1dd3` |
| Pre-A3 code commit | `550442ae58864f6f32b76ba78172251ba3119bb8` |
| Bindings / baselines compared | 1,774 / 1,774 |
| Raw `bindings.json` bytes | 23,116,300 |
| Projected `bindings.json` bytes | 8,435,959 |
| Size reduction | 63.506% |
| Current-code mutation deltas | 0 |
| Pre-A3-code rollback mutation deltas | 0 |
| Missing tickets or comparison errors | 0 |
| Assignee shape/value deltas | 0 |

The measured reduction clears the ticket's 50% floor by 13.506 percentage
points.

## Reproduction

Run from an A3 checkout whose worktree contains the normalization change:

```sh
env PATH="$PWD/.venv/bin:$PATH" python \
  scripts/measure_baseline_normalization.py \
  --store-ref da937e47068898ed3fc6feae09acdfea963e1dd3 \
  --pre-a3-ref 550442ae58864f6f32b76ba78172251ba3119bb8
```

Use `--store-ref origin/tickets` to repeat the same gate against the newest
store tip; the pinned SHA above reproduces the table exactly.

The command prints one JSON result and exits zero only when all of these are
true:

- every stored baseline has a corresponding reduced ticket and executes
  without error;
- raw and normalized baselines produce identical observable outbound mutation
  fields, conflict reports, and dropped-field reports under the current code;
- the same raw/normalized pair produces identical outcomes under code exported
  from the named pre-A3 revision;
- all assignee presence bits and values remain byte-equivalent as JSON values;
- the projected full-file serialization is at least 50% smaller.

## Test contract card

```yaml
authoritative_contract: >-
  A3 acceptance criteria: description/status/priority may be scalarized only
  when live mutation verdicts are unchanged in both the forward and pre-A3
  rollback directions, assignee remains untouched, and size falls by >=50%.
trigger_preconditions: >-
  Load every real origin/tickets binding with a baseline; use the real reduced
  local ticket and the stored vendor baseline as the observed remote state.
production_path: >-
  JiraBackend inbound/outbound mappers -> compute_update_fields ->
  diff_canonical_fields, with the binding lookup and parent lookup surfaces
  backed by the real corpus maps.
test_tier: >-
  Repository-level corpus subprocess. A unit fixture cannot establish the
  distributional size result or exercise every production-shaped baseline.
observable_postcondition: >-
  Zero mutation-result deltas under current and exported pre-A3 code; zero
  assignee deltas; complete coverage; normalized bytes <= 50% of raw bytes.
ci_gate: >-
  The reproduction command above plus the repository make test gate.
negative_control: >-
  Assignee is deliberately not projected and must retain both presence and its
  complete dict/scalar/null value for every row.
collateral_invariants: >-
  Binding count, reverse map, non-baseline metadata, summary, and assignee are
  copied unchanged; only description/status/priority baseline values project.
```

## Method

The current `prev_snapshot.json` is intentionally key-only, so it cannot supply
field-bearing Jira rows for this measurement. Instead, for each binding the
stored baseline is used as the last observed remote state. This is the exact
state whose representation A3 changes and is sufficient to isolate the claimed
invariant: changing only the ancestor's representation must not alter the live
three-way comparison verdict for the same local and remote values.

The script builds two in-memory binding stores:

1. the unmodified vendor-shaped store;
2. a deep copy where only `description`, `status`, and `priority` are projected
   with A3's production normalizer.

It obtains local states through the native complete reducer, rather than the
work-ticket listing, because three legitimate bound rows are `code_review`
artifacts. Both stores are then evaluated through `compute_update_fields` for
all 1,774 rows. The observable record includes outbound fields, conflict-sink
entries, and dropped-field-sink entries.

For the rollback oracle, the script exports the named pre-A3 revision with
`git archive` into a temporary directory and runs the same worker in a separate
Python process with that revision's `rebar_reconciler` first on `PYTHONPATH`.
This prevents module-cache leakage from turning the rollback check into another
current-code run.

The script deletes its temporary corpus and exported source tree on exit. It
does not write to `origin/tickets`, the local tracker, or Jira.

## Interpretation

The corpus proves representation equivalence for all stored baselines at the
pinned store revision and proves the old reader accepts the normalized store.
It is intentionally not a live-Jira canary: deployment and writer-version
evidence belong to the later A2-2/A6 rollout gates. Those gates remain required
before any irreversible history reclaim.
