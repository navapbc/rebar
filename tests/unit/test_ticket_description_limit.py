"""Blocking admission control for historically harmful ticket-description sizes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rebar.config import Config, ConfigError
from rebar.llm.plan_review.det_floor import PlanContext, p4_oversize, run_det_floor
from rebar.llm.workflow import steps as _steps  # noqa: F401 — register gate operations
from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

pytestmark = pytest.mark.unit

_TARGET = "T-oversize"
_PLAN_START = (
    "## Why\nBound expensive review inputs.\n\n"
    "## What\nReject descriptions above the configured maximum.\n\n"
    "## Scope\nPlan and completion admission only.\n\n"
    "## Acceptance Criteria\n"
    "- [ ] The exact boundary is enforced and covered by tests.\n\n"
    "## Testing\nRun the focused unit test.\n\n"
)


def _description(length: int) -> str:
    assert length >= len(_PLAN_START)
    return _PLAN_START + ("x" * (length - len(_PLAN_START)))


def _state(ticket_type: str, length: int, *, file_impact=None) -> dict:
    state = {
        "ticket_id": _TARGET,
        "ticket_type": ticket_type,
        "title": "Description admission",
        "description": _description(length),
        "deps": [],
    }
    if file_impact is not None:
        state["file_impact"] = file_impact
    return state


def _plan_context(length: int, *, file_impact=None, description: str | None = None) -> PlanContext:
    state = _state("task", length, file_impact=file_impact)
    if description is not None:
        state["description"] = description
    return PlanContext(
        ticket_id=_TARGET,
        ticket_type="task",
        title=state["title"],
        description=state["description"],
        state=state,
        repo_root=None,
    )


def _step_context() -> StepContext:
    return StepContext(
        run_id="r",
        step_id="precheck",
        kind="uses",
        step={},
        inputs={"ticket_id": _TARGET, "graph": False},
        workflow={},
        target_ticket=_TARGET,
        repo_root=None,
    )


def _patch_plan_reads(monkeypatch, state: dict) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda *a, **k: [])
    monkeypatch.setattr(
        "rebar.llm.config.resolve_gate_config",
        lambda *a, **k: SimpleNamespace(runner="fake", model="fake", repo_path="."),
    )


def test_typed_config_defaults_to_8000_and_accepts_positive_override() -> None:
    assert Config.from_mapping(None).verify.max_ticket_description_chars == 8_000
    configured = Config.from_mapping({"verify": {"max_ticket_description_chars": "12000"}})
    assert configured.verify.max_ticket_description_chars == 12_000


@pytest.mark.parametrize("value", [0, -1, True, "many"])
def test_typed_config_rejects_non_positive_integer_limit(value) -> None:
    with pytest.raises(ConfigError):
        Config.from_mapping({"verify": {"max_ticket_description_chars": value}})


def test_p4_allows_8000_and_blocks_8001() -> None:
    admitted = p4_oversize(_plan_context(8_000))
    rejected = p4_oversize(_plan_context(8_001))

    assert admitted.status == "pass"
    assert admitted.blocked is False
    assert rejected.blocked is True
    assert rejected.coverage["desc_chars"] == 8_001
    assert rejected.coverage["desc_limit_chars"] == 8_000


def test_p4_honors_configured_positive_override(monkeypatch) -> None:
    monkeypatch.setattr(
        "rebar.config.load_config",
        lambda *a, **k: Config.from_mapping({"verify": {"max_ticket_description_chars": 9_000}}),
    )

    result = p4_oversize(_plan_context(8_500))

    assert result.status == "pass"
    assert result.coverage["desc_limit_chars"] == 9_000


def test_p4_does_not_hide_invalid_typed_configuration(monkeypatch) -> None:
    def _invalid_config(*_args, **_kwargs):
        raise ConfigError("verify.max_ticket_description_chars must be >= 1")

    monkeypatch.setattr("rebar.config.load_config", _invalid_config)

    with pytest.raises(ConfigError, match="max_ticket_description_chars"):
        p4_oversize(_plan_context(8_001))


def test_det_floor_does_not_downgrade_invalid_configuration_to_abstain(monkeypatch) -> None:
    def _invalid_config(*_args, **_kwargs):
        raise ConfigError("verify.max_ticket_description_chars must be >= 1")

    monkeypatch.setattr("rebar.config.load_config", _invalid_config)

    with pytest.raises(ConfigError, match="max_ticket_description_chars"):
        run_det_floor(_plan_context(8_001))


def test_p4_other_size_signals_remain_one_advisory_finding() -> None:
    acs = "## Acceptance Criteria\n" + "".join(f"- [ ] item {index}\n" for index in range(26))
    ctx = _plan_context(
        len(acs),
        description=acs,
        file_impact=[{"path": f"src/f{index}.py"} for index in range(31)],
    )

    result = p4_oversize(ctx)

    assert result.status == "fail"
    assert result.blocked is False
    assert result.finding is not None
    assert len(result.finding["evidence"]) == 2


def test_p4_combines_all_signals_into_one_blocking_finding() -> None:
    start = "## Acceptance Criteria\n" + "".join(f"- [ ] item {index}\n" for index in range(26))
    description = start + ("x" * (8_001 - len(start)))
    ctx = _plan_context(
        8_001,
        description=description,
        file_impact=[{"path": f"src/f{index}.py"} for index in range(31)],
    )

    result = p4_oversize(ctx)

    assert result.blocked is True
    assert result.finding is not None
    assert len(result.finding["evidence"]) == 3


@pytest.mark.parametrize("ticket_type", ["bug", "story", "task", "epic"])
def test_plan_precheck_blocks_every_oversize_work_ticket_without_llm(
    monkeypatch, ticket_type
) -> None:
    # No file impact deliberately keeps bugs in the light tier; the size block must survive its
    # usual DET-block-to-advisory downgrade.
    state = _state(ticket_type, 8_001)
    _patch_plan_reads(monkeypatch, state)

    result = STEP_REGISTRY["plan_review_precheck"](_step_context())

    assert result["run_llm"] is False
    assert result["verdict"]["verdict"] == "BLOCK"
    assert result["verdict"]["coverage"]["llm_ran"] is False
    assert any(finding["criteria"] == ["P4"] for finding in result["det_blocking"])


def test_bug_tier_still_downgrades_non_p4_blocks_to_advisory(monkeypatch) -> None:
    state = {
        "ticket_id": _TARGET,
        "ticket_type": "bug",
        "title": "Missing acceptance criteria",
        "description": "## Reproduction Steps\n1. Trigger the bug.\n2. Observe the failure.\n",
        "deps": [],
    }
    _patch_plan_reads(monkeypatch, state)

    result = STEP_REGISTRY["plan_review_precheck"](_step_context())

    assert result["run_llm"] is True
    assert result["det_blocking"] == []
    assert any(finding["criteria"] == ["P1"] for finding in result["det_advisory"])


def test_completion_precheck_accepts_description_at_exact_limit(monkeypatch) -> None:
    state = _state("task", 5_000)
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: dict(state))
    monkeypatch.setattr(
        "rebar.config.load_config",
        lambda *a, **k: Config.from_mapping({"verify": {"max_ticket_description_chars": 5_000}}),
    )
    monkeypatch.setattr("rebar.llm.completion.child_closure_findings", lambda *a, **k: ([], []))
    monkeypatch.setattr("rebar.llm.operations.assemble_context", lambda *a, **k: ("context", []))
    monkeypatch.setattr("rebar.llm.completion.build_child_closure_evidence", lambda *a, **k: "")
    monkeypatch.setenv("REBAR_VERIFY_PREFETCH", "0")

    result = STEP_REGISTRY["completion_precheck"](_step_context())

    assert result["run_verify"] is True
    assert result["precheck_failed"] is False
    assert result["verdict"] is None


def test_completion_precheck_rejects_before_child_or_context_work(monkeypatch) -> None:
    state = _state("task", 5_001)
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: dict(state))
    monkeypatch.setattr(
        "rebar.config.load_config",
        lambda *a, **k: Config.from_mapping({"verify": {"max_ticket_description_chars": 5_000}}),
    )

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("oversize completion must short-circuit before downstream work")

    monkeypatch.setattr("rebar.llm.completion.child_closure_findings", _unexpected)
    monkeypatch.setattr("rebar.llm.operations.assemble_context", _unexpected)
    monkeypatch.setattr(
        "rebar.llm.config.resolve_gate_config",
        lambda *a, **k: SimpleNamespace(runner="fake", model="fake", repo_path="."),
    )

    result = STEP_REGISTRY["completion_precheck"](_step_context())

    assert result["run_verify"] is False
    assert result["precheck_failed"] is True
    assert result["context"] == ""
    assert result["verdict"]["verdict"] == "FAIL"
    assert result["verdict"]["runner"] == "deterministic"
    assert "5,001" in result["verdict"]["summary"]
    assert "5,000" in result["verdict"]["summary"]
