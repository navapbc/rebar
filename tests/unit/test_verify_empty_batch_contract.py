"""Pass-2 with ZERO Pass-1 findings: the request must not solicit verifications.

The verify step always makes one aggregate call, even when Pass-1 produced nothing. The
request it carries must agree with the index domain it was built from: an empty batch lists
no finding, so asking for "one verification per finding" solicits indices that cannot exist.
Every index a verifier then returns falls outside ``range(len(findings))`` and is classified
``unexpected`` by :func:`reshape_verifications` — an ERROR log plus a contract-violation
record stamped into the signed verdict's coverage, on a review that is otherwise clean.

The focused prerequisite verifier already states the empty-case contract explicitly
(``prerequisite_workflow_ops.plan_review_prerequisite_verify_inputs``); the general Pass-2
request is the seam that does not.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.review_kernel import reshape_verifications
from rebar.llm.review_kernel import verify as kverify
from rebar.llm.workflow.executor import StepContext


def _fnd(text: str) -> dict[str, Any]:
    return {"finding": text, "criteria": ["E1"], "evidence": ["e"], "impact": "i"}


def _solicited_indices(request: str) -> list[int]:
    """The finding indices a verify request actually presents to the verifier."""
    return [int(m) for m in re.findall(r"### finding index (\d+)", request)]


def _built_request(findings: list[dict[str, Any]]) -> str:
    """The single Pass-2 request the gate sends for ``findings`` (the producer expression in
    ``workflow_ops.plan_review_verify_inputs``: one chunk, or one EMPTY chunk when none fit)."""
    chunks, _omitted = kverify.verify_request_chunks(
        findings, window_tokens=200_000, est_tokens=lambda s: len(s) // 4
    )
    instructions = [kverify.verify_instructions(chunk) for chunk in (chunks or [[]])]
    assert len(instructions) == 1
    return instructions[0]


def test_request_solicits_exactly_the_indices_it_carries() -> None:
    """A verify request must solicit exactly the index domain it lists — no more."""
    for count in (0, 1, 3):
        findings = [_fnd(f"finding {i}") for i in range(count)]
        request = _built_request(findings)
        assert _solicited_indices(request) == list(range(count))


def test_empty_batch_request_asks_for_an_empty_verifications_array() -> None:
    """With no findings, the request must instruct an EMPTY verifications array rather than
    ask for one verification per finding — the instruction that cannot be met truthfully."""
    request = _built_request([])
    assert "empty verifications array" in request
    assert "one verification per finding" not in request


def test_empty_batch_response_indices_are_all_unexpected() -> None:
    """The captured live signature: with no findings the valid index domain is empty, so any
    index a verifier returns is `unexpected` and the whole response is discarded."""
    raw = [{"index": i, "severity_attributes": {}, "binary": {}} for i in range(4)]
    reshape = reshape_verifications(raw, valid_indices=range(0))
    assert reshape.summary() == {"unexpected": [0, 1, 2, 3]}
    assert reshape.verifications == {}
    # 0 can only be `unexpected` when the finding list is empty — the basis fact that pins the
    # mismatch to the SENT side (a request built from zero findings), not to bad indices.
    assert all(0 in range(n) for n in (1, 2, 3))


def test_live_verify_inputs_emits_the_empty_batch_contract(monkeypatch) -> None:
    """The producer that runs live (`plan_review_verify_inputs`) emits exactly one instruction
    for a zero-finding review, and it carries the empty-batch contract."""
    import rebar.llm.plan_review as pr
    from rebar.llm.plan_review import context_assembly, workflow_ops

    monkeypatch.setattr(
        context_assembly,
        "assemble_context",
        lambda tid, repo_root=None: SimpleNamespace(plan_text="PLAN-TEXT"),
    )
    monkeypatch.setattr(
        "rebar.llm.config.resolve_gate_config", lambda repo_root: LLMConfig(runner="fake")
    )
    monkeypatch.setattr(pr, "_verifier_cfg", lambda cfg: cfg)
    ctx = StepContext(
        run_id="r",
        step_id="verify_inputs",
        kind="uses",
        step={"id": "verify_inputs", "uses": "plan_review_verify_inputs"},
        inputs={"ticket_id": "T-1", "findings": []},
        workflow={"name": "plan-review"},
        target_ticket="T-1",
        repo_root=None,
    )
    instructions = workflow_ops.plan_review_verify_inputs(ctx)["instructions"]
    assert len(instructions) == 1
    assert "empty verifications array" in instructions[0]
    assert "one verification per finding" not in instructions[0]
