"""Plan review must not silently pass a container whose hierarchy failed to load (ticket
b24d-a840-ea8a-4a54 / epic 6eca-183e-1cd2-4fb9).

``_assemble_context_uncached`` swallowed child-enumeration/per-child-fetch failures with a bare
``except Exception`` and continued with an empty/partial ``children`` list. Since ``has_children``
is ``bool(self.children)``, a total enumeration failure flips ``has_children`` False, so
container-scoped DET checks (P5) trivially pass instead of reporting incompleteness. The fix:
bounded retry (2 attempts, fixed delay) on both the ``list_tickets`` enumeration and each per-child
``show_ticket`` fetch; on exhaustion, ``PlanContext.hierarchy_incomplete`` is set True with a
``hierarchy_incomplete_detail`` list (``["enumeration"]`` for total failure, the failing child's id
for a per-child failure). The flag threads through every ``finalize_verdict`` call site (the
precheck/coach workflow ops and all three ``gate_dispatch`` recovery/degrade paths) and the
drift-floor's post-drop re-derivation, forcing the verdict to INDETERMINATE rather than PASS.
"""

from __future__ import annotations

import dataclasses

import pytest

from rebar.llm.plan_review import orchestrator
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.drift_floor import _recompute_verdict_after_drop
from rebar.llm.workflow import gate_dispatch

pytestmark = pytest.mark.unit

_TARGET = "epic-0000-0000-0001"
_CHILD = "task-0000-0000-0002"


def _parent_state(tid: str) -> dict:
    return {
        "ticket_id": tid,
        "ticket_type": "epic",
        "title": "Build X",
        "description": "## Acceptance Criteria\n- [ ] X\n",
        "deps": [],
    }


# ── happy path: total enumeration failure flags hierarchy_incomplete + drives INDETERMINATE ────
def test_total_enumeration_failure_flags_hierarchy_incomplete(monkeypatch) -> None:
    """``list_tickets`` fails on every attempt → ``PlanContext.hierarchy_incomplete`` is True with
    ``hierarchy_incomplete_detail == ["enumeration"]``, and ``children`` stays empty (the prior
    silent-continue behavior for the read itself is unchanged; only the new flag is added)."""

    def _show(tid, *, repo_root=None):  # noqa: ANN001
        return _parent_state(tid)

    def _list(*, parent=None, repo_root=None):  # noqa: ANN001
        raise RuntimeError("store unavailable")

    monkeypatch.setattr("rebar._reads.show_ticket", _show)
    monkeypatch.setattr("rebar._reads.list_tickets", _list)

    pctx = orchestrator.assemble_context(_TARGET, repo_root=None)

    assert pctx.children == []
    assert pctx.has_children is False
    assert pctx.hierarchy_incomplete is True
    assert pctx.hierarchy_incomplete_detail == ["enumeration"]


def test_finalize_verdict_is_indeterminate_when_hierarchy_incomplete() -> None:
    """The new coverage flag drives ``finalize_verdict`` to INDETERMINATE — a plan review can no
    longer reach a clean PASS when the ticket hierarchy failed to load (parent epic AC #4)."""
    ctx = PlanContext(ticket_id="T-1", ticket_type="epic", title="t", description="d")
    parts = {"blocking": [], "surfaced": [], "overflow": [], "indeterminate": [], "dropped": []}
    verdict = orchestrator.finalize_verdict(
        ctx,
        parts,
        coaching=[],
        coverage={"llm_ran": True, "hierarchy_incomplete": True},
        runner_name="fake",
        model=None,
    )
    assert verdict["verdict"] == "INDETERMINATE"


# ── held-out: partial per-child failure records the SPECIFIC failing child id ──────────────────
def test_partial_child_fetch_failure_records_child_id(monkeypatch) -> None:
    """``list_tickets`` succeeds (one child), but ``show_ticket(child)`` fails on every attempt →
    ``hierarchy_incomplete_detail`` names that exact child id, and the summary row from
    ``list_tickets`` is still kept in ``children`` (the existing per-child fallback, unchanged)."""
    child_summary = {"ticket_id": _CHILD, "ticket_type": "task", "title": "child"}

    def _show(tid, *, repo_root=None):  # noqa: ANN001
        if tid == _CHILD:
            raise RuntimeError("child unreadable")
        return _parent_state(tid)

    def _list(*, parent=None, repo_root=None):  # noqa: ANN001
        return [dict(child_summary)] if parent == _TARGET else []

    monkeypatch.setattr("rebar._reads.show_ticket", _show)
    monkeypatch.setattr("rebar._reads.list_tickets", _list)

    pctx = orchestrator.assemble_context(_TARGET, repo_root=None)

    assert pctx.hierarchy_incomplete is True
    assert pctx.hierarchy_incomplete_detail == [_CHILD]
    assert pctx.children == [child_summary]


# ── held-out: a transient-then-success failure recovers — the retry has teeth ──────────────────
def test_retry_recovers_transient_enumeration_failure(monkeypatch) -> None:
    """``list_tickets`` fails once then succeeds on the SECOND attempt → the bounded retry
    recovers, ``hierarchy_incomplete`` stays False, and the real children are used. This is the
    test that gives the retry its teeth: a naive single-attempt implementation fails it."""
    calls = {"n": 0}
    child_summary = {"ticket_id": _CHILD, "ticket_type": "task", "title": "child"}

    def _show(tid, *, repo_root=None):  # noqa: ANN001
        return dict(child_summary) if tid == _CHILD else _parent_state(tid)

    def _list(*, parent=None, repo_root=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return [dict(child_summary)]

    monkeypatch.setattr("rebar._reads.show_ticket", _show)
    monkeypatch.setattr("rebar._reads.list_tickets", _list)

    pctx = orchestrator.assemble_context(_TARGET, repo_root=None)

    assert calls["n"] == 2, "must retry after the first failure, not give up immediately"
    assert pctx.hierarchy_incomplete is False
    assert pctx.hierarchy_incomplete_detail == []
    assert pctx.children == [child_summary]


# ── held-out: PlanContext's new list field must not share a mutable default across instances ───
def test_plan_context_hierarchy_detail_default_is_not_shared() -> None:
    a = PlanContext(ticket_id="A", ticket_type="task", title="a", description="d")
    b = PlanContext(ticket_id="B", ticket_type="task", title="b", description="d")
    assert a.hierarchy_incomplete_detail == []
    assert a.hierarchy_incomplete is False
    a.hierarchy_incomplete_detail.append("enumeration")
    assert b.hierarchy_incomplete_detail == [], "default list must not be shared across instances"
    # A bare `= []` class-body default raises TypeError at class-definition time for a dataclass;
    # this constructs two independent instances, proving `field(default_factory=list)` was used.
    assert dataclasses.fields(PlanContext)  # sanity: still a well-formed dataclass


# ── held-out: plan_review_coach threads the flag via ctx.inputs (no real PlanContext there) ─────
def test_coach_step_threads_hierarchy_incomplete_from_inputs() -> None:
    """``plan_review_coach`` builds only a minimal ``PlanContext`` (no real hierarchy data) and
    must read the flag out of ``ctx.inputs`` (as ``plan_review_precheck`` emitted it), not off a
    ``PlanContext`` attribute. Exercised directly against ``finalize_verdict`` the way the coach
    step assembles its coverage dict."""
    coverage = {
        "det": {},
        "routing": {},
        "llm_ran": True,
        "hierarchy_incomplete": True,
        "hierarchy_incomplete_detail": ["enumeration"],
    }
    ctx = PlanContext(ticket_id="T-1", ticket_type="task", title="", description="")
    parts = {"blocking": [], "surfaced": [], "overflow": [], "indeterminate": [], "dropped": []}
    verdict = orchestrator.finalize_verdict(
        ctx, parts, coaching=[], coverage=coverage, runner_name="fake", model=None
    )
    assert verdict["verdict"] == "INDETERMINATE"


# ── held-out: all three gate_dispatch recovery/degrade paths thread the flag ────────────────────
def _succeeded(step_id: str, outputs: dict) -> dict:
    return {"status": "succeeded", "frame_key": f"review/then/{step_id}", "outputs": outputs}


def test_recover_coach_failure_threads_hierarchy_incomplete() -> None:
    from types import SimpleNamespace

    from rebar.llm.config import LLMConfig

    rec = SimpleNamespace(
        steps=[
            _succeeded(
                "precheck",
                {
                    "det_coverage": {},
                    "canonical_id": "T-1",
                    "ticket_type": "task",
                    "hierarchy_incomplete": True,
                    "hierarchy_incomplete_detail": ["enumeration"],
                },
            ),
            _succeeded("assemble", {"routing": {}}),
            _succeeded(
                "decide",
                {
                    "blocking": [],
                    "surfaced": [],
                    "overflow": [],
                    "indeterminate": [],
                    "dropped": [],
                },
            ),
        ]
    )
    verdict = gate_dispatch._recover_plan_review_coach_failure(
        rec, LLMConfig(runner="fake"), error="coach step failed"
    )
    assert verdict is not None
    assert verdict["verdict"] == "INDETERMINATE"


def test_recover_verify_failure_threads_hierarchy_incomplete() -> None:
    from types import SimpleNamespace

    from rebar.llm.config import LLMConfig

    rec = SimpleNamespace(
        steps=[
            _succeeded(
                "precheck",
                {
                    "det_coverage": {},
                    "det_blocking": [],
                    "det_advisory": [],
                    "canonical_id": "T-1",
                    "ticket_type": "task",
                    "hierarchy_incomplete": True,
                    "hierarchy_incomplete_detail": ["enumeration"],
                },
            ),
            _succeeded("assemble", {"routing": {}}),
            _succeeded("finders", {"findings": [{"finding": "uses A1", "criteria": ["A1"]}]}),
        ]
    )
    verdict = gate_dispatch._recover_plan_review_verify_failure(
        rec, LLMConfig(runner="fake"), error="verify step failed"
    )
    assert verdict is not None
    # Advisory-only would normally fail OPEN to PASS (bug 59bc) — hierarchy_incomplete must
    # override that and still force INDETERMINATE.
    assert verdict["verdict"] == "INDETERMINATE"


def test_degraded_verdict_threads_hierarchy_incomplete_from_ctx() -> None:
    from rebar.llm.config import LLMConfig

    ctx = PlanContext(
        ticket_id="T-1",
        ticket_type="task",
        title="t",
        description="## Acceptance Criteria\n- [ ] x\n",
        hierarchy_incomplete=True,
        hierarchy_incomplete_detail=["enumeration"],
    )
    verdict = gate_dispatch._degraded_plan_review_verdict(
        ctx, LLMConfig(runner="fake"), error="outage", advisory_cap=10, runner_name="fake"
    )
    assert verdict["verdict"] == "INDETERMINATE"


# ── held-out: the drift floor must not revert a hierarchy-incomplete drop back to PASS ──────────
def test_drift_floor_does_not_revert_hierarchy_incomplete_to_pass() -> None:
    """A BLOCK verdict whose blocking bucket the drift floor emptied normally re-derives to PASS
    (``_recompute_verdict_after_drop``'s existing else branch). When the coverage carries
    ``hierarchy_incomplete``, it must re-derive to INDETERMINATE instead — the incomplete-hierarchy
    signal must survive a drift-floor drop, not be silently overwritten by the drop's PASS
    default."""
    verdict = {
        "verdict": "BLOCK",
        "blocking": [],  # the drift floor already emptied this bucket
        "indeterminate": [],
        "coverage": {"hierarchy_incomplete": True},
    }
    _recompute_verdict_after_drop(verdict)
    assert verdict["verdict"] == "INDETERMINATE"


# ── held-out: the schema stays forward/backward compatible ──────────────────────────────────────
def test_precheck_output_schema_accepts_old_and_new_shapes() -> None:
    from rebar.llm import findings as _findings

    old_shape = {
        "run_llm": True,
        "verdict": None,
        "canonical_id": "T-1",
        "ticket_type": "task",
        "review_phase": "planning",
        "det_blocking": [],
        "det_advisory": [],
        "det_coverage": {},
    }
    new_shape = {
        **old_shape,
        "hierarchy_incomplete": True,
        "hierarchy_incomplete_detail": ["enumeration"],
    }
    # Neither call raises: the hierarchy fields are optional additions, not required.
    _findings.validate_structured(dict(old_shape), "plan_review_precheck_output")
    _findings.validate_structured(dict(new_shape), "plan_review_precheck_output")

    without_required_phase = dict(old_shape)
    without_required_phase.pop("review_phase")
    with pytest.raises(_findings.FindingsError, match="review_phase"):
        _findings.validate_structured(without_required_phase, "plan_review_precheck_output")


# ── held-out: every plan-review.yaml `coach` call site forwards the two new precheck outputs ────
def test_plan_review_yaml_coach_steps_forward_hierarchy_incomplete() -> None:
    doc = gate_dispatch._gate_doc("plan-review", None)

    def _find_coach_steps(node):
        found = []
        if isinstance(node, dict):
            if node.get("uses") == "plan_review_coach":
                found.append(node)
            for v in node.values():
                found.extend(_find_coach_steps(v))
        elif isinstance(node, list):
            for item in node:
                found.extend(_find_coach_steps(item))
        return found

    coach_steps = _find_coach_steps(doc.get("steps"))
    assert len(coach_steps) == 4, "expected exactly 4 plan_review_coach call sites"
    for step in coach_steps:
        with_block = step.get("with") or {}
        assert (
            with_block.get("hierarchy_incomplete")
            == "${{ steps.precheck.outputs.hierarchy_incomplete }}"
        )
        assert (
            with_block.get("hierarchy_incomplete_detail")
            == "${{ steps.precheck.outputs.hierarchy_incomplete_detail }}"
        )
