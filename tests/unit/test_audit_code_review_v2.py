"""Story 7c84: code-review sidecar -> lossless v2 (all buckets + thresholds).

code_review_result_v1 persisted only the SURFACED buckets (blocking + advisory + coaching)
and omitted the dropped / indeterminate findings Pass-3 produced, plus the numeric
block_threshold/blocking_enabled each finding was judged against. v2 pools every bucket the
code-review gate produces — blocking, advisory, dropped, indeterminate — with each finding's
Pass-2 verification + Pass-3 determination + resolved threshold, while keeping the SURFACED
(blocking+advisory) reader byte-unchanged. (Code review has NO overflow bucket.)
"""

from __future__ import annotations

import pytest

import rebar.llm.workflow.executor as _ex
from rebar.llm.code_review import sidecar
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers code_review ops

pytestmark = pytest.mark.unit


def _finding(fid: str, decision: str, *, bt: float = 0.9, be: bool = False) -> dict:
    return {
        "id": fid,
        "finding": f"finding {fid}",
        "criteria": ["c1"],
        "location": "src/x.py:1",
        "decision": decision,
        "verification": {"binary": {"is_verifiable": "yes"}},
        "block_threshold": bt,
        "blocking_enabled": be,
    }


def _full_verdict() -> dict:
    return {
        "verdict": "PASS",
        "blocking": [_finding("b1", "block", be=True)],
        "advisory": [_finding("a1", "advisory")],
        "dropped": [_finding("d1", "dropped")],
        "indeterminate": [_finding("i1", "indeterminate")],
        "coaching": [],
        "coverage": {},
    }


def test_build_payload_v2_pools_all_buckets_with_verification_and_thresholds() -> None:
    """v2 persists every bucket — blocking + advisory + dropped + indeterminate — and each
    persisted finding retains its Pass-2 verification, Pass-3 decision, and the resolved
    block_threshold/blocking_enabled it was judged against, plus a norm_id."""
    p = sidecar.build_payload(_full_verdict(), target_ticket="T1")
    assert p["schema"] == "code_review_result_v2"
    assert [f["id"] for f in p["blocking"]] == ["b1"]
    assert [f["id"] for f in p["advisory"]] == ["a1"]
    assert [f["id"] for f in p["dropped"]] == ["d1"]
    assert [f["id"] for f in p["indeterminate"]] == ["i1"]
    for bucket in ("blocking", "advisory", "dropped", "indeterminate"):
        f = p[bucket][0]
        assert f["verification"] == {"binary": {"is_verifiable": "yes"}}
        assert "block_threshold" in f and "blocking_enabled" in f
        assert f["norm_id"].startswith("n")


def test_build_payload_v2_absent_buckets_degrade_gracefully() -> None:
    """A verdict with no dropped/indeterminate buckets (an all-clean review) still emits a valid
    v2 payload — the new buckets default to empty lists, never KeyError."""
    p = sidecar.build_payload({"verdict": "PASS", "advisory": []}, target_ticket="T1")
    assert p["schema"] == "code_review_result_v2"
    assert p["dropped"] == []
    assert p["indeterminate"] == []


def _ctx(inputs: dict):
    return _ex.StepContext(
        run_id="r",
        step_id="s",
        kind="uses",
        step={"uses": "code_review_coach"},
        inputs=inputs,
        workflow={},
        repo_root=None,
    )


def test_code_review_coach_forwards_dropped_and_indeterminate() -> None:
    """The terminal-verdict assembly op forwards the decide stage's dropped/indeterminate buckets
    onto the verdict dict, so build_payload downstream can persist them. Without this passthrough
    they are step-local and lost."""
    out = _ex.STEP_REGISTRY["code_review_coach"](
        _ctx(
            {
                "blocking": [_finding("b1", "block", be=True)],
                "surfaced": [_finding("a1", "advisory")],
                "dropped": [_finding("d1", "dropped")],
                "indeterminate": [_finding("i1", "indeterminate")],
                "notes": [],
            }
        )
    )
    assert [f["id"] for f in out["blocking"]] == ["b1"]
    assert [f["id"] for f in out["advisory"]] == ["a1"]
    assert [f["id"] for f in out.get("dropped", [])] == ["d1"]
    assert [f["id"] for f in out.get("indeterminate", [])] == ["i1"]


def test_build_payload_v2_surfaced_union_unchanged_vs_v1() -> None:
    """AC: the SURFACED set a downstream reader reconstructs (blocking + advisory union) is
    identical whether the payload is v1-shaped or v2-shaped — v2 only ADDS buckets, it never
    changes which findings are surfaced. dropped/indeterminate are NOT in the surfaced union."""
    p_v2 = sidecar.build_payload(_full_verdict(), target_ticket="T1")
    surfaced_v2 = {f["id"] for f in (p_v2["blocking"] + p_v2["advisory"])}
    assert surfaced_v2 == {"b1", "a1"}
    assert "d1" not in surfaced_v2 and "i1" not in surfaced_v2
