"""WS-K1: characterization / golden-master of the review-ops public surface.

Freezes the library return contract of review_ticket / review_code /
scan_epics_for_spec BEFORE WS-K2 reframes their internals as workflows. Driven with
an injected FakeRunner (offline, deterministic) so the SHAPE — not a live model —
is what is pinned. WS-K2 must keep these green (the parallel-diff invariant).

CLI stdout/exit + MCP outputSchema for these ops are separately frozen by
test_schema_outputs.py / test_mcp_output_schema_coverage.py (review_result) and
test_llm_optionality.py (degradation), which WS-K2 must also keep green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar.llm.runner import FakeRunner

pytest.importorskip("jsonschema")

# The frozen review_result key set (the output contract every review op returns).
_REQUIRED_KEYS = {"findings", "runner", "model", "trace_id", "target", "reviewers"}


def test_review_ticket_contract(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Review me", description="body", repo_root=r)
    fake = FakeRunner([{"severity": "high", "dimension": "bugs", "detail": "x"}], summary="s")
    result = rebar.llm.review_ticket(tid, "ticket-quality", runner=fake, repo_root=r)

    assert _REQUIRED_KEYS <= set(result)
    assert result["runner"] == "fake"
    assert result["target"] == {"kind": "ticket", "ticket_ids": [tid]}
    assert result["reviewers"] == ["ticket-quality"]
    f = result["findings"][0]
    assert f["severity"] == "high" and f["dimension"] == "bugs" and f["detail"] == "x"
    assert result["summary"] == "s"


def test_review_ticket_graph_target_kind(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    epic = rebar.create_ticket("epic", "E", repo_root=r)
    rebar.create_ticket("task", "child", parent=epic, repo_root=r)
    result = rebar.llm.review_ticket(
        epic, "ticket-quality", graph=True, runner=FakeRunner([]), repo_root=r
    )
    assert result["target"]["kind"] == "ticket_graph"
    assert epic in result["target"]["ticket_ids"]


def test_review_code_contract(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+print(1)\n"
    result = rebar.llm.review_code(
        diff_text=diff, reviewers=["code-quality"], runner=FakeRunner([]), repo_root=r
    )
    assert _REQUIRED_KEYS <= set(result)
    assert "findings" in result and isinstance(result["findings"], list)


def test_scan_epics_for_spec_contract(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    rebar.create_ticket("epic", "Epic A", description="some scope", repo_root=r)
    result = rebar.llm.scan_epics_for_spec("the spec text", runner=FakeRunner([]), repo_root=r)
    assert _REQUIRED_KEYS <= set(result)
    assert isinstance(result["findings"], list)


def test_review_result_validates_against_schema(rebar_repo: Path) -> None:
    from rebar import schemas

    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "T", repo_root=r)
    result = rebar.llm.review_ticket(
        tid,
        "ticket-quality",
        runner=FakeRunner([{"severity": "low", "dimension": "x", "detail": "d"}]),
        repo_root=r,
    )
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
