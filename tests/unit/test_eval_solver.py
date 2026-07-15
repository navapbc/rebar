"""Eval solver (epic 6f2d / WS-EVAL): run_case stands up a per-case temp rebar store +
fixture repo and runs the real agentic op with an injected FakeRunner — offline, no
model. Proves the solver wires ticket/epic context + fixture files for all 3 reviewers
and that the output is scorable by the registry."""

from __future__ import annotations

import pathlib

import pytest

from rebar.llm.evals import eval_scorers as sc
from rebar.llm.evals import eval_solver
from rebar.llm.runner import FakeRunner


def test_completion_verifier_case_runs_and_scores() -> None:
    case = {
        "id": "cv1",
        "expect": "fail",
        "ticket_context": "## Acceptance Criteria\n- [ ] add `rebar foo`\n- [ ] support --json",
        "files": {"src/foo.py": "def foo():\n    return 1\n"},
    }
    fake = FakeRunner(
        structured={
            "verdict": "FAIL",
            "findings": [
                {
                    "severity": "high",
                    "dimension": "completion",
                    "detail": "--json AC unmet",
                    "citations": [{"kind": "file", "path": "src/foo.py", "line_start": 1}],
                }
            ],
        }
    )
    out = eval_solver.run_case("completion-verifier", case, runner=fake)
    assert out["verdict"] == "FAIL"
    # registry scorers run on the real output
    assert sc.score("emits_valid_verdict", case, out).passed is True
    assert sc.score("recall_on_incomplete", case, out).passed is True


def test_ticket_quality_case_runs_and_scores() -> None:
    case = {"id": "tq1", "expect": "finding", "ticket_context": "Title: X\nVague work, no AC."}
    fake = FakeRunner(
        findings=[{"severity": "medium", "dimension": "ticket-quality", "detail": "no AC block"}]
    )
    out = eval_solver.run_case("ticket-quality", case, runner=fake)
    assert sc.score("emits_valid_review_result", case, out).passed is True
    assert sc.score("recall_on_seeded_defects", case, out).passed is True


def test_ticket_quality_good_case_no_fire() -> None:
    ctx = "Title: Y\n## Acceptance Criteria\n- [ ] x"
    case = {"id": "tq2", "expect": "pass", "ticket_context": ctx}
    out = eval_solver.run_case("ticket-quality", case, runner=FakeRunner(findings=[]))
    assert sc.score("no_fire_on_good_cases", case, out).passed is True


def test_spec_alignment_batch_case_runs() -> None:
    case = {
        "id": "sa1",
        "expect": "finding",
        "spec": "MUST ingest events AND emit an audit log.",
        "epics": ["Epic A: event ingestion", "Epic B: Jira reconciler"],
    }
    fake = FakeRunner(
        findings=[{"severity": "high", "dimension": "spec-alignment", "detail": "no audit log"}]
    )
    out = eval_solver.run_case("spec-alignment", case, runner=fake)
    assert sc.score("recall_on_gaps_and_conflicts", case, out).passed is True


def _novelty_fake(answer: str) -> FakeRunner:
    """A FakeRunner canned as the novelty sub-call's structured output: one novelties item
    answering all three matches-prior sub-answers with ``answer`` (``"no"`` → novelty 1.0,
    ``"yes"`` → 0.0)."""
    return FakeRunner(
        structured={
            "novelties": [
                {
                    "index": 0,
                    "matches_prior": {
                        "restates_prior_defect": answer,
                        "cites_prior_location": answer,
                        "matches_prior_fix": answer,
                    },
                    "matched_prior_id": "" if answer == "no" else "prior-1",
                }
            ]
        }
    )


def test_plan_review_novelty_novel_case_routes_and_scores() -> None:
    # bug cuddlesome-titanous-seamonkey: plan-review-novelty must dispatch (no ValueError)
    # and return the {"novelty": float} shape the discriminates_novelty scorer reads.
    case = {
        "id": "N1-novel",
        "pair": "idempotency",
        "kind": "novel",
        "expect": "high_novelty",
        "prior_finding": "The plan states no idempotency test; a retried webhook double-charges.",
        "finding": "The migration drops the legacy table before the backfill completes.",
    }
    out = eval_solver.run_case("plan-review-novelty", case, runner=_novelty_fake("no"))
    assert out["novelty"] == 1.0
    assert sc.score("discriminates_novelty", case, out).passed is True


def test_plan_review_novelty_carryover_case_routes_and_scores() -> None:
    case = {
        "id": "N1-carryover",
        "pair": "idempotency",
        "kind": "carryover",
        "expect": "low_novelty",
        "prior_finding": "The plan states no idempotency test; a retried webhook double-charges.",
        "finding": "A duplicate webhook delivery is not guarded by an idempotency check.",
    }
    out = eval_solver.run_case("plan-review-novelty", case, runner=_novelty_fake("yes"))
    assert out["novelty"] == 0.0
    assert sc.score("discriminates_novelty", case, out).passed is True


def test_fixture_files_are_written_into_the_store() -> None:
    case = {"id": "f1", "expect": "pass", "files": {"a/b.txt": "hello"}}
    with eval_solver.case_store(case) as root:
        assert pathlib.Path(root, "a/b.txt").read_text() == "hello"


def test_unknown_prompt_raises() -> None:
    with pytest.raises(ValueError, match="no eval solver"):
        eval_solver.run_case("not-a-reviewer", {"id": "x", "expect": "pass"}, runner=FakeRunner())
