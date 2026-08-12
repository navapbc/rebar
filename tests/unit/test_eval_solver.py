"""Eval solver (epic 6f2d / WS-EVAL): run_case stands up a per-case temp rebar store +
fixture repo and runs the real agentic op with an injected FakeRunner — offline, no
model. Proves the solver wires ticket/epic context + fixture files for all 3 reviewers
and that the output is scorable by the registry."""

from __future__ import annotations

import pathlib

import pytest

import rebar
from rebar.llm import pai_tools
from rebar.llm.config import LLMConfig
from rebar.llm.evals import eval_scorers as sc
from rebar.llm.evals import eval_solver
from rebar.llm.runner import FakeRunner, PydanticAIRunner


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


def test_spec_alignment_fixture_uses_one_acceptance_criteria_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_descriptions: list[str] = []
    create_ticket = rebar.create_ticket

    def capture_create_ticket(*args: object, **kwargs: object) -> object:
        if args and args[0] == "epic":
            captured_descriptions.append(str(kwargs["description"]))
        return create_ticket(*args, **kwargs)

    monkeypatch.setattr(rebar, "create_ticket", capture_create_ticket)

    eval_solver.run_case(
        "spec-alignment",
        {
            "id": "sa-single-heading",
            "expect": "pass",
            "spec": "MUST ingest events.",
            "epics": ["Epic A: event ingestion"],
        },
        runner=FakeRunner(findings=[]),
    )

    assert len(captured_descriptions) == 1
    assert captured_descriptions[0].count("## Acceptance Criteria") == 1
    assert "## Success Criteria" not in captured_descriptions[0]


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


# ── Per-case rooting of an injected live-style runner (bug undyed-unheedful-conure) ──
# `_live_solver` builds ONE PydanticAIRunner before any case fixture exists, so its config
# points at the eval CHECKOUT. These pin that each disposable-store case re-roots that
# runner at its own fixture, so the agent's file/ticket tools read fixture code.


def _checkout_rooted_runner(checkout: str) -> PydanticAIRunner:
    """A live-style runner rooted at a stand-in checkout — no network (never run)."""
    return PydanticAIRunner(
        LLMConfig(runner="pydantic_ai", repo_path=checkout, tickets_path=checkout)
    )


@pytest.mark.parametrize("prompt_id", ["completion-verifier", "ticket-quality", "spec-alignment"])
def test_agentic_case_reroots_injected_runner_at_its_fixture(
    prompt_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    checkout = str(tmp_path / "checkout")
    pathlib.Path(checkout).mkdir()
    pathlib.Path(checkout, "src").mkdir()
    pathlib.Path(checkout, "src", "cli.py").write_text("# the real checkout\n")
    seen: dict = {}

    def spy(*args, **kwargs) -> dict:
        used: PydanticAIRunner = kwargs["runner"]
        seen["repo_path"] = used._config.repo_path
        seen["repo_root"] = kwargs["repo_root"]
        # The agent's OWN file tools, built exactly as the runner builds them.
        tools = pai_tools.filesystem_tools(used._config.repo_path)
        read_file = next(t for t in tools if t.__name__ == "read_file")
        seen["read"] = read_file("src/cli.py")
        seen["tickets_path"] = used._config.tickets_path
        return {"verdict": "PASS", "findings": [], "summary": ""}

    monkeypatch.setattr("rebar.llm.completion.verify_completion", spy)
    monkeypatch.setattr("rebar.llm.operations._review_ticket_impl", spy)
    monkeypatch.setattr("rebar.llm.spec_scan.scan_epics_for_spec", spy)

    case = {
        "id": "root1",
        "expect": "pass",
        "ticket_context": "Title: T\n## Acceptance Criteria\n- [ ] x",
        "spec": "s",
        "epics": ["e"],
        "files": {"src/cli.py": "# the disposable fixture\n"},
    }
    eval_solver.run_case(prompt_id, case, runner=_checkout_rooted_runner(checkout))

    assert seen["repo_path"] == seen["repo_root"], "runner must be rooted at the case fixture"
    assert seen["repo_path"] != checkout
    assert "# the disposable fixture" in seen["read"]
    assert "# the real checkout" not in seen["read"]
    # Ticket tools fall back to the fixture's own seeded store, not a checkout snapshot.
    assert seen["tickets_path"] is None


def test_reroot_preserves_offline_model_override_and_is_noop_for_fake() -> None:
    sentinel = object()
    runner = PydanticAIRunner(
        LLMConfig(runner="pydantic_ai", repo_path="/elsewhere"), model_override=sentinel
    )
    rerooted = eval_solver._rerooted(runner, "/fixture")
    assert rerooted._config.repo_path == "/fixture"
    assert rerooted._model_override is sentinel
    fake = FakeRunner()
    assert eval_solver._rerooted(fake, "/fixture") is fake
