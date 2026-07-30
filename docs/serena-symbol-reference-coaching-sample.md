# `project.symbol-reference-completeness` — real `review-plan` runs

Evidence for story `84c7-ad6c-be99-4c30` (epic `9109-2ad9-2477-4cf6`). The criterion and its
coaching move ride the `.rebar/` overlay only; this file records what real runs actually did,
including the one thing the overlay turned out **not** to control.

All runs: `rebar review-plan <id> --source local --no-sign`. `--source local` is required
pre-land — a gate run otherwise pins its code read-root to the attested `origin/main` snapshot
(`llm/config.py::resolve_code_root`), where the new overlay does not exist yet, so the criterion
never even enters `coverage.routing`.

## The subject ticket

`4b14-8eae-404c-4b16` — a plan to rename `_anthropic_cache_settings`, whose Scope says
verbatim *"I went through the tree with `grep` and these are the files that mention it"* and then
lists two files. Four more carry the name, one of them by string literal.

## What the criterion found (fires reliably)

Every run produced a grounded finding at `validity 1.0`. From run 3:

> The affected-file inventory for `_anthropic_cache_settings` was produced by grep alone and
> omits two files: `tests/unit/test_pydantic_ai_runner.py` (structural import + 4 direct call
> sites) and `tests/interfaces/store/test_execution_mode_dispatch.py` (string-based
> `monkeypatch.setattr` site). Both are confirmed by a grep sweep. The plan's own text admits the
> grep-only method, and `docs/code-navigation.md` documents these exact two omissions as the
> canonical illustration of why grep-only inventory fails for this symbol.
>
> — `location:` `## Scope — "I went through the tree with grep and these are the files that mention it"`

It correctly identified `tests/interfaces/store/test_execution_mode_dispatch.py:90` — the
`monkeypatch.setattr` string reference that `find_referencing_symbols` cannot resolve. That is
the exact site class that broke epic `061c` story S1.

## Run log

| run | exec tier | verdict | findings carrying the criterion | moves the coach picked |
|---|---|---|---|---|
| 1 | 1-TURN, attested snapshot | PASS | criterion never ran (overlay absent from snapshot) | — |
| 2 | 1-TURN, `--source local` | BLOCK | **0** — ran and abstained | built-ins only |
| 3 | AGENT | BLOCK | 2 (validity 1.0) | `14`, `9` |
| 4 | AGENT | BLOCK | 2 (validity 1.0) | `14`, `6` |
| 5 | AGENT, move keyed on `G1G2`/`G6` too | BLOCK | 1 (validity 1.0) | `14`, `9`, `4`, `15` |

Two findings from those runs are worth keeping:

**`exec: 1-TURN` could not do this job.** At 1-TURN the criterion ran and emitted nothing even
on a deliberately thin plan: a 1-TURN reviewer cannot search the codebase, so it could not name
an omitted site, and the rubric (correctly) forbids speculation. Promoting it to `exec: AGENT`
is what made it work — the agent tier looks the symbol up and cites real line numbers. The
rubric was also corrected so the *stated-method* branch ("the plan says it grepped") is
independently sufficient without naming an omission.

**The contrast case holds.** The same criterion, run against `7468-25e5-ae4f-43d1` (epic `061c`
story S1, whose plan had already been remediated), fired **nothing** and the review returned
`PASS`. It is not a criterion that flags every plan.

## The limitation this evidence establishes

Across four agent-tier runs the coach **never selected this move**, and that is not a
misconfiguration — it is the boundary of what a project overlay controls.

- The overlay controls **applicability**. Proven deterministically in
  `tests/unit/test_project_symbol_reference_completeness.py`: the move is offered for
  `project.symbol-reference-completeness`, `G1G2` and `G6`, and is *not* offered for no
  triggers, for `project.portability`, or for `security`/`tests`. In run 5's real trigger set it
  was 1 of 14 applicable moves.
- The coach LLM controls **selection**, choosing a handful from the applicable set. It
  consistently preferred built-in moves. There is no closed enum on `move_id`, so a project move
  is structurally pickable; it simply was not picked.

There is also a structural reason it competes badly. The *same* incomplete-inventory evidence
fires rebar's built-in `G1G2`/`G6`/`E4` criteria, which are **blocking**, while this criterion is
**advisory** by design (coaching a method must not gate process). Pass-4's coachable union is
blocking findings first, then surviving advisory — so an advisory-only move is last in line.
Widening `applies_when` to include `G1G2`/`G6` was done for exactly this reason: keyed only on
its own advisory id, the move would have been **dead by construction**, the same defect the
plan-review gate flagged for the code-review routing.

Net: the detection half of this story is demonstrated live and reliably. The coaching half is
demonstrated as *offered*, deterministically, but its appearance in any given run's output is a
model judgement the overlay cannot guarantee. Story `84c7`'s acceptance criterion was corrected
to say that, rather than being reported as met.
