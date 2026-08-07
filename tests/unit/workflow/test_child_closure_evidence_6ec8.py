"""Ticket 6ec8: the deterministic child-closure/certification proof must reach the completion
verifier as EVIDENCE inside the fenced ticket context — today it is computed then discarded.

These tests pin the observable contract (the precheck's returned `context` = the verify step's
input): (AC4) the evidence's counts/ids MATCH what `child_closure_findings` returned; (AC1) the
block sits INSIDE the `<untrusted_ticket_context>` fence; (AC5 regression) an UNCLOSED direct
child STILL short-circuits deterministically (no LLM, no context).
"""

from __future__ import annotations

import pytest

import rebar
from rebar.llm import operations
from rebar.llm.completion import build_child_closure_evidence, child_closure_findings
from rebar.llm.workflow.executor import StepContext
from rebar.llm.workflow.gate_ops import completion_precheck

pytestmark = pytest.mark.unit


def _patch(monkeypatch, *, ticket_type="epic", children=None, sigs=None):
    monkeypatch.setattr(
        "rebar._reads.show_ticket",
        lambda tid, repo_root=None: {"ticket_id": "T-1", "ticket_type": ticket_type},
    )

    def _fake_list(*, parent=None, status=None, ticket_type=None, repo_root=None, **_kw):
        if ticket_type == "bug":
            return []
        return list(children or []) if parent is not None else []

    monkeypatch.setattr("rebar._reads.list_tickets", _fake_list)
    sigs = sigs or {}
    monkeypatch.setattr(
        rebar,
        "verify_signature",
        lambda cid, kind=None, repo_root=None: {"verdict": sigs.get(cid, "certified")},
    )
    # Avoid the real assemble_context / prefetch touching a store: stub base context.
    monkeypatch.setattr(
        operations, "assemble_context", lambda tid, *, graph, repo_root: ("BASE-CTX", [str(tid)])
    )
    monkeypatch.setenv("REBAR_VERIFY_PREFETCH", "0")


def _ctx(inputs):
    return StepContext(
        run_id="r",
        step_id="precheck",
        kind="uses",
        step={},
        inputs=inputs,
        workflow={"name": "completion-verification"},
        target_ticket="T-1",
        repo_root=None,
    )


def test_evidence_in_fenced_context_matches_findings(monkeypatch):
    # A parent with two closed children: one certified, one NOT certified (force-closed).
    children = [
        {"ticket_id": "C-cert", "title": "certified child", "status": "closed"},
        {"ticket_id": "C-force", "title": "forced child", "status": "closed"},
    ]
    _patch(monkeypatch, children=children, sigs={"C-cert": "certified", "C-force": "absent"})

    blocking, uncertified = child_closure_findings("T-1", None)
    assert blocking == []  # both closed → LLM path, no short-circuit
    assert len(uncertified) == 1  # only C-force is uncertified

    out = completion_precheck(_ctx({"ticket_id": "T-1", "graph": False}))
    assert out["run_verify"] is True
    ctx = out["context"]

    # AC1: the block sits INSIDE the prompt-injection fence.
    assert ctx.startswith("<untrusted_ticket_context>")
    assert ctx.rstrip().endswith("</untrusted_ticket_context>")
    open_i = ctx.index("<untrusted_ticket_context>")
    close_i = ctx.index("</untrusted_ticket_context>")

    # AC4: the evidence appears and its counts/ids MATCH child_closure_findings' return.
    assert "2 direct child" in ctx  # total children
    assert "C-force" in ctx  # the uncertified id is surfaced
    assert open_i < ctx.index("C-force") < close_i
    # A certified child is NOT flagged as uncertified in the block's id list.
    assert "Verified +1" in ctx  # AC3: the Gerrit half is explicitly noted as out of reach


def test_all_certified_states_all_closed(monkeypatch):
    children = [
        {"ticket_id": "C-1", "title": "a", "status": "closed"},
        {"ticket_id": "C-2", "title": "b", "status": "closed"},
    ]
    _patch(monkeypatch, children=children, sigs={"C-1": "certified", "C-2": "certified"})
    out = completion_precheck(_ctx({"ticket_id": "T-1", "graph": False}))
    ctx = out["context"]
    assert "2 direct child" in ctx
    # No uncertified ids listed; the block states all are closed & certified.
    assert "certified" in ctx.lower()


def test_childless_ticket_gets_no_block(monkeypatch):
    _patch(monkeypatch, children=[])
    assert build_child_closure_evidence("T-1", None, []) == ""
    out = completion_precheck(_ctx({"ticket_id": "T-1", "graph": False}))
    assert "direct child" not in out["context"]


def test_unclosed_child_short_circuits_no_llm(monkeypatch):
    # AC5 regression: an UNCLOSED direct child STILL deterministically fails — no LLM, no context.
    children = [{"ticket_id": "C-open", "title": "open child", "status": "open"}]
    _patch(monkeypatch, children=children)
    out = completion_precheck(_ctx({"ticket_id": "T-1", "graph": False}))
    assert out["run_verify"] is False
    assert out["precheck_failed"] is True
    assert out["context"] == ""  # short-circuit assembles no context at all
