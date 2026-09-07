"""Regression tests for separate code and ticket roots in attested gates.

The orphan tickets branch is materialized apart from the code snapshot and exposed through
``current_tickets_root()``. Assembly captures it in ``PlanContext.tickets_root`` before
pass-one worker fan-out. Every plan-review ticket read must use that root rather than
``ctx.repo_root`` or it drops linked session logs and prior concerns. Tests run without a
store or model.
"""

from __future__ import annotations

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import pass1, registry, sidecar
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit

_CODE_ROOT = "/code-snapshot/NO-tickets-tracker"
_TICKETS_ROOT = "/tickets-snapshot/HAS-tickets-tracker"


def _ctx(**over) -> PlanContext:
    base = dict(
        ticket_id="rec-0000-0000-0001",
        ticket_type="task",
        title="A task",
        description="## Acceptance Criteria\n- [ ] observably correct\n",
        repo_root=_CODE_ROOT,
        tickets_root=_TICKETS_ROOT,
    )
    base.update(over)
    return PlanContext(**base)


def test_linked_session_log_reads_via_tickets_root(monkeypatch) -> None:
    """``_linked_session_log`` fetches the linked session_log ticket from the TICKETS
    root, never the code snapshot — else in an attested gate the read hits a missing
    ``.tickets-tracker`` and the ISF context is silently dropped."""
    import rebar._reads as _reads

    seen: dict[str, object] = {}

    def _fake_show(ticket_id, *, repo_root=None):
        seen["root"] = repo_root
        return {"ticket_type": "session_log", "title": "SL", "description": "log body"}

    monkeypatch.setattr(_reads, "show_ticket", _fake_show)
    ctx = _ctx(state={"deps": [{"target_id": "sl-1", "relation": "relates_to"}]})
    pass1._linked_session_log(ctx, LLMConfig(runner="fake"), FakeRunner())
    # Must resolve against the tickets root, never the code snapshot.
    assert seen["root"] == _TICKETS_ROOT


def test_recall_reads_prior_concerns_via_tickets_root(monkeypatch) -> None:
    """The Pass-1 recall path reads prior REVIEW_RESULT concerns from the TICKETS root —
    feeding it the code snapshot silently returns no concerns (recall disabled)."""
    seen: dict[str, object] = {}

    def _fake_pc(ticket_id, *, repo_root=None, **_):
        seen["root"] = repo_root
        return []

    monkeypatch.setattr(sidecar, "prior_concerns", _fake_pc)
    ctx = _ctx()
    pass1.run_pass1(
        ctx,
        LLMConfig(runner="fake"),
        FakeRunner(structured={"analysis": "", "findings": []}),
        [registry.by_id()["E2"]],
        [],
        {},
    )
    # Recall must read prior concerns from the tickets root, never the code snapshot.
    assert seen["root"] == _TICKETS_ROOT
