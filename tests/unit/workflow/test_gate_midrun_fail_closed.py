"""The mid-run fail-closed arms of the gate dispatchers (ticket shaggy-crass-bull).

Bug 43d4 (wolflike-chevroned-haddock) added ``LLMInputRejectedError`` to the mid-run
``except (LLMUnavailableError, LLMInputRejectedError)`` arms in ``gate_dispatch`` — the
arms that turn an LLM-tier failure escaping ``run_workflow`` into a degraded INDETERMINATE
verdict plus a ``gate_error_v1`` sidecar, instead of an unhandled crash. Nothing reached
them: reverting either arm to its pre-43d4 form left the whole suite green.

These tests pin each arm at the ``run_workflow`` seam (the established
``test_gate_engine_cutover.py`` monkeypatch pattern), and — because a monkeypatched seam is
only as honest as the seam itself — pin separately that a batch runner's raise really does
propagate RAW out of a real ``run_workflow`` (``interpreter._run_batch`` wraps
``runner.run(req, rc.runner)`` in no try), which is what makes the mid-run arms reachable:
plan-review's ``ProductionBatchRunner`` re-raises a systemic ``LLMUnavailableError`` from
the Pass-1 chunk pool, and the container stage's warm-call abort raises both types through
it (see ``tests/unit/test_container_warm_abort.py``).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMInputRejectedError, LLMUnavailableError
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import gate_dispatch, plan_review_recovery

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _local_gate_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin this module to the in-place (``local``) gate read — same reasoning as
    ``test_code_review_ws4.py``: these are repo-less unit tests of dispatch logic, and the
    suite-wide ``attested``/``ref=HEAD`` default would target the ambient checkout, which on
    CI's shallow blobless clone degenerates into per-object lazy fetching that hangs the
    lane."""
    monkeypatch.setenv("REBAR_GATE_SOURCE", "local")
    monkeypatch.delenv("REBAR_GATE_REF", raising=False)


def _ctx() -> PlanContext:
    return PlanContext(
        ticket_id="T-1",
        ticket_type="story",
        title="Build X",
        description=(
            "## Why\nneed X\n\n## What\nbuild X in src/x.py\n\n## Scope\njust X\n\n"
            "## Acceptance Criteria\n- [ ] X persists\n- [ ] seam calls X (`pytest -q`)\n"
        ),
    )


def _cfg() -> LLMConfig:
    return dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5")


class _SidecarRecorder:
    """Records ``emit_gate_error`` calls kwargs-tolerantly: the PREFLIGHT arm passes only
    ``cause=``/``repo_root=`` while the MID-RUN arms also thread ``diagnostic=``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, ticket_id: str, gate: str, **kw: Any) -> None:
        self.calls.append({"ticket_id": ticket_id, "gate": gate, **kw})


def _raise_from_run_workflow(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def _boom(*a: Any, **kw: Any):
        raise exc

    monkeypatch.setattr(_ex, "run_workflow", _boom)


# ── plan-review mid-run arm ─────────────────────────────────────────────────────────
def _minimal_plan_review_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    # The dispatch-time step-id contract check runs BEFORE the billable run; a skeleton
    # carrying the required ids is enough (the run itself is monkeypatched to raise).
    monkeypatch.setattr(
        gate_dispatch,
        "_gate_doc",
        lambda name, repo_root: {
            "id": "g",
            "steps": [{"id": sid} for sid in plan_review_recovery._PLAN_REVIEW_REQUIRED_STEP_IDS],
        },
    )


@pytest.mark.parametrize(
    "exc",
    [LLMUnavailableError("provider down mid-run"), LLMInputRejectedError("prompt too large")],
    ids=["unavailable", "input-rejected"],
)
def test_plan_review_midrun_failure_degrades_with_sidecar(monkeypatch, exc) -> None:
    """A systemic LLM-tier error escaping ``run_workflow`` mid-run must degrade to the
    INDETERMINATE verdict AND emit the gate_error_v1 sidecar — never crash, never PASS.
    Reverting the arm to its pre-43d4 form (``except LLMUnavailableError`` only) makes the
    input-rejected case ESCAPE, turning that parametrization RED."""
    recorder = _SidecarRecorder()
    monkeypatch.setattr(gate_dispatch, "emit_gate_error", recorder)
    _minimal_plan_review_doc(monkeypatch)
    _raise_from_run_workflow(monkeypatch, exc)

    verdict = gate_dispatch.produce_plan_review_verdict(
        _ctx(), _cfg(), runner=FakeRunner(), advisory_cap=10, repo_root=None
    )

    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["verdict"] != "PASS"  # the fuel-posse-ball guard: never a hollow PASS
    assert verdict["coverage"].get("llm_ran") is False
    # The write-then-degrade contract (ticket 8bc5): the sidecar is emitted BEFORE the
    # degraded verdict is returned, on the MID-RUN arm (diagnostic= threaded; None is
    # legitimate when the failure struck before anything was consumed).
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["ticket_id"] == "T-1"
    assert call["gate"] == "plan_review"
    assert call["cause"] == str(exc)
    assert "diagnostic" in call  # the mid-run arm, not the preflight one


# ── code-review mid-run arm ─────────────────────────────────────────────────────────
_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('hi')\n"


@pytest.mark.parametrize(
    "exc",
    [LLMUnavailableError("provider down mid-run"), LLMInputRejectedError("prompt too large")],
    ids=["unavailable", "input-rejected"],
)
def test_code_review_midrun_failure_degrades_with_sidecar(monkeypatch, exc) -> None:
    """Same contract on the code-review dispatcher: a mid-run systemic error degrades to
    INDETERMINATE and (with a target ticket) emits the sidecar. NOTE the arm is a DEFENSIVE
    seam contract today: ``CodeReviewBatchRunner`` translates a runner's
    ``LLMUnavailableError`` into the kernel's ``SystemicDiscoveryError``, which
    ``discovery.execute_stage`` absorbs (``systemic_abort``, no re-raise), and agent-step
    raises are absorbed by ``interpreter._run_leaf`` — so no in-tree production path raises
    these types out of the code-review workflow. Pinning the arm keeps the contract from
    silently rotting if a future producer (like plan-review's Pass-1 pool re-raise) appears."""
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    recorder = _SidecarRecorder()
    monkeypatch.setattr(gate_dispatch, "emit_gate_error", recorder)
    _raise_from_run_workflow(monkeypatch, exc)

    verdict = gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            _cfg(),
            diff_text=_DIFF,
            changed_files=["x.py"],
            runner=FakeRunner(),
            target_ticket="T-9",
        )
    )

    assert verdict["verdict"] == "INDETERMINATE"
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["ticket_id"] == "T-9"
    assert call["gate"] == "code_review"
    assert call["cause"] == str(exc)
    assert "diagnostic" in call


# ── seam fidelity: a batch runner's raise propagates RAW out of run_workflow ───────
class _RaisingBatchRunner(_ex.BatchRunner):
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def run(self, req, agent_runner):
        raise self._exc


def _batch_wf() -> dict:
    # The minimal valid batch-step doc (the test_batch_step.py shape).
    return {
        "schema_version": "3",
        "name": "batch-raise-wf",
        "steps": [
            {
                "id": "finders",
                "batch": {
                    "prompt": "plan-review-finder",
                    "criteria": [{"prompt": "plan-review-E1"}],
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "exc_type",
    [LLMUnavailableError, LLMInputRejectedError],
    ids=["unavailable", "input-rejected"],
)
def test_batch_runner_raise_propagates_raw_out_of_run_workflow(exc_type) -> None:
    """``interpreter._run_batch`` wraps ``runner.run(req, rc.runner)`` in NO try, so an
    LLM-tier error a batch runner raises escapes ``run_workflow`` RAW — the seam that makes
    the mid-run arms above reachable (plan-review's production batch runner re-raises a
    systemic outage from the Pass-1 pool, and the container warm abort raises both types
    through it). If the interpreter ever started absorbing these, the mid-run arms would go
    dead and the degraded-verdict contract with them — this test makes that move loud."""
    with pytest.raises(exc_type):
        _ex.run_workflow(
            _batch_wf(),
            {},
            scripted_registry=dict(_ex.STEP_REGISTRY),
            agent_runner=_ex.FakeAgentRunner(),
            batch_runner=_RaisingBatchRunner(exc_type("boom")),
        )
