"""Region-gated novelty floor for code review (story blameless-grindable-noctule, epic
super-path-bag). Unit tests run against a MOCKED reader/scorer — no LLM, no store."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rebar.llm.code_review import contracts as cr_contracts
from rebar.llm.code_review import region_gate, workflow_ops

cr_contracts.register_contracts()


def _verify_cfg(active=True, t_novel=0.7, floor=0.4):
    return SimpleNamespace(
        verify=SimpleNamespace(
            novelty_drop_active=active,
            novelty_drop_threshold=t_novel,
            novelty_priority_floor=floor,
        )
    )


# ── region detector ───────────────────────────────────────────────────────────────────────────
def test_region_for_finding_unchanged_changed_unknown(tmp_path):
    from rebar.llm.plan_review import attest

    (tmp_path / "a.py").write_text("x = 1\n")
    cur = attest._hash_file("a.py", base=str(tmp_path))
    root = str(tmp_path)
    # UNCHANGED: prior hash == current
    assert (
        region_gate.region_for_finding({"location": "a.py:3"}, {"a.py": cur}, repo_root=root)
        == region_gate.REGION_UNCHANGED
    )
    # CHANGED: prior hash differs
    assert (
        region_gate.region_for_finding({"location": "a.py"}, {"a.py": "deadbeef"}, repo_root=root)
        == region_gate.REGION_CHANGED
    )
    # UNKNOWN: path absent from prior deps
    assert (
        region_gate.region_for_finding({"location": "a.py"}, {"other.py": cur}, repo_root=root)
        == region_gate.REGION_UNKNOWN
    )
    # UNKNOWN: no/empty location (absence-evidence), and multi-location
    assert region_gate.region_for_finding({"location": ""}, {"a.py": cur}, repo_root=root) == (
        region_gate.REGION_UNKNOWN
    )
    assert (
        region_gate.region_for_finding({"location": "a.py, b.py"}, {"a.py": cur}, repo_root=root)
        == region_gate.REGION_UNKNOWN
    )


def test_region_unknown_on_absent_sentinel(tmp_path):
    # a deleted file (current == absent) is UNKNOWN even if the prior recorded a real hash
    root = str(tmp_path)
    assert (
        region_gate.region_for_finding(
            {"location": "gone.py"}, {"gone.py": "abc123"}, repo_root=root
        )
        == region_gate.REGION_UNKNOWN
    )


# ── the floor ───────────────────────────────────────────────────────────────────────────────
def _apply(
    monkeypatch, *, advisory, nmap, region, prior_findings=None, coaching=None, cfg_active=True
):
    """Drive apply_region_gated_floor with a mocked reader/scorer/region/config; return verdict."""
    prior = {
        "findings": prior_findings if prior_findings is not None else [{"id": "P1"}],
        "deps": {},
    }
    monkeypatch.setattr(
        "rebar.llm.code_review.sidecar.latest_code_review_result",
        lambda key, repo_root=None: prior,
    )
    monkeypatch.setattr(workflow_ops, "score_code_novelty", lambda *a, **k: nmap)
    monkeypatch.setattr(region_gate, "region_for_finding", lambda f, deps, repo_root=None: region)
    monkeypatch.setattr(
        "rebar.config.load_config", lambda repo_root=None: _verify_cfg(active=cfg_active)
    )
    verdict = {"advisory": list(advisory), "coaching": coaching or [], "dropped": []}
    workflow_ops.apply_region_gated_floor(
        verdict, key="session:s", cfg=SimpleNamespace(repo_path=None), runner=object()
    )
    return verdict


def test_floor_drops_unchanged_novel_low_priority(monkeypatch):
    v = _apply(
        monkeypatch,
        advisory=[{"id": "f1", "finding": "nit", "priority": 0.2, "location": "a.py"}],
        nmap={0: (0.9, "")},  # novel (>= 0.7 threshold)
        region=region_gate.REGION_UNCHANGED,
    )
    assert v["advisory"] == []
    assert len(v["dropped"]) == 1
    assert v["dropped"][0]["drop_reason"] == "novelty-region"


def test_floor_never_drops_on_changed(monkeypatch):
    v = _apply(
        monkeypatch,
        advisory=[{"id": "f1", "priority": 0.2, "location": "a.py"}],
        nmap={0: (0.9, "")},
        region=region_gate.REGION_CHANGED,
    )
    assert len(v["advisory"]) == 1 and v["dropped"] == []


def test_floor_never_drops_on_unknown(monkeypatch):
    v = _apply(
        monkeypatch,
        advisory=[{"id": "f1", "priority": 0.2, "location": "multi, loc"}],
        nmap={0: (0.9, "")},
        region=region_gate.REGION_UNKNOWN,
    )
    assert len(v["advisory"]) == 1 and v["dropped"] == []


def test_floor_never_drops_high_priority_even_if_unchanged(monkeypatch):
    v = _apply(
        monkeypatch,
        advisory=[{"id": "f1", "priority": 0.9, "location": "a.py"}],  # above the floor
        nmap={0: (0.95, "")},
        region=region_gate.REGION_UNCHANGED,
    )
    assert len(v["advisory"]) == 1 and v["dropped"] == []


def test_carryover_stamped_and_uncoached(monkeypatch):
    v = _apply(
        monkeypatch,
        advisory=[{"id": "f1", "finding": "repeat", "priority": 0.2, "location": "a.py"}],
        nmap={0: (0.1, "P1")},  # low novelty + matched a prior → carryover, not dropped
        region=region_gate.REGION_UNCHANGED,
        coaching=[
            {"move_id": "m", "finding_refs": ["f1"]},
            {"move_id": "m2", "finding_refs": ["other"]},
        ],
    )
    assert len(v["advisory"]) == 1
    assert v["advisory"][0]["carried_from"] == "P1"  # stamped with the matched prior id
    assert v["dropped"] == []
    # coaching for the carried finding is stripped; the unrelated note survives
    assert [c["move_id"] for c in v["coaching"]] == ["m2"]


def test_reader_error_or_no_prior_yields_no_drops(monkeypatch):
    # reader returns None → self-gate inert, verdict untouched
    monkeypatch.setattr(
        "rebar.llm.code_review.sidecar.latest_code_review_result", lambda key, repo_root=None: None
    )
    monkeypatch.setattr("rebar.config.load_config", lambda repo_root=None: _verify_cfg(active=True))
    verdict = {"advisory": [{"id": "f1", "priority": 0.2, "location": "a.py"}], "coaching": []}
    workflow_ops.apply_region_gated_floor(
        verdict, key="session:s", cfg=SimpleNamespace(repo_path=None), runner=object()
    )
    assert len(verdict["advisory"]) == 1 and not verdict.get("dropped")


def test_floor_inert_when_evidence_gate_off(monkeypatch):
    v = _apply(
        monkeypatch,
        advisory=[{"id": "f1", "priority": 0.2, "location": "a.py"}],
        nmap={0: (0.9, "")},
        region=region_gate.REGION_UNCHANGED,
        cfg_active=False,  # verify.novelty_drop_active off → no drops
    )
    assert len(v["advisory"]) == 1 and not v.get("dropped")


# ── the novelty scorer captures matched_prior_id (for carried_from) ───────────────────────────
def test_score_code_novelty_returns_novelty_and_matched_id():
    from rebar.llm.runner import FakeRunner, LLMConfig

    runner = FakeRunner(
        structured={
            "novelties": [
                {
                    "index": 0,
                    "matched_prior_id": "P7",
                    "matches_prior": {
                        "restates_prior_defect": "yes",
                        "cites_prior_location": "yes",
                        "matches_prior_fix": "yes",
                    },
                }
            ]
        }
    )
    out = workflow_ops.score_code_novelty(
        [{"finding": "x", "criteria": ["c"], "evidence": [], "impact": ""}],
        [{"id": "P7", "finding": "x-prior"}],
        diff_text="--- a\n+++ b\n",
        cfg=LLMConfig(model="m"),
        runner=runner,
    )
    assert 0 in out
    nov, matched = out[0]
    assert matched == "P7"
    assert nov == pytest.approx(0.0, abs=1e-9)  # all-yes matches → carryover (novelty 0.0)


def test_novelty_contract_registered_as_kernel_novelty_model():
    from rebar.llm import contracts as reg

    assert reg.response_model_for("code_review_novelty").__name__ == "NoveltyOutput"
