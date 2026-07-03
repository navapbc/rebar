"""The ``overlay_triggers`` precompute step + the precompute-then-``if:`` overlay pattern (A1).

Conditional criterion INCLUSION reuses the engine's existing per-step ``if:`` skip-guard; the only
new piece is a deterministic scripted step that COMPUTES the trigger booleans an ``if:`` references
(so the deliberately-tiny expression grammar never needs operators/regex). These tests pin the
trigger logic directly, the downstream include/skip behavior, and replay-stability of the decision.
"""

from __future__ import annotations

import pytest

from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import (
    steps as _steps,  # noqa: F401 — registers overlay_triggers into STEP_REGISTRY
)
from rebar.llm.workflow.steps import overlay_triggers

pytestmark = pytest.mark.unit


def _ctx(**inputs) -> _ex.StepContext:
    return _ex.StepContext(
        run_id="r",
        step_id="triggers",
        kind="scripted",
        step={},
        inputs=inputs,
        workflow={},
        target_ticket=inputs.get("ticket_id"),
    )


# ── (1) keyword triggers — pure, no ticket (advisory E5: test the trigger logic itself) ───────────
def test_keyword_triggers_case_insensitive_substring():
    out = overlay_triggers(
        _ctx(
            text="This plan stores the SECRET api Token in plaintext.",
            keyword_triggers={
                "security": ["secret", "password", "token"],
                "migration": ["alter table", "backfill"],
            },
        )
    )
    assert out == {"security": True, "migration": False}


def test_keyword_triggers_empty_text_is_all_false():
    out = overlay_triggers(_ctx(text="", keyword_triggers={"security": ["secret"]}))
    assert out == {"security": False}


def test_no_triggers_and_no_ticket_yields_empty():
    # Nothing requested (no keyword_triggers, no structural/linked_types) → no outputs.
    assert overlay_triggers(_ctx(text="anything")) == {}


# ── (2) structural triggers — read the ticket graph, reduce to booleans ───────────────────────────
def test_structural_has_children_and_linked_session_log(monkeypatch):

    monkeypatch.setattr(
        "rebar._reads.deps",
        lambda tid, repo_root=None: {
            "children": ["c1", "c2"],
            "deps": [{"target_id": "sl1", "relation": "relates_to"}],
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        "rebar._reads.show_ticket", lambda tid, repo_root=None: {"ticket_type": "session_log"}
    )
    out = overlay_triggers(_ctx(ticket_id="t1", linked_types=["session_log"]))
    assert out["has_children"] is True
    assert out["has_linked_session_log"] is True


def test_structural_no_children_no_linked(monkeypatch):

    monkeypatch.setattr(
        "rebar._reads.deps",
        lambda tid, repo_root=None: {"children": [], "deps": [], "blockers": []},
    )
    out = overlay_triggers(_ctx(ticket_id="t1", linked_types=["session_log"], structural=True))
    assert out["has_children"] is False
    assert out["has_linked_session_log"] is False


# ── (3)/(4) downstream inclusion: a criterion gated by `if:` over a computed trigger
class _Rec(_ex.RunRecorder):
    """Minimal in-memory recorder: last-writer-wins markers by frame_key + resume support."""

    def __init__(self, store=None):
        self.store = store if store is not None else {}
        self.events: list[dict] = []

    def run_started(self, record):
        self.events.append(dict(record))

    def run_finished(self, record): ...

    def step_recorded(self, record):
        self.events.append(dict(record))
        if record.get("status") == "running":
            return
        self.store[record.get("frame_key") or record.get("step_id")] = dict(record)

    def completed_step(self, run_id, frame_key):
        rec = self.store.get(frame_key)
        return rec if rec and rec.get("status") == "succeeded" else None


def _overlay_wf() -> dict:
    return {
        "schema_version": "2",
        "name": "overlay-keyword-demo",
        "inputs": {"plan": {"type": "string"}},
        "steps": [
            {
                "id": "triggers",
                "uses": "overlay_triggers",
                "with": {
                    "text": "${{ inputs.plan }}",
                    "keyword_triggers": {"security": ["secret", "password"]},
                },
            },
            {
                "id": "security_review",
                "needs": ["triggers"],
                "uses": "noop",
                "if": "${{ steps.triggers.outputs.security }}",
            },
        ],
    }


def _registry():
    return {**_ex.STEP_REGISTRY, "noop": lambda ctx: {"ran": True}}


def test_criterion_included_when_trigger_truthy():
    rec = _Rec()
    _ex.run_workflow(
        _overlay_wf(),
        {"plan": "the design persists a secret token"},
        recorder=rec,
        scripted_registry=_registry(),
        agent_runner=_ex.FakeAgentRunner(),
    )
    assert rec.store["security_review"]["status"] == "succeeded"


def test_criterion_skipped_when_trigger_falsy():
    rec = _Rec()
    _ex.run_workflow(
        _overlay_wf(),
        {"plan": "a perfectly clean plan with nothing sensitive"},
        recorder=rec,
        scripted_registry=_registry(),
        agent_runner=_ex.FakeAgentRunner(),
    )
    assert rec.store["security_review"]["status"] == "skipped"


# ── replay-stability: the RECORDED trigger output drives the decision, not live store state ──
def _structural_wf() -> dict:
    return {
        "schema_version": "2",
        "name": "overlay-structural-demo",
        "steps": [
            {"id": "triggers", "uses": "overlay_triggers", "with": {"structural": True}},
            {
                "id": "container_review",
                "needs": ["triggers"],
                "uses": "noop",
                "if": "${{ steps.triggers.outputs.has_children }}",
            },
        ],
    }


def test_inclusion_is_stable_under_a_store_edit_on_replay(monkeypatch):
    """A structural trigger reads the ticket graph at run time, but its boolean is RECORDED as the
    step output; on replay/resume the `if:` evaluates over that recorded output, so a store edit
    (a changed `rebar.deps`) between the original run and the replay cannot change inclusion."""

    # Original run: the ticket HAS children → has_children truthy → the criterion runs + recorded.
    monkeypatch.setattr(
        "rebar._reads.deps",
        lambda tid, repo_root=None: {"children": ["c1"], "deps": [], "blockers": []},
    )
    rec = _Rec()
    _ex.run_workflow(
        _structural_wf(),
        {},
        recorder=rec,
        scripted_registry=_registry(),
        agent_runner=_ex.FakeAgentRunner(),
        target_ticket="t1",
    )
    assert rec.store["triggers"]["outputs"]["has_children"] is True
    assert rec.store["container_review"]["status"] == "succeeded"

    # STORE EDIT: the ticket now has NO children. Replay/resume with the SAME recorder — the
    # recorded has_children=True is reused (live `deps` is never re-read), so the decision holds.
    monkeypatch.setattr(
        "rebar._reads.deps",
        lambda tid, repo_root=None: {"children": [], "deps": [], "blockers": []},
    )
    _ex.run_workflow(
        _structural_wf(),
        {},
        recorder=rec,
        scripted_registry=_registry(),
        agent_runner=_ex.FakeAgentRunner(),
        target_ticket="t1",
    )
    assert rec.store["triggers"]["outputs"]["has_children"] is True
    assert rec.store["container_review"]["status"] == "succeeded"
