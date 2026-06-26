"""Unit tests for the plan-review :class:`ProductionBatchRunner` (epic B, story B1).

These are OFFLINE: a fake ``rebar.llm.Runner`` drives the finder (no model / network),
and ``rebar.show_ticket`` / ``rebar.list_tickets`` are monkeypatched so
``assemble_context`` reconstructs a synthetic ticket with no git store. The key AC is
BEHAVIOURAL EQUIVALENCE — the runner's findings equal those of the bespoke
``run_pass1`` path on the same context + criteria + fake runner (no duplicated
algorithm).
"""

from __future__ import annotations

import dataclasses

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import registry
from rebar.llm.plan_review.orchestrator import assemble_context, route_criteria
from rebar.llm.plan_review.pass1 import run_pass1
from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow.runners import BatchRunRequest, BatchRunResult

_GOOD_AC = (
    "## Why\nthe system needs X.\n\n## What\nbuild X in `src/rebar/x.py`.\n\n"
    "## Scope\njust X.\n\n## Acceptance Criteria\n"
    "- [ ] X is observably true\n- [ ] another check\n"
)

_TARGET = "abcd-0000-0000-0001"


def _state(*, ttype: str = "story", description: str = _GOOD_AC) -> dict:
    return {
        "ticket_id": _TARGET,
        "ticket_type": ttype,
        "title": "Build X",
        "description": description,
        "deps": [],
    }


@pytest.fixture
def _stub_reads(monkeypatch):
    """Monkeypatch the rebar reads ``assemble_context`` uses so it reconstructs a
    synthetic, store-free ticket with no children."""
    import rebar

    state = _state()

    def _show(ticket_id, *, repo_root=None):  # noqa: ANN001
        return dict(state)

    def _list(*, parent=None, repo_root=None):  # noqa: ANN001
        return []

    monkeypatch.setattr(rebar, "show_ticket", _show)
    monkeypatch.setattr(rebar, "list_tickets", _list)
    return state


def _cfg() -> LLMConfig:
    # Match what ProductionBatchRunner builds: from_env (offline-safe) with the entry
    # model pinned to the ladder head used in the requests below.
    return dataclasses.replace(LLMConfig.from_env(repo_root=None), model="claude-opus-4-8")


def _make_req(criteria, *, target=_TARGET, usd_budget=None) -> BatchRunRequest:
    return BatchRunRequest(
        finder="plan-review-finder",
        criteria=tuple(criteria),
        usd_budget=usd_budget,
        model_ladder=("claude-opus-4-8",),
        workflow={},
        target_ticket=target,
        repo_root=None,
        run_id="run-1",
        step_id="finders",
    )


# ── equivalence: the runner ≡ the bespoke run_pass1 path (the key AC) ───────────
def test_runner_findings_equal_bespoke_run_pass1(_stub_reads):
    ctx = assemble_context(_TARGET, repo_root=None)
    single, agent = route_criteria(ctx)
    cfg = _cfg()

    # A deterministic fake that returns one finding per routed criterion. pass1_chunk
    # filters findings to the chunk's criteria, so each routed criterion surfaces its
    # own finding in BOTH paths.
    routed_ids = [c["id"] for c in single + agent]
    fake = FakeRunner(
        structured={
            "analysis": "",
            "findings": [{"finding": f"f-{cid}", "criteria": [cid]} for cid in routed_ids],
        }
    )

    cov_bespoke: dict = {}
    findings_bespoke = run_pass1(ctx, cfg, fake, single, agent, cov_bespoke)

    # Same criteria as {prompt: id} dicts → the runner re-splits them by exec_tier.
    req = _make_req({"prompt": cid} for cid in routed_ids)
    result = ProductionBatchRunner(runner=fake).run(req, None)

    assert isinstance(result, BatchRunResult)
    assert result.outputs["findings"] == findings_bespoke
    # The test must actually exercise non-empty findings (not a trivial [] == []).
    assert findings_bespoke, "expected the routed criteria to produce findings"


# ── tier split matches exec_tier; unknown ids skipped ──────────────────────────
def test_tier_split_matches_exec_tier_and_skips_unknown(_stub_reads):
    # E2 is a single-turn (1-TURN) criterion; E4 is AGENT-tier.
    assert registry.exec_tier(registry.by_id()["E2"]) != "AGENT"
    assert registry.exec_tier(registry.by_id()["E4"]) == "AGENT"

    fake = FakeRunner(structured={"analysis": "", "findings": []})
    req = _make_req([{"prompt": "E2"}, {"prompt": "E4"}, {"prompt": "NOPE-not-a-criterion"}])
    result = ProductionBatchRunner(runner=fake).run(req, None)

    res = result.outputs["batch_plan"]["batch_resolution"]
    assert res["single"] == ["E2"]
    assert res["agent"] == ["E4"]
    assert res["skipped"] == ["NOPE-not-a-criterion"]


# ── seam conformance: BatchRunResult + opaque batch_plan + unused agent_runner ──
def test_seam_conformance_and_agent_runner_unused(_stub_reads):
    fake = FakeRunner(structured={"analysis": "", "findings": []})
    req = _make_req([{"prompt": "E2"}])

    # agent_runner is the seam param; the production runner does not use it (D3). Both
    # an explicit None and omitting it entirely must work.
    result_none = ProductionBatchRunner(runner=fake).run(req, None)
    result_omit = ProductionBatchRunner(runner=fake).run(req)

    for result in (result_none, result_omit):
        assert isinstance(result, BatchRunResult)
        # batch_plan IS the coverage dict run_pass1 populates (opaque to the engine).
        plan = result.outputs["batch_plan"]
        assert isinstance(plan, dict)
        assert "chunks" in plan and "budget" in plan  # run_pass1 populated it
        assert result.outputs["criteria_count"] == 1


# ── target_ticket guard ────────────────────────────────────────────────────────
def test_target_ticket_required():
    req = _make_req([{"prompt": "E2"}], target=None)
    with pytest.raises(ValueError, match="target_ticket"):
        ProductionBatchRunner(runner=FakeRunner()).run(req, None)


# ── budget: default computed cap; override journaled but not applied (follow-up) ─
def test_budget_default_computed_cap(_stub_reads):
    from rebar.llm.plan_review import sizing

    ctx = assemble_context(_TARGET, repo_root=None)
    expected_cap = sizing.plan_budget_cap(ctx)

    fake = FakeRunner(structured={"analysis": "", "findings": []})
    req = _make_req([{"prompt": "E2"}, {"prompt": "E4"}])  # usd_budget=None
    result = ProductionBatchRunner(runner=fake).run(req, None)

    plan = result.outputs["batch_plan"]
    assert plan["budget"]["cap_usd"] == expected_cap
    assert "requested_usd_budget" not in plan  # no override requested


def test_budget_override_is_journaled_but_not_yet_applied(_stub_reads):
    # Documents the D4 follow-up: req.usd_budget is recorded, but the computed cap is
    # still used (no clean override seam in pass1/sizing yet).
    from rebar.llm.plan_review import sizing

    ctx = assemble_context(_TARGET, repo_root=None)
    expected_cap = sizing.plan_budget_cap(ctx)

    fake = FakeRunner(structured={"analysis": "", "findings": []})
    req = _make_req([{"prompt": "E2"}, {"prompt": "E4"}], usd_budget=0.01)
    result = ProductionBatchRunner(runner=fake).run(req, None)

    plan = result.outputs["batch_plan"]
    assert plan["requested_usd_budget"] == 0.01
    assert plan["budget_override_applied"] is False
    assert plan["budget"]["cap_usd"] == expected_cap  # computed cap, not the override
