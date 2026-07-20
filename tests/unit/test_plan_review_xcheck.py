"""Validation-assessment cross-checks (bug 5e40) — the two intra-verdict / in-store consistency
drops that converge a non-deterministic re-review:

* CONTRADICTION cross-check (ticket AC): two findings in ONE verdict that mutually contradict —
  reproduces 5e40 finding A1 (a BLOCKING "no one is tasked with capturing the snapshot" alongside
  an ADVISORY "the parent explicitly assigns capture to S1"). The contradicted member is dropped.
* COMMENT-TRAIL consultation (ticket AC): a finding that re-litigates a point the ticket's recorded
  comment trail already resolved — reproduces 5e40 finding B3 (the ``rebase:chain`` endpoint,
  conceded in-trail). It is dropped.

Both drops are DETERMINISTIC given the (injected) judgment — the LLM detection seam is exercised
separately with a ``FakeRunner``. No live LLM.
"""

from __future__ import annotations

import types

import pytest

from rebar import config as core_config
from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import xcheck
from rebar.llm.review_kernel import decide

pytestmark = pytest.mark.unit


# ── the A1 verdict: a BLOCKING false positive contradicted by an ADVISORY that states the fact ──
def _contradiction_verdict() -> dict:
    """combined index order [b0, a0, a1]: b0 is the BLOCKING false positive ('no one is tasked with
    capturing the snapshot', priority 0.85); a0 is the ADVISORY that CONTRADICTS it ('the parent
    explicitly assigns capture to S1', priority 0.4); a1 is unrelated advisory noise."""
    return {
        "verdict": "BLOCK",
        "blocking": [
            {
                "id": "b0",
                "priority": 0.85,
                "criteria": ["E2"],
                "finding": "no one is tasked with capturing the pre-cutover snapshot",
            },
        ],
        "advisory": [
            {
                "id": "a0",
                "priority": 0.4,
                "criteria": ["F1"],
                "finding": "the parent explicitly assigns snapshot capture to S1",
            },
            {"id": "a1", "priority": 0.2, "criteria": ["P5"], "finding": "unrelated advisory"},
        ],
        "dropped": [],
        "coverage": {"counts": {"blocking": 1, "advisory_surfaced": 2, "dropped": 0}},
    }


# ── pure predicate: which member of a contradictory pair is dropped ─────────────────────────────
def test_contradiction_drop_index_uses_model_identified_member() -> None:
    # the model judges b0 (index 0) the contradicted/false one — drop it even though it is the
    # HIGHER-priority finding (the whole 5e40 A1 point: a false block outscores its true refuter).
    prios = [0.85, 0.4, 0.2]
    pair = {"a": 0, "b": 1, "contradiction": True, "drop": 0}
    assert decide.contradiction_drop_index(pair, prios) == 0


def test_contradiction_drop_index_tiebreak_when_model_gives_no_drop() -> None:
    prios = [0.85, 0.4]
    # no valid `drop` → deterministic tiebreak: drop the LOWER-priority member (index 1).
    assert decide.contradiction_drop_index({"a": 0, "b": 1, "contradiction": True}, prios) == 1
    # equal priority → tie broken by higher index.
    assert decide.contradiction_drop_index({"a": 0, "b": 1, "contradiction": True}, [0.5, 0.5]) == 1


def test_contradiction_drop_index_fail_safe() -> None:
    prios = [0.85, 0.4, 0.2]
    assert decide.contradiction_drop_index({"a": 0, "b": 1, "contradiction": False}, prios) is None
    assert decide.contradiction_drop_index({"a": 0, "b": 9, "contradiction": True}, prios) is None
    assert decide.contradiction_drop_index({"a": 0, "b": 0, "contradiction": True}, prios) is None
    assert decide.contradiction_drop_index("garbage", prios) is None


# ── the in-place verdict mutation (contradiction) ──────────────────────────────────────────────
def test_apply_contradiction_drops_the_contradicted_block_and_converges() -> None:
    v = _contradiction_verdict()
    # combined [b0, a0, a1]: b0 (idx 0) and a0 (idx 1) contradict; model drops b0.
    xcheck.apply_contradiction_drops(v, [{"a": 0, "b": 1, "contradiction": True, "drop": 0}])
    assert [f["id"] for f in v["blocking"]] == []  # the false block dropped
    assert [f["id"] for f in v["advisory"]] == ["a0", "a1"]  # the refuter survives
    assert [f["id"] for f in v["dropped"]] == ["b0"]
    dropped = v["dropped"][0]
    assert dropped["drop_reason"] == "contradiction"
    assert dropped["contradicts"] == "a0"  # records the surviving counterpart
    cov = v["coverage"]
    assert cov["narrowed"] is True and cov["contradiction_xcheck"] is True
    assert cov["contradiction_dropped_finding_ids"] == ["b0"]
    assert cov["counts"] == {"blocking": 0, "advisory_surfaced": 2, "dropped": 1}
    assert v["verdict"] == "PASS"  # BLOCK→PASS re-derivation (only the false block was blocking)


def test_apply_contradiction_no_drop_leaves_verdict_untouched() -> None:
    v = _contradiction_verdict()
    xcheck.apply_contradiction_drops(v, [{"a": 0, "b": 1, "contradiction": False}])
    assert v["dropped"] == [] and v["verdict"] == "BLOCK"
    assert "narrowed" not in v["coverage"]


def test_apply_contradiction_empty_pairs_noop() -> None:
    v = _contradiction_verdict()
    xcheck.apply_contradiction_drops(v, [])
    assert v["dropped"] == [] and v["verdict"] == "BLOCK"


# ── the entry gate (contradiction): config-gated + sub-call wiring ─────────────────────────────
def _cfg(*, contradiction=False, comment_trail=False):
    verify = types.SimpleNamespace(
        contradiction_xcheck_active=contradiction,
        comment_trail_xcheck_active=comment_trail,
    )
    return types.SimpleNamespace(verify=verify)


def test_maybe_apply_contradiction_inert_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        core_config, "load_config", lambda repo_root=None: _cfg(contradiction=False)
    )
    v = _contradiction_verdict()
    ctx = types.SimpleNamespace(plan_text="p", state={})
    xcheck.maybe_apply_contradiction(
        "t", v, ctx=ctx, cfg=LLMConfig(runner="fake"), runner=None, repo_root=None
    )
    assert v["dropped"] == [] and v["verdict"] == "BLOCK"


def test_maybe_apply_contradiction_runs_subcall_when_active(monkeypatch) -> None:
    from rebar.llm.runner import FakeRunner

    monkeypatch.setattr(core_config, "load_config", lambda repo_root=None: _cfg(contradiction=True))
    v = _contradiction_verdict()
    ctx = types.SimpleNamespace(plan_text="the plan text", state={})
    fr = FakeRunner(structured={"pairs": [{"a": 0, "b": 1, "contradiction": True, "drop": 0}]})
    xcheck.maybe_apply_contradiction(
        "t", v, ctx=ctx, cfg=LLMConfig(runner="fake"), runner=fr, repo_root=None
    )
    assert [f["id"] for f in v["dropped"]] == ["b0"]
    assert v["verdict"] == "PASS"


# ── COMMENT-TRAIL consultation (B3): the rebase:chain finding conceded in-trail ─────────────────
def _trail_verdict() -> dict:
    """combined [a0, a1]: a0 re-litigates the ``rebase:chain`` endpoint (conceded in-trail); a1 is a
    genuine open finding the trail never touched."""
    return {
        "verdict": "PASS",
        "blocking": [],
        "advisory": [
            {
                "id": "a0",
                "priority": 0.0,
                "criteria": ["T3"],
                "finding": "POST /changes/{id}/rebase:chain endpoint unverified",
            },
            {"id": "a1", "priority": 0.3, "criteria": ["T1"], "finding": "an unrelated open gap"},
        ],
        "dropped": [],
        "coverage": {"counts": {"blocking": 0, "advisory_surfaced": 2, "dropped": 0}},
    }


def test_comment_trail_drop_predicate() -> None:
    assert decide.comment_trail_drop({"resolved_in_trail": "yes"}) is True
    assert decide.comment_trail_drop({"resolved_in_trail": "no"}) is False
    assert decide.comment_trail_drop({"resolved_in_trail": "insufficient"}) is False
    assert decide.comment_trail_drop(None) is False
    assert decide.comment_trail_drop("garbage") is False


def test_apply_comment_trail_drops_resolved_finding() -> None:
    v = _trail_verdict()
    # index 0 (a0) resolved in-trail; index 1 (a1) not.
    resolved = {
        0: {"resolved_in_trail": "yes", "comment_ref": "c1"},
        1: {"resolved_in_trail": "no"},
    }
    xcheck.apply_comment_trail_drops(v, resolved)
    assert [f["id"] for f in v["advisory"]] == ["a1"]
    assert [f["id"] for f in v["dropped"]] == ["a0"]
    dropped = v["dropped"][0]
    assert dropped["drop_reason"] == "comment_trail"
    assert dropped["comment_ref"] == "c1"
    cov = v["coverage"]
    assert cov["narrowed"] is True and cov["comment_trail_xcheck"] is True
    assert cov["comment_trail_dropped_finding_ids"] == ["a0"]
    assert cov["counts"] == {"blocking": 0, "advisory_surfaced": 1, "dropped": 1}


def test_apply_comment_trail_no_resolved_is_noop() -> None:
    v = _trail_verdict()
    xcheck.apply_comment_trail_drops(v, {0: {"resolved_in_trail": "no"}})
    assert v["dropped"] == [] and "narrowed" not in v["coverage"]


def test_maybe_apply_comment_trail_inert_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        core_config, "load_config", lambda repo_root=None: _cfg(comment_trail=False)
    )
    v = _trail_verdict()
    ctx = types.SimpleNamespace(plan_text="p", state={"comments": [{"body": "x"}]})
    xcheck.maybe_apply_comment_trail(
        "t", v, ctx=ctx, cfg=LLMConfig(runner="fake"), runner=None, repo_root=None
    )
    assert v["dropped"] == []


def test_maybe_apply_comment_trail_runs_subcall_when_active(monkeypatch) -> None:
    from rebar.llm.runner import FakeRunner

    monkeypatch.setattr(core_config, "load_config", lambda repo_root=None: _cfg(comment_trail=True))
    v = _trail_verdict()
    ctx = types.SimpleNamespace(
        plan_text="p",
        state={
            "comments": [
                {
                    "body": "rebase:chain DOES exist; the advisory concedes this",
                    "author": "Joe",
                    "timestamp": 1,
                }
            ]
        },
    )
    fr = FakeRunner(
        structured={"assessments": [{"index": 0, "resolved_in_trail": "yes", "comment_ref": "c1"}]}
    )
    xcheck.maybe_apply_comment_trail(
        "t", v, ctx=ctx, cfg=LLMConfig(runner="fake"), runner=fr, repo_root=None
    )
    assert [f["id"] for f in v["dropped"]] == ["a0"]


def test_maybe_apply_comment_trail_inert_without_comments(monkeypatch) -> None:
    monkeypatch.setattr(core_config, "load_config", lambda repo_root=None: _cfg(comment_trail=True))
    v = _trail_verdict()
    ctx = types.SimpleNamespace(plan_text="p", state={"comments": []})
    xcheck.maybe_apply_comment_trail(
        "t", v, ctx=ctx, cfg=LLMConfig(runner="fake"), runner=None, repo_root=None
    )
    assert v["dropped"] == []
