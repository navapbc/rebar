"""The Pass-3 rising-floor drop rule (epic 7d43, child cc5b).

A finding is dropped IFF it is NOVEL (``novelty >= T_novel``) AND LOW-PRIORITY
(``priority < floor``). These tests pin: the pure drop predicate (all four quadrants), the
in-place verdict mutation (dropped→`dropped` bucket, narrowed coverage, corrected counts), and
the TRIPLE GATE — the floor is inert unless config ``remediation_mode`` + per-review eligibility +
``novelty_drop_active`` all hold (the evidence gate). No live LLM: the novelty map is injected.
"""

from __future__ import annotations

import types

import pytest

from rebar import config as core_config
from rebar.llm import plan_review
from rebar.llm.plan_review import sidecar
from rebar.llm.review_kernel import decide

pytestmark = pytest.mark.unit

_T = 0.7
_FLOOR = 0.4


# ── the pure drop predicate ───────────────────────────────────────────────────────────────────
def test_rising_floor_drop_quadrants() -> None:
    # novel + low-priority → DROP
    assert decide.rising_floor_drop(0.2, 0.9, t_novel=_T, floor=_FLOOR) is True
    # novel + high-priority → KEEP (a real defect the edit introduced)
    assert decide.rising_floor_drop(0.6, 0.9, t_novel=_T, floor=_FLOOR) is False
    # carryover (low novelty) + low-priority → KEEP (must still be resolved)
    assert decide.rising_floor_drop(0.2, 0.3, t_novel=_T, floor=_FLOOR) is False
    # carryover + high-priority → KEEP
    assert decide.rising_floor_drop(0.6, 0.3, t_novel=_T, floor=_FLOOR) is False
    # boundary: novelty exactly T_novel is novel; priority exactly floor is NOT below floor
    assert decide.rising_floor_drop(0.39, 0.7, t_novel=_T, floor=_FLOOR) is True
    assert decide.rising_floor_drop(0.4, 0.7, t_novel=_T, floor=_FLOOR) is False


# ── the in-place verdict mutation ───────────────────────────────────────────────────────────────
def _verdict() -> dict:
    return {
        "verdict": "PASS",
        "advisory": [
            {"id": "f0", "priority": 0.2, "criteria": ["E2"]},  # novel + low → dropped
            {"id": "f1", "priority": 0.6, "criteria": ["T4"]},  # novel + high → kept
            {"id": "f2", "priority": 0.1, "criteria": ["F1"]},  # carryover (low novelty) → kept
        ],
        "dropped": [],
        "coverage": {"counts": {"advisory_surfaced": 3, "dropped": 0}},
    }


def test_apply_floor_drops_only_novel_low_priority() -> None:
    v = _verdict()
    novelty = {0: 0.9, 1: 0.95, 2: 0.1}  # f0/f1 novel, f2 carryover
    plan_review._apply_floor_to_verdict(v, novelty, t_novel=_T, floor=_FLOOR)

    kept_ids = [f["id"] for f in v["advisory"]]
    assert kept_ids == ["f1", "f2"]  # f0 dropped; f1 (high prio) + f2 (carryover) kept
    dropped_ids = [f["id"] for f in v["dropped"]]
    assert dropped_ids == ["f0"]
    assert v["dropped"][0]["_floored"] is True and v["dropped"][0]["novelty"] == 0.9
    cov = v["coverage"]
    assert cov["narrowed"] is True
    assert cov["floored_criteria"] == ["E2"]
    assert cov["floored_finding_ids"] == ["f0"]
    # counts corrected to match the post-floor buckets (the code-review G6 catch)
    assert cov["counts"]["advisory_surfaced"] == 2
    assert cov["counts"]["dropped"] == 1


def test_apply_floor_no_drop_leaves_verdict_untouched() -> None:
    v = _verdict()
    before = {k: (list(val) if isinstance(val, list) else val) for k, val in v.items()}
    # nothing novel enough to drop
    plan_review._apply_floor_to_verdict(v, {0: 0.1, 1: 0.1, 2: 0.1}, t_novel=_T, floor=_FLOOR)
    assert [f["id"] for f in v["advisory"]] == [f["id"] for f in before["advisory"]]
    assert v["dropped"] == []
    assert "narrowed" not in v["coverage"]  # absent on a normal review
    assert v["coverage"]["counts"]["advisory_surfaced"] == 3


# ── the triple gate (inert by default; evidence gate) ─────────────────────────────────────────
def _cfg(*, remediation_mode=True, novelty_drop_active=True):
    verify = types.SimpleNamespace(
        novelty_drop_active=novelty_drop_active,
        novelty_drop_threshold=_T,
        novelty_priority_floor=_FLOOR,
    )
    return types.SimpleNamespace(verify=verify)


def _patch(monkeypatch, *, novelty_drop_active, injected_map):
    # _maybe_apply_rising_floor does `from rebar import config as _config; _config.load_config(...)`
    monkeypatch.setattr(
        core_config,
        "load_config",
        lambda repo_root=None: _cfg(novelty_drop_active=novelty_drop_active),
    )
    monkeypatch.setattr(
        sidecar,
        "latest_review_result",
        # A prior SURFACED finding (decision="advisory") — surfaced_findings must keep it so the
        # floor has a non-empty prior set and stays live (bug old-frilly-plankton filter).
        lambda tid, repo_root=None: {"findings": [{"finding": "x", "decision": "advisory"}]},
    )
    monkeypatch.setattr(
        plan_review,
        "_score_floor_novelty",
        lambda advisory, prior_findings, *, ctx, cfg, runner, repo_root: injected_map,
    )


def _ctx():
    return types.SimpleNamespace(plan_text="PLAN")


def test_floor_applied_when_all_gates_open(monkeypatch) -> None:
    _patch(monkeypatch, novelty_drop_active=True, injected_map={0: 0.9})
    v = _verdict()
    plan_review._maybe_apply_rising_floor(
        "T", v, {"eligible": True}, ctx=_ctx(), cfg=_cfg(), runner=object(), repo_root=None
    )
    assert [f["id"] for f in v["dropped"]] == ["f0"]
    assert v["coverage"]["narrowed"] is True


def test_floor_inert_when_drop_flag_off(monkeypatch) -> None:
    """The evidence gate: even with eligibility, novelty_drop_active=False → un-floored."""
    _patch(monkeypatch, novelty_drop_active=False, injected_map={0: 0.9})
    v = _verdict()
    plan_review._maybe_apply_rising_floor(
        "T",
        v,
        {"eligible": True},
        ctx=_ctx(),
        cfg=_cfg(novelty_drop_active=False),
        runner=object(),
        repo_root=None,
    )
    assert v["dropped"] == []
    assert "narrowed" not in v["coverage"]


def test_floor_inert_when_not_eligible(monkeypatch) -> None:
    _patch(monkeypatch, novelty_drop_active=True, injected_map={0: 0.9})
    v = _verdict()
    # remediation None (config off) and not-eligible both → no floor
    plan_review._maybe_apply_rising_floor(
        "T", v, None, ctx=_ctx(), cfg=_cfg(), runner=object(), repo_root=None
    )
    plan_review._maybe_apply_rising_floor(
        "T", v, {"eligible": False}, ctx=_ctx(), cfg=_cfg(), runner=object(), repo_root=None
    )
    assert v["dropped"] == []


# ── config defaults ───────────────────────────────────────────────────────────────────────────
def test_config_defaults() -> None:
    vc = core_config.VerifyConfig()
    assert vc.novelty_drop_threshold == 0.7
    assert vc.novelty_priority_floor == 0.4
    assert vc.novelty_drop_active is False  # inert by default (the evidence gate)


# ── surfaced-only prior set (bug old-frilly-plankton) ─────────────────────────────────────────
def test_surfaced_findings_keeps_only_client_returned_decisions() -> None:
    """``surfaced_findings`` keeps block/advisory, drops the dropped/indeterminate/overflow ones
    ``build_payload`` also persists — so a previously-DROPPED finding can never re-enter a prior
    set. None/empty payloads degrade to ``[]``."""
    payload = {
        "findings": [
            {"id": "b", "decision": "block"},
            {"id": "a", "decision": "advisory"},
            {"id": "d", "decision": "dropped", "drop_reason": "novelty"},
            {"id": "i", "decision": "indeterminate"},
            {"id": "o", "decision": "overflow"},
            {"id": "n"},  # no decision key at all
        ]
    }
    assert [f["id"] for f in sidecar.surfaced_findings(payload)] == ["b", "a"]
    assert sidecar.surfaced_findings(None) == []
    assert sidecar.surfaced_findings({}) == []


def test_dropped_prior_findings_never_reach_the_novelty_scorer(monkeypatch) -> None:
    """REGRESSION (bug old-frilly-plankton): a finding permanently DROPPED for convergence must not
    re-enter the novelty prior set. ``_maybe_apply_rising_floor`` must feed ``_score_floor_novelty``
    the SURFACED-only prior findings — else the recurring dropped finding matches its own prior
    record, scores low-novelty 'carryover', and escapes the floor that dropped it."""
    # A prior sidecar holding BOTH a surfaced advisory AND a previously-floored (dropped) finding.
    prior_payload = {
        "findings": [
            {"id": "surfaced1", "finding": "kept concern", "decision": "advisory"},
            {
                "id": "floored1",
                "finding": "repeat noise",
                "decision": "dropped",
                "drop_reason": "novelty",
            },
        ]
    }
    monkeypatch.setattr(
        core_config, "load_config", lambda repo_root=None: _cfg(novelty_drop_active=True)
    )
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: prior_payload)
    captured: dict[str, object] = {}

    def _capture(advisory, prior_findings, *, ctx, cfg, runner, repo_root):
        captured["prior_findings"] = prior_findings
        return {}

    monkeypatch.setattr(plan_review, "_score_floor_novelty", _capture)
    plan_review._maybe_apply_rising_floor(
        "T", _verdict(), {"eligible": True}, ctx=_ctx(), cfg=_cfg(), runner=object(), repo_root=None
    )
    ids = [f["id"] for f in captured["prior_findings"]]  # type: ignore[union-attr]
    assert ids == ["surfaced1"], "dropped prior finding leaked into the novelty prior set"
