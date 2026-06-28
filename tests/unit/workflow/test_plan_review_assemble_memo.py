"""Run-scoped memoization of ``assemble_context`` on the plan-review gate
(rancid-vane-wreak).

Each plan-review workflow op (``precheck`` / ``assemble_criteria`` / ``verify_inputs`` /
``coach_inputs``) independently calls ``orchestrator.assemble_context``, an N+1 store read
(``show_ticket`` + ``list_tickets(parent=)`` + one ``show_ticket`` per child). Pre-change one
gate run re-assembled the graph ~4× → ~4·(2+K) reads (K = direct children). The fix wraps the
run in :func:`orchestrator.assemble_context_cache`, collapsing those repeated identical calls to
a SINGLE graph read while returning an IDENTICAL ``PlanContext`` (verdict bytes unchanged).

These tests are OFFLINE: ``rebar.show_ticket`` / ``rebar.list_tickets`` are spied (no git store),
and a branching :class:`FakeRunner` drives the finder/verify/coach steps (no model / network).
"""

from __future__ import annotations

import dataclasses

import pytest

from rebar.llm import findings as _findings
from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import orchestrator
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import gate_dispatch

pytestmark = pytest.mark.unit

_TARGET = "epic-0000-0000-0001"
_CHILD = "task-0000-0000-0002"

_GOOD_AC = (
    "## Why\nthe system needs X.\n\n## What\nbuild X in `src/rebar/x.py`.\n\n"
    "## Scope\njust X.\n\n## Acceptance Criteria\n"
    "- [ ] X is observably true\n- [ ] another check\n"
)


def _parent_state() -> dict:
    return {
        "ticket_id": _TARGET,
        "ticket_type": "story",
        "title": "Build X",
        "description": _GOOD_AC,
        "deps": [],
    }


def _child_state() -> dict:
    return {
        "ticket_id": _CHILD,
        "ticket_type": "task",
        "title": "Sub X",
        "description": _GOOD_AC,
        "deps": [],
        "parent_id": _TARGET,
    }


class _ReadSpy:
    """Counts the rebar reads ``assemble_context`` issues: ``show_ticket`` (one for the
    parent + one per child fetched whole) and ``list_tickets`` (the child enumeration)."""

    def __init__(self) -> None:
        self.show_calls: list[str] = []
        self.list_calls: list[str | None] = []

    def install(self, monkeypatch, *, children: bool) -> None:
        import rebar

        parent = _parent_state()
        child = _child_state()

        def _show(tid, *, repo_root=None):  # noqa: ANN001
            self.show_calls.append(tid)
            return dict(child) if tid == _CHILD else dict(parent)

        def _list(*, parent=None, repo_root=None):  # noqa: ANN001
            self.list_calls.append(parent)
            return [dict(child)] if children else []

        monkeypatch.setattr(rebar, "show_ticket", _show)
        monkeypatch.setattr(rebar, "list_tickets", _list)

    @property
    def total(self) -> int:
        return len(self.show_calls) + len(self.list_calls)


class _BranchingRunner(FakeRunner):
    """One offline runner for every plan-review LLM step (finder batch + verify/coach agent
    steps). Branches on the output schema and returns a schema-valid EMPTY payload, so the run
    reaches a clean PASS with no model — same fake the metrics test uses."""

    def run(self, req):
        schema = req.output_schema or ""
        if "verification" in schema:
            payload: dict = {"verifications": []}
        elif "coach" in schema:
            payload = {"notes": [{"move_id": "1", "subject": "the X design", "finding_refs": []}]}
        else:  # the Pass-1 finder
            payload = {"analysis": "", "findings": []}
        validated = _findings.validate_structured(dict(payload), schema)
        return {**validated, "runner": self.name, "model": None, "trace_id": None}


def _cfg() -> LLMConfig:
    return dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5")


def _run_gate(monkeypatch, *, children: bool) -> tuple[dict, _ReadSpy]:
    spy = _ReadSpy()
    spy.install(monkeypatch, children=children)
    ctx = PlanContext(ticket_id=_TARGET, ticket_type="story", title="Build X", description=_GOOD_AC)
    verdict = gate_dispatch.produce_plan_review_verdict(
        ctx, _cfg(), runner=_BranchingRunner(), advisory_cap=10, repo_root=None
    )
    return verdict, spy


# ── 1. read-counter spy: the gate assembles the graph at most once ────────────────
def test_one_gate_run_assembles_graph_at_most_once_leaf(monkeypatch) -> None:
    """A leaf ticket's graph (no children) costs 2 reads (show_ticket + list_tickets). The four
    workflow ops that call assemble_context must share ONE assembly, not re-read 4× (8 reads)."""
    verdict, spy = _run_gate(monkeypatch, children=False)

    assert verdict["verdict"] == "PASS", verdict.get("coverage")
    # 2+K reads with K=0 children: exactly one show_ticket + one list_tickets.
    assert len(spy.show_calls) == 1, spy.show_calls
    assert len(spy.list_calls) == 1, spy.list_calls
    assert spy.total == 2, spy.total
    # The pre-change behavior re-assembled per op (~4×); the memo collapses it to one.
    assert spy.show_calls == [_TARGET]


def test_one_gate_run_assembles_graph_at_most_once_with_child(monkeypatch) -> None:
    """A parent with K=1 child costs 2+K = 3 reads (show parent + list + show child). Across the
    four ops that is 4·(2+K)=12 pre-change; the run-scoped memo keeps it at 3 (a single graph
    read), proving the redundant re-assembly is eliminated for containers too."""
    verdict, spy = _run_gate(monkeypatch, children=True)

    assert verdict["verdict"] == "PASS", verdict.get("coverage")
    # 2+K reads with K=1: show parent + list_tickets + show child == 3, ONCE for the whole run.
    assert spy.total == 3, (spy.show_calls, spy.list_calls)
    assert spy.show_calls == [_TARGET, _CHILD]
    assert spy.list_calls == [_TARGET]


# ── 2. characterization: the memo returns an IDENTICAL PlanContext (verdict bytes) ─
def test_memoized_assemble_returns_identical_context(monkeypatch) -> None:
    """Inside an active scope, repeated assemble_context calls return the SAME object; the value
    is field-for-field identical to a fresh (un-memoized) assembly — so nothing downstream sees a
    different context (the verdict-byte-preservation guarantee at the assembly seam)."""
    spy = _ReadSpy()
    spy.install(monkeypatch, children=True)

    # A fresh (un-memoized) assembly is the byte reference.
    reference = orchestrator.assemble_context(_TARGET, repo_root=None)
    reads_after_reference = spy.total

    with orchestrator.assemble_context_cache():
        first = orchestrator.assemble_context(_TARGET, repo_root=None)
        second = orchestrator.assemble_context(_TARGET, repo_root=None)

    # Within the scope the second call is a cache HIT (the very same object), and no new reads
    # were issued for it: the scope cost exactly one graph read (2+K=3), same as the reference.
    assert first is second
    assert spy.total - reads_after_reference == 3, spy.total
    # The memoized context equals the fresh one field-for-field (identity at the value level).
    assert dataclasses.asdict(first) == dataclasses.asdict(reference)


def test_cache_does_not_leak_across_scopes(monkeypatch) -> None:
    """The memo is dropped on scope exit: a second scope re-reads the graph (no cross-run leak),
    and outside any scope every call reads fresh (the historical behavior is preserved)."""
    spy = _ReadSpy()
    spy.install(monkeypatch, children=False)

    with orchestrator.assemble_context_cache():
        orchestrator.assemble_context(_TARGET, repo_root=None)
        orchestrator.assemble_context(_TARGET, repo_root=None)  # cache hit
    after_first = spy.total
    assert after_first == 2  # one graph read for the whole first scope

    with orchestrator.assemble_context_cache():
        orchestrator.assemble_context(_TARGET, repo_root=None)
    assert spy.total == after_first + 2  # the second scope re-read (no leak)

    # Outside any scope, every call reads fresh — byte-identical to the prior behavior.
    orchestrator.assemble_context(_TARGET, repo_root=None)
    orchestrator.assemble_context(_TARGET, repo_root=None)
    assert spy.total == after_first + 2 + 4


def test_distinct_keys_are_cached_separately(monkeypatch) -> None:
    """The memo keys on every input that changes the result (ticket_id + repo_root + cfg fields +
    active read-roots), so a different ticket within the same scope is NOT served a stale entry."""
    import rebar

    seen: list[str] = []

    def _show(tid, *, repo_root=None):  # noqa: ANN001
        seen.append(tid)
        return {
            "ticket_id": tid,
            "ticket_type": "task",
            "title": tid,
            "description": _GOOD_AC,
            "deps": [],
        }

    monkeypatch.setattr(rebar, "show_ticket", _show)
    monkeypatch.setattr(rebar, "list_tickets", lambda parent=None, repo_root=None: [])

    with orchestrator.assemble_context_cache():
        a1 = orchestrator.assemble_context("ticket-A", repo_root=None)
        b1 = orchestrator.assemble_context("ticket-B", repo_root=None)
        a2 = orchestrator.assemble_context("ticket-A", repo_root=None)  # hit
        b2 = orchestrator.assemble_context("ticket-B", repo_root=None)  # hit

    assert a1 is a2 and b1 is b2
    assert a1.ticket_id == "ticket-A" and b1.ticket_id == "ticket-B"
    # Two distinct tickets each assembled once (two show_ticket calls), the repeats were hits.
    assert seen == ["ticket-A", "ticket-B"]
