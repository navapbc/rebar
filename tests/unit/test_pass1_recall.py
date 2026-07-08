"""Plan-review Pass-1 finding-memory / recall (story disused-unpoliced-solenodon).

Recall re-surfaces prior-review findings the fresh Pass-1 finder MISSED, as POST-Pass-1 candidates
for the UNCHANGED Pass-2 verifier. The Pass-1 finder never receives prior findings (independence by
construction; ADR 0008 Invariant 1 / the pinned test_prior_findings_only_reach_the_novelty_seam).

Proving command:
    .venv/bin/pytest tests/unit/test_pass1_recall.py tests/unit/test_plan_review_novelty.py -v
"""

from __future__ import annotations

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import pass1, registry, sidecar
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.review_kernel.decide import pass3_decide
from rebar.llm.runner import FakeRunner


def _ctx(tmp_path) -> PlanContext:
    return PlanContext(
        ticket_id="rec-0000-0000-0001",
        ticket_type="task",
        title="A task",
        description="## Acceptance Criteria\n- [ ] the widget is observably correct\n",
        repo_root=str(tmp_path),
    )


def _run(ctx: PlanContext, fresh_findings: list[dict], runner=None) -> list[dict]:
    fr = runner or FakeRunner(structured={"analysis": "", "findings": fresh_findings})
    return pass1.run_pass1(ctx, LLMConfig(runner="fake"), fr, [registry.by_id()["E2"]], [], {})


class _Capture:
    name = "capture"

    def __init__(self) -> None:
        self.reqs: list = []

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req):
        self.reqs.append(req)
        return {"findings": []}


# ── sidecar.prior_concerns: filtering + best-effort ──────────────────────────────────────
def test_prior_concerns_filters_by_decision_and_priority(monkeypatch) -> None:
    payload = {
        "findings": [
            {
                "finding": "block hi",
                "criteria": ["E2"],
                "decision": "block",
                "priority": 0.9,
                "norm_id": "a",
            },
            {
                "finding": "adv near",
                "criteria": ["F1"],
                "decision": "advisory",
                "priority": 0.6,
                "norm_id": "b",
            },
            {
                "finding": "adv low",
                "criteria": ["E2"],
                "decision": "advisory",
                "priority": 0.2,
                "norm_id": "c",
            },
            {
                "finding": "dropped",
                "criteria": ["E2"],
                "decision": "dropped",
                "priority": 0.99,
                "norm_id": "d",
            },
        ]
    }
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: payload)
    got = sidecar.prior_concerns("t1")
    # only block/advisory with priority >= 0.5, highest first; low-priority + dropped excluded
    assert [c["norm_id"] for c in got] == ["a", "b"]


def test_prior_concerns_caps_at_recall_cap(monkeypatch) -> None:
    payload = {
        "findings": [
            {
                "finding": f"f{i}",
                "criteria": ["E2"],
                "decision": "block",
                "priority": 0.9,
                "norm_id": str(i),
            }
            for i in range(20)
        ]
    }
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: payload)
    assert len(sidecar.prior_concerns("t1")) == sidecar.RECALL_CAP


def test_prior_concerns_best_effort_on_reader_error(monkeypatch) -> None:
    def boom(tid, repo_root=None):
        raise RuntimeError("corrupt sidecar")

    monkeypatch.setattr(sidecar, "latest_review_result", boom)
    assert sidecar.prior_concerns("t1") == []  # never raises -> recall no-op


def test_prior_concerns_no_sidecar_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: None)
    assert sidecar.prior_concerns("t1") == []


# ── run_pass1 recall behavior ────────────────────────────────────────────────────────────
def test_missed_prior_is_recalled(monkeypatch, tmp_path) -> None:
    concern = {
        "finding": "the migration lacks a rollback path",
        "suggested_fix": "add a down-migration",
        "criteria": ["E2"],
        "location": "Scope",
        "norm_id": "n-missed",
    }
    monkeypatch.setattr(sidecar, "prior_concerns", lambda tid, repo_root=None: [concern])
    findings = _run(_ctx(tmp_path), [{"finding": "an unrelated fresh finding", "criteria": ["E2"]}])
    recalled = [f for f in findings if f.get("_recall")]
    assert len(recalled) == 1
    assert recalled[0]["finding"] == concern["finding"]
    assert recalled[0]["criteria"] == ["E2"]


def test_found_prior_is_not_double_surfaced(monkeypatch, tmp_path) -> None:
    fresh = {"finding": "the migration lacks a rollback path", "criteria": ["E2"]}
    # a prior concern whose norm_id equals the fresh finding's -> the fresh finder already found it
    concern = {"finding": fresh["finding"], "criteria": ["E2"], "norm_id": sidecar.norm_id(fresh)}
    monkeypatch.setattr(sidecar, "prior_concerns", lambda tid, repo_root=None: [concern])
    findings = _run(_ctx(tmp_path), [fresh])
    assert not [f for f in findings if f.get("_recall")]  # deduped by norm_id


def test_no_prior_concerns_is_noop(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sidecar, "prior_concerns", lambda tid, repo_root=None: [])
    findings = _run(_ctx(tmp_path), [{"finding": "fresh only", "criteria": ["E2"]}])
    assert not [f for f in findings if f.get("_recall")]


def test_finder_never_receives_prior_findings(monkeypatch, tmp_path) -> None:
    # Independence by construction: the recalled concern's text must appear in NO finder request
    # (system prompt or instructions) — it enters strictly AFTER Pass-1.
    secret = "PRIOR-ONLY-SENTINEL-rollback-path"
    concern = {"finding": secret, "criteria": ["E2"], "norm_id": "n-secret"}
    monkeypatch.setattr(sidecar, "prior_concerns", lambda tid, repo_root=None: [concern])
    cap = _Capture()
    findings = _run(_ctx(tmp_path), [], runner=cap)
    assert cap.reqs, "the Pass-1 finder still ran"
    for req in cap.reqs:
        assert secret not in (req.instructions or "")
        assert secret not in (req.system_prompt or "")
    # but the recall candidate IS present in the post-Pass-1 output
    assert any(f.get("_recall") and f["finding"] == secret for f in findings)


def test_recalled_candidate_dropped_by_pass2_when_resolved() -> None:
    # The FP backstop: a recalled candidate whose CURRENT-plan verification fails validity (< 0.5)
    # is DROPPED by Pass-3, never re-blocking on memory alone.
    resolved_verification = {
        "binary": {"is_verifiable": "no", "evidence_entails_finding": "no"},
        "severity_attributes": {},
    }
    d = pass3_decide(resolved_verification, block_threshold=0.6, blocking_enabled=True)
    assert d["decision"] == "dropped"
