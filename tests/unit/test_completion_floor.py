"""The Pass-3 COMPLETION floor (epic 66ac / story 6533).

The container-completion analogue of the novelty rising floor. A finding is dropped IFF it is
fully about DELIVERED, settled plan text — attribution is a delivered-now child (in the manifest's
delivered set) AND containment = limited-to-closed AND layer = plan-semantics AND priority < floor
AND its criterion is not in the always-preserve set. Every ambiguous/fail-safe sub-answer fails
toward KEEP. These tests pin: the pure drop predicate (each single-condition flip preserves the
finding, incl. a force-closed/undelivered attribution), the in-place verdict mutation (dropped→
`dropped` bucket with ``drop_reason="completion"``, namespaced coverage, corrected counts), and the
GATE — inert unless the ticket is a container AND ``completion_floor_active`` is true (the evidence
gate), plus the fail-safe (empty manifest / classification → no drops). No live LLM: the manifest +
completion map are injected.
"""

from __future__ import annotations

import types

import pytest

from rebar import config as core_config
from rebar.llm import plan_review
from rebar.llm.plan_review import orchestrator
from rebar.llm.plan_review.passes import (
    COMPLETION_ATTRIBUTION_NONE,
    COMPLETION_CONTAINMENT_CLOSED,
    COMPLETION_LAYER_PLAN,
    completion_floor_drop,
)

pytestmark = pytest.mark.unit

_FLOOR = 0.4
_PRESERVE = frozenset({"T5c", "T10"})
_DELIVERED_IDS = frozenset({"abcd-child"})  # the manifest's delivered-now set for these tests
# A finding that satisfies EVERY drop-eligible axis (attributed to a delivered child, contained in
# closed work, about the plan text, on a non-preserved criterion). Each test flips exactly one axis.
_DELIVERED = {
    "attribution": "abcd-child",
    "containment": COMPLETION_CONTAINMENT_CLOSED,
    "layer": COMPLETION_LAYER_PLAN,
}


def _drop(f, priority, criteria):
    return completion_floor_drop(
        f, priority, criteria, floor=_FLOOR, preserve=_PRESERVE, delivered_ids=_DELIVERED_IDS
    )


# ── the pure drop predicate: the full truth table ─────────────────────────────────────────────
def test_drop_when_all_axes_eligible_and_below_floor() -> None:
    assert _drop(_DELIVERED, 0.2, ["COH"]) is True


def test_keep_when_priority_at_or_above_floor() -> None:
    # exactly at floor is NOT below → keep; above → keep
    assert _drop(_DELIVERED, 0.4, ["COH"]) is False
    assert _drop(_DELIVERED, 0.9, ["COH"]) is False


def test_keep_when_attribution_none() -> None:
    assert _drop({**_DELIVERED, "attribution": COMPLETION_ATTRIBUTION_NONE}, 0.1, ["COH"]) is False


def test_keep_when_attribution_missing() -> None:
    f = {"containment": COMPLETION_CONTAINMENT_CLOSED, "layer": COMPLETION_LAYER_PLAN}
    assert _drop(f, 0.1, ["COH"]) is False


def test_keep_when_attribution_not_delivered() -> None:
    # a structural finding attributed to a FORCE-CLOSED / undelivered child (not in delivered_ids) —
    # "delivery is proven, not assumed": never dropped even if the model calls it plan-semantics.
    assert _drop({**_DELIVERED, "attribution": "forced-closed-child"}, 0.1, ["COH"]) is False


def test_keep_when_containment_spans_open() -> None:
    assert _drop({**_DELIVERED, "containment": "spans-open-or-system"}, 0.1, ["COH"]) is False


def test_keep_when_containment_na() -> None:
    assert _drop({**_DELIVERED, "containment": "n-a"}, 0.1, ["COH"]) is False


def test_keep_when_layer_delivered_functionality() -> None:
    assert _drop({**_DELIVERED, "layer": "delivered-functionality"}, 0.1, ["COH"]) is False


def test_keep_when_layer_na() -> None:
    assert _drop({**_DELIVERED, "layer": "n-a"}, 0.1, ["COH"]) is False


def test_preserve_set_veto_beats_every_other_axis() -> None:
    # a security (T5c) / contract (T10) finding is never dropped, even fully delivered + below floor
    assert _drop(_DELIVERED, 0.0, ["T5c"]) is False
    assert _drop(_DELIVERED, 0.0, ["T10"]) is False
    # preserved id anywhere in a multi-criterion finding still vetoes
    assert _drop(_DELIVERED, 0.0, ["COH", "T5c"]) is False


def test_empty_criteria_is_droppable() -> None:
    assert _drop(_DELIVERED, 0.1, []) is True
    assert _drop(_DELIVERED, 0.1, None) is True


# ── the in-place verdict mutation ───────────────────────────────────────────────────────────────
def _verdict() -> dict:
    return {
        "verdict": "PASS",
        "advisory": [
            {"id": "f0", "priority": 0.2, "criteria": ["COH"]},  # delivered + low → dropped
            {"id": "f1", "priority": 0.9, "criteria": ["COH"]},  # delivered + high prio → kept
            {"id": "f2", "priority": 0.1, "criteria": ["T5c"]},  # preserve-set → kept
        ],
        "dropped": [],
        "coverage": {"counts": {"advisory_surfaced": 3, "dropped": 0}},
    }


def _map() -> dict:
    # all three classified as fully-delivered plan-semantics; only f0 is below floor + non-preserved
    return {0: dict(_DELIVERED), 1: dict(_DELIVERED), 2: dict(_DELIVERED)}


def _apply(v, m):
    plan_review._apply_completion_floor_to_verdict(
        v, m, floor=_FLOOR, preserve=_PRESERVE, delivered_ids=_DELIVERED_IDS
    )


def test_apply_completion_floor_drops_only_eligible() -> None:
    v = _verdict()
    _apply(v, _map())

    assert [f["id"] for f in v["advisory"]] == ["f1", "f2"]  # f1 high-prio, f2 preserved
    assert [f["id"] for f in v["dropped"]] == ["f0"]
    d = v["dropped"][0]
    assert d["_floored"] is True
    assert d["drop_reason"] == "completion"
    assert d["completion"] == _DELIVERED  # the sub-answers ride along for the sidecar join
    cov = v["coverage"]
    assert cov["narrowed"] is True
    # namespaced so they never collide with the novelty floor's floored_* keys
    assert cov["completion_floored_criteria"] == ["COH"]
    assert cov["completion_floored_finding_ids"] == ["f0"]
    assert "floored_criteria" not in cov  # the novelty floor's key is untouched
    assert cov["counts"]["advisory_surfaced"] == 2
    assert cov["counts"]["dropped"] == 1


def test_apply_completion_floor_no_drop_leaves_verdict_untouched() -> None:
    v = _verdict()
    # every finding spans open work → nothing droppable
    m = {i: {**_DELIVERED, "containment": "spans-open-or-system"} for i in range(3)}
    _apply(v, m)
    assert [f["id"] for f in v["advisory"]] == ["f0", "f1", "f2"]
    assert v["dropped"] == []
    assert "narrowed" not in v["coverage"]  # absent on a normal review (byte-identical)
    assert v["coverage"]["counts"]["advisory_surfaced"] == 3


def test_apply_completion_floor_ignores_unclassified_index() -> None:
    v = _verdict()
    # f0 has NO completion entry (classification degraded for it) → kept despite low priority
    _apply(v, {1: dict(_DELIVERED), 2: dict(_DELIVERED)})
    assert v["dropped"] == []
    assert [f["id"] for f in v["advisory"]] == ["f0", "f1", "f2"]


# ── the gate (container + evidence gate) + fail-safe ──────────────────────────────────────────
def _cfg(*, completion_floor_active=True):
    verify = types.SimpleNamespace(
        completion_floor_active=completion_floor_active,
        completion_priority_floor=_FLOOR,
        completion_preserve_criteria=("T5c", "T10"),
    )
    return types.SimpleNamespace(verify=verify)


def _patch(monkeypatch, *, completion_floor_active, injected_map, manifest=None):
    monkeypatch.setattr(
        core_config,
        "load_config",
        lambda repo_root=None: _cfg(completion_floor_active=completion_floor_active),
    )
    # the gate builds the manifest (→ delivered_ids) then classifies; inject both (no live LLM)
    monkeypatch.setattr(
        orchestrator,
        "delivered_children_manifest",
        lambda cid, repo_root=None: (
            manifest if manifest is not None else [{"ticket_id": "abcd-child", "ac_text": "x"}]
        ),
    )
    monkeypatch.setattr(
        plan_review,
        "_classify_completion",
        lambda advisory, mani, *, ctx, cfg, runner: injected_map,
    )


def _ctx(*, has_children=True):
    return types.SimpleNamespace(plan_text="PLAN", has_children=has_children)


def test_floor_applied_when_container_and_flag_on(monkeypatch) -> None:
    _patch(monkeypatch, completion_floor_active=True, injected_map=_map())
    v = _verdict()
    plan_review._maybe_apply_completion_floor(
        "T", v, ctx=_ctx(), cfg=_cfg(), runner=object(), repo_root=None
    )
    assert [f["id"] for f in v["dropped"]] == ["f0"]
    assert v["coverage"]["narrowed"] is True


def test_floor_inert_when_flag_off(monkeypatch) -> None:
    """The evidence gate: a container review with completion_floor_active=False → un-floored."""
    _patch(monkeypatch, completion_floor_active=False, injected_map=_map())
    v = _verdict()
    plan_review._maybe_apply_completion_floor(
        "T", v, ctx=_ctx(), cfg=_cfg(completion_floor_active=False), runner=object(), repo_root=None
    )
    assert v["dropped"] == []
    assert "narrowed" not in v["coverage"]


def test_floor_inert_on_leaf_ticket(monkeypatch) -> None:
    """A leaf (no children) has no delivered children to settle → never floored."""
    _patch(monkeypatch, completion_floor_active=True, injected_map=_map())
    v = _verdict()
    plan_review._maybe_apply_completion_floor(
        "T", v, ctx=_ctx(has_children=False), cfg=_cfg(), runner=object(), repo_root=None
    )
    assert v["dropped"] == []


def test_floor_fail_safe_on_empty_manifest(monkeypatch) -> None:
    """No delivered children (empty manifest) → no delivered_ids → drops NOTHING."""
    _patch(monkeypatch, completion_floor_active=True, injected_map=_map(), manifest=[])
    v = _verdict()
    plan_review._maybe_apply_completion_floor(
        "T", v, ctx=_ctx(), cfg=_cfg(), runner=object(), repo_root=None
    )
    assert v["dropped"] == []


def test_floor_fail_safe_on_empty_classification(monkeypatch) -> None:
    """A degraded sub-call (empty completion map) drops NOTHING even with a delivered child."""
    _patch(monkeypatch, completion_floor_active=True, injected_map={})
    v = _verdict()
    plan_review._maybe_apply_completion_floor(
        "T", v, ctx=_ctx(), cfg=_cfg(), runner=object(), repo_root=None
    )
    assert v["dropped"] == []


# ── config ────────────────────────────────────────────────────────────────────────────────────
def test_config_defaults() -> None:
    vc = core_config.VerifyConfig()
    assert vc.completion_priority_floor == 0.4
    assert vc.completion_preserve_criteria == ("T5c", "T10")
    assert vc.completion_floor_active is False  # inert by default (the evidence gate)


def test_config_preserve_criteria_parses_csv() -> None:
    # a comma-separated string coerces to a trimmed tuple (both "T5c, T10" and ["T5c","T10"] parse)
    cfg = core_config.load_config(
        cli_overrides={"verify": {"completion_preserve_criteria": "T5c, T10, P1"}}
    )
    assert cfg.verify.completion_preserve_criteria == ("T5c", "T10", "P1")


def test_config_completion_floor_active_parses() -> None:
    cfg = core_config.load_config(cli_overrides={"verify": {"completion_floor_active": "true"}})
    assert cfg.verify.completion_floor_active is True


# ── the novelty floor now stamps its own drop_reason (sidecar disambiguation) ─────────────────
def test_novelty_floor_stamps_drop_reason() -> None:
    v = {
        "advisory": [{"id": "n0", "priority": 0.2, "criteria": ["E2"]}],
        "dropped": [],
        "coverage": {"counts": {"advisory_surfaced": 1, "dropped": 0}},
    }
    plan_review._apply_floor_to_verdict(v, {0: 0.9}, t_novel=0.7, floor=_FLOOR)
    assert v["dropped"][0]["drop_reason"] == "novelty"
