"""Offline convergence proof for the rising-floor remediation re-review (epic 7d43, child 4cb9).

Drives the REAL novelty mechanism over a ``FakeRunner`` (no live LLM, no network): the novelty
sub-call → ``decide.novelty`` → the Pass-3 rising floor. Proves the convergence behavior (novel
low-priority dropped, carryover enforced, novel high-priority preserved, and the loop reaches a
stable surfaced set) and that EVERY remediation precondition failing falls back to a
byte-identical full (un-floored) review.
"""

from __future__ import annotations

import copy
import types

import pytest

import rebar.signing as signing
from rebar import config as core_config
from rebar.llm import config as llm_config
from rebar.llm import plan_review
from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import attest, sidecar
from rebar.llm.review_kernel.verify import score_novelty
from rebar.llm.runner import FakeRunner, RunRequest

pytestmark = pytest.mark.unit

_T = 0.7
_FLOOR = 0.4
_ALL_NO = {"restates_prior_defect": "no", "cites_prior_location": "no", "matches_prior_fix": "no"}
_ALL_YES = {
    "restates_prior_defect": "yes",
    "cites_prior_location": "yes",
    "matches_prior_fix": "yes",
}


def _novelty_map_via_fakerunner(findings, prior, novelties):
    """Score novelty over ``findings`` against ``prior`` using a real FakeRunner that emits the
    canned ``novelties`` — exercising score_novelty → the `novelty` contract → decide.novelty."""
    fr = FakeRunner(structured={"novelties": novelties})
    cfg = LLMConfig()

    def run_chunk(instructions: str, context: str):
        req = RunRequest(
            system_prompt="novelty",
            instructions=instructions,
            config=cfg,
            reviewers=["plan-novelty"],
            mode="structured",
            output_schema="plan_review_novelty",
            execution_mode="single_turn",
        )
        return fr.run(req).get("novelties", []) or []

    return score_novelty(
        findings, prior_findings=prior, run_chunk=run_chunk, window_tokens=100_000, est_tokens=len
    )


def test_fakerunner_convergence_on_a_remediation_edit() -> None:
    """A remediation round: a novel low-priority finding is dropped, a carryover is enforced
    (kept), a novel high-priority finding is preserved — and a second identical round drops
    nothing more (the surfaced set has converged to a stable fixed point)."""
    advisory = [
        {"id": "f_novel_low", "priority": 0.2, "criteria": ["F1"], "finding": "a fresh nit"},
        {"id": "f_carryover", "priority": 0.2, "criteria": ["E2"], "finding": "the prior defect"},
        {"id": "f_novel_high", "priority": 0.9, "criteria": ["T4"], "finding": "fresh data loss"},
    ]
    prior = [{"id": "p0", "finding": "the prior defect", "location": "Scope", "criteria": ["E2"]}]
    novelties = [
        {"index": 0, "matches_prior": _ALL_NO},  # novel
        {"index": 1, "matches_prior": _ALL_YES},  # carryover (matches p0)
        {"index": 2, "matches_prior": _ALL_NO},  # novel
    ]
    nmap = _novelty_map_via_fakerunner(advisory, prior, novelties)
    assert nmap == {0: 1.0, 1: 0.0, 2: 1.0}

    verdict = {
        "verdict": "PASS",
        "advisory": copy.deepcopy(advisory),
        "dropped": [],
        "coverage": {"counts": {"advisory_surfaced": 3, "dropped": 0}},
    }
    plan_review._apply_floor_to_verdict(verdict, nmap, t_novel=_T, floor=_FLOOR)

    kept = [f["id"] for f in verdict["advisory"]]
    assert kept == ["f_carryover", "f_novel_high"]  # only the novel LOW-priority finding dropped
    assert [f["id"] for f in verdict["dropped"]] == ["f_novel_low"]
    assert verdict["coverage"]["narrowed"] is True
    assert verdict["coverage"]["floored_finding_ids"] == ["f_novel_low"]

    # CONVERGENCE: re-run a second identical round over the now-narrowed surfaced set. The
    # carryover is still enforced (kept) and the novel high-priority finding is still preserved;
    # nothing further drops → the surfaced set is a stable fixed point (the loop terminates).
    round2_findings = verdict["advisory"]
    round2_novelties = [
        {"index": 0, "matches_prior": _ALL_YES},  # f_carryover still matches a prior finding
        {"index": 1, "matches_prior": _ALL_NO},  # f_novel_high still novel...
    ]
    nmap2 = _novelty_map_via_fakerunner(round2_findings, prior, round2_novelties)
    verdict2 = {
        "verdict": "PASS",
        "advisory": copy.deepcopy(round2_findings),
        "dropped": [],
        "coverage": {"counts": {"advisory_surfaced": 2, "dropped": 0}},
    }
    plan_review._apply_floor_to_verdict(verdict2, nmap2, t_novel=_T, floor=_FLOOR)
    assert verdict2["dropped"] == []  # ...but high-priority, so NOT dropped → converged
    assert "narrowed" not in verdict2["coverage"]
    assert [f["id"] for f in verdict2["advisory"]] == ["f_carryover", "f_novel_high"]


def test_failed_novelty_subcall_keeps_everything() -> None:
    """A novel low-priority finding that WOULD be dropped is KEPT when the novelty sub-call fails
    (fail-safe: novelty defaults to 0.0 → carryover → never dropped)."""
    advisory = [{"id": "f0", "priority": 0.1, "criteria": ["F1"], "finding": "x"}]
    prior = [{"id": "p0", "finding": "y"}]

    def boom(instructions, context):
        raise RuntimeError("sub-call down")

    nmap = score_novelty(
        advisory, prior_findings=prior, run_chunk=boom, window_tokens=100_000, est_tokens=len
    )
    assert nmap == {0: 0.0}  # fail-safe carryover
    verdict = {"advisory": list(advisory), "dropped": [], "coverage": {"counts": {}}}
    plan_review._apply_floor_to_verdict(verdict, nmap, t_novel=_T, floor=_FLOOR)
    assert verdict["dropped"] == []  # nothing dropped on a broken signal


# ── each precondition failing → byte-identical full (un-floored) review ───────────────────────
_MIN_NS = 60 * 1_000_000_000


def _manifest(material, regver, sha):
    return attest.build_manifest(
        {"verdict": "PASS", "ticket_id": "T", "model": "m", "runner": "r"},
        material=material,
        regver=regver,
        verified_at_sha=sha,
    )


def _eligible_setup(monkeypatch, **overrides):
    """Monkeypatch the candidate's dependencies to a fully-eligible baseline; per-test overrides
    break ONE precondition."""
    cfg = {
        "prior_material": "OLD",
        "cur_material": "NEW",  # plan changed
        "prior_sha": "sha-base",
        "cur_sha": "sha-base",  # code unchanged
        "prior_regver": "REG",
        "cur_regver": "REG",  # registry unchanged
        "prior_findings": [{"finding": "prior"}],
        "last_ts": 10_000 * _MIN_NS,
    }
    cfg.update(overrides)
    manifest = _manifest(cfg["prior_material"], cfg["prior_regver"], cfg["prior_sha"])
    monkeypatch.setattr(
        signing,
        "verify_signature",
        lambda tid, repo_root=None: {"verified": True, "manifest": manifest, "key_id": "k"},
    )
    monkeypatch.setattr(
        attest, "current_material_fingerprint", lambda tid, repo_root=None: cfg["cur_material"]
    )
    monkeypatch.setattr(llm_config, "current_code_sha", lambda: cfg["cur_sha"])
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: cfg["cur_regver"])
    monkeypatch.setattr(
        sidecar,
        "latest_review_result",
        lambda tid, repo_root=None: (
            {"findings": cfg["prior_findings"]} if cfg["prior_findings"] is not None else None
        ),
    )
    monkeypatch.setattr(
        sidecar, "latest_review_timestamp", lambda tid, repo_root=None: cfg["last_ts"]
    )


def _verdict_with_droppable():
    # a finding that WOULD be dropped if the floor were active (novel + low-priority)
    return {
        "verdict": "PASS",
        "advisory": [{"id": "f0", "priority": 0.1, "criteria": ["F1"], "finding": "fresh nit"}],
        "dropped": [],
        "coverage": {"counts": {"advisory_surfaced": 1, "dropped": 0}},
    }


@pytest.mark.parametrize(
    "label,overrides",
    [
        ("code_drift", {"cur_sha": "sha-DIFFERENT"}),
        ("window_expiry", {"last_ts": 1 * _MIN_NS}),  # ancient → outside window
        ("registry_skew", {"cur_regver": "REG2"}),
        ("no_sidecar", {"prior_findings": None}),
        ("plan_unchanged", {"cur_material": "OLD"}),  # no edit → not a remediation
    ],
)
def test_each_precondition_falls_back_to_full_review(monkeypatch, label, overrides) -> None:
    """When any precondition fails, the candidate is NOT eligible, so the floor never runs and the
    verdict is byte-identical to a normal full review (the droppable finding survives)."""
    _eligible_setup(monkeypatch, **overrides)
    # an enabled config (so the only thing stopping the floor is the failed precondition)
    monkeypatch.setattr(
        core_config,
        "load_config",
        lambda repo_root=None: types.SimpleNamespace(
            verify=types.SimpleNamespace(
                remediation_window_minutes=60,
                novelty_drop_threshold=_T,
                novelty_priority_floor=_FLOOR,
            )
        ),
    )
    remediation = attest.remediation_mode_candidate(
        "T", window_minutes=60, now_ns=10_000 * _MIN_NS + 5 * _MIN_NS
    )
    assert remediation["eligible"] is False, f"{label} should not be eligible"

    verdict = _verdict_with_droppable()
    before = copy.deepcopy(verdict)
    plan_review._maybe_apply_rising_floor(
        "T",
        verdict,
        remediation,
        ctx=types.SimpleNamespace(plan_text="P"),
        cfg=LLMConfig(),
        runner=object(),
        repo_root=None,
    )
    assert verdict == before, f"{label}: verdict must be byte-identical (un-floored)"


def test_no_remediation_falls_back_to_full_review(monkeypatch) -> None:
    """remediation=None (e.g. config unreadable — the remediation_mode off switch was retired in
    story 4cdf) ⇒ the wrapper gets remediation=None ⇒ byte-identical full review."""
    verdict = _verdict_with_droppable()
    before = copy.deepcopy(verdict)
    plan_review._maybe_apply_rising_floor(
        "T",
        verdict,
        None,
        ctx=types.SimpleNamespace(plan_text="P"),
        cfg=LLMConfig(),
        runner=object(),
        repo_root=None,
    )
    assert verdict == before
