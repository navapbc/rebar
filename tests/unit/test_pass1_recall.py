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
def _prior(norm_id: str, *, decision: str = "block", priority: float = 0.9, **over) -> dict:
    """A prior sidecar finding shaped as `build_payload._slim` persists it. Grounding
    `evidence` is part of the eligibility contract, so the default carries one quote."""
    return {
        "finding": f"prior {norm_id}",
        "criteria": ["E2"],
        "decision": decision,
        "priority": priority,
        "evidence": [f"quote for {norm_id}"],
        "norm_id": norm_id,
        **over,
    }


def test_prior_concerns_filters_by_decision_and_priority(monkeypatch) -> None:
    payload = {
        "findings": [
            _prior("a", decision="block", priority=0.9),
            _prior("b", decision="advisory", priority=0.6, criteria=["F1"]),
            _prior("c", decision="advisory", priority=0.2),
            _prior("d", decision="dropped", priority=0.99),
        ]
    }
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: payload)
    got = sidecar.prior_concerns("t1")
    # only block/advisory with priority >= 0.5, highest first; low-priority + dropped excluded
    assert [c["norm_id"] for c in got] == ["a", "b"]


def test_prior_concerns_caps_at_recall_cap(monkeypatch) -> None:
    payload = {"findings": [_prior(str(i)) for i in range(20)]}
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: payload)
    assert len(sidecar.prior_concerns("t1")) == sidecar.RECALL_CAP


# ── the replay ratchet (bug deceitful-flannel-jerboa) ────────────────────────────────────
def test_ungrounded_prior_is_not_recalled(monkeypatch) -> None:
    """THE REGRESSION. A prior finding persisted with NO grounding evidence is exactly what a
    previous recall injection leaves behind (`run_pass1` injected `evidence: []`, `_slim`
    persisted it). Re-surfacing it gives Pass-2 nothing to re-ground against the current plan,
    and — because the replay is re-decided `block` and re-persisted — lets one finding block
    every later review forever (observed: 5 consecutive rounds on epic 0e68-41eb-5782-4336,
    the fresh finder having stopped emitting it once the plan was edited to address it)."""
    payload = {
        "findings": [
            _prior("replayed", evidence=[]),  # a previous recall injection
            _prior("grounded"),  # a genuine fresh finding
        ]
    }
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: payload)
    coverage: dict = {}
    got = sidecar.prior_concerns("t1", coverage=coverage)
    assert [c["norm_id"] for c in got] == ["grounded"]  # the replay cannot seed another replay
    assert coverage["recall_suppressed"] == "ungrounded-prior"


def test_recalled_concern_carries_its_grounding_evidence(monkeypatch, tmp_path) -> None:
    # Re-grounding, not restatement: the candidate reaching Pass-2 carries the prior finding's
    # own quotes, so `evidence_entails_finding` has something to test against the CURRENT plan.
    payload = {"findings": [_prior("g", evidence=["the plan never names a rollback path"])]}
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: payload)
    monkeypatch.setattr(sidecar, "_material_changed", lambda *a, **k: False)
    concern = sidecar.prior_concerns("t1")[0]
    assert concern["evidence"] == ["the plan never names a rollback path"]

    monkeypatch.setattr(sidecar, "prior_concerns", lambda tid, repo_root=None, **k: [concern])
    findings = _run(_ctx(tmp_path), [{"finding": "an unrelated fresh finding", "criteria": ["E2"]}])
    recalled = [f for f in findings if f.get("_recall")]
    assert recalled and recalled[0]["evidence"] == ["the plan never names a rollback path"]


def test_changed_material_suppresses_recall(monkeypatch) -> None:
    # Recall is a backstop for a finder that MISSED a finding on IDENTICAL material. Once the
    # material changed, the fresh finder's silence is evidence the edit resolved the finding.
    payload = {"findings": [_prior("a")], "material_fingerprint": "fp-old"}
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: payload)
    from rebar.llm.plan_review import attest

    monkeypatch.setattr(
        attest, "current_material_fingerprint", lambda tid, repo_root=None: "fp-new"
    )
    coverage: dict = {}
    assert sidecar.prior_concerns("t1", coverage=coverage) == []
    assert coverage["recall_suppressed"] == "material-changed"


def test_unchanged_material_still_recalls(monkeypatch) -> None:
    payload = {"findings": [_prior("a")], "material_fingerprint": "fp-same"}
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: payload)
    from rebar.llm.plan_review import attest

    monkeypatch.setattr(
        attest, "current_material_fingerprint", lambda tid, repo_root=None: "fp-same"
    )
    assert [c["norm_id"] for c in sidecar.prior_concerns("t1")] == ["a"]


def test_unresolvable_fingerprint_leaves_recall_enabled(monkeypatch) -> None:
    # Fail-open: a degraded fingerprint read must not silently disable the backstop.
    payload = {"findings": [_prior("a")], "material_fingerprint": "fp-old"}
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: payload)
    from rebar.llm.plan_review import attest

    def boom(tid, repo_root=None):
        raise RuntimeError("no store")

    monkeypatch.setattr(attest, "current_material_fingerprint", boom)
    assert [c["norm_id"] for c in sidecar.prior_concerns("t1")] == ["a"]


def test_missing_prior_fingerprint_leaves_recall_enabled(monkeypatch) -> None:
    payload = {"findings": [_prior("a")]}  # an older sidecar with no material stamp
    monkeypatch.setattr(sidecar, "latest_review_result", lambda tid, repo_root=None: payload)
    assert [c["norm_id"] for c in sidecar.prior_concerns("t1")] == ["a"]


def test_slim_persists_recall_provenance() -> None:
    # The replay chain must be visible offline instead of inferable only from empty evidence.
    verdict = {
        "blocking": [
            {"finding": "recalled one", "criteria": ["E2"], "evidence": [], "_recall": True},
            {"finding": "fresh one", "criteria": ["E2"], "evidence": ["a quote"]},
        ]
    }
    payload = sidecar.build_payload(verdict)
    assert [f["recalled"] for f in payload["findings"]] == [True, False]


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
    monkeypatch.setattr(sidecar, "prior_concerns", lambda tid, repo_root=None, **_: [concern])
    findings = _run(_ctx(tmp_path), [{"finding": "an unrelated fresh finding", "criteria": ["E2"]}])
    recalled = [f for f in findings if f.get("_recall")]
    assert len(recalled) == 1
    assert recalled[0]["finding"] == concern["finding"]
    assert recalled[0]["criteria"] == ["E2"]


def test_found_prior_is_not_double_surfaced(monkeypatch, tmp_path) -> None:
    fresh = {"finding": "the migration lacks a rollback path", "criteria": ["E2"]}
    # a prior concern whose norm_id equals the fresh finding's -> the fresh finder already found it
    concern = {"finding": fresh["finding"], "criteria": ["E2"], "norm_id": sidecar.norm_id(fresh)}
    monkeypatch.setattr(sidecar, "prior_concerns", lambda tid, repo_root=None, **_: [concern])
    findings = _run(_ctx(tmp_path), [fresh])
    assert not [f for f in findings if f.get("_recall")]  # deduped by norm_id


def test_no_prior_concerns_is_noop(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sidecar, "prior_concerns", lambda tid, repo_root=None, **_: [])
    findings = _run(_ctx(tmp_path), [{"finding": "fresh only", "criteria": ["E2"]}])
    assert not [f for f in findings if f.get("_recall")]


def test_finder_never_receives_prior_findings(monkeypatch, tmp_path) -> None:
    # Independence by construction: the recalled concern's text must appear in NO finder request
    # (system prompt or instructions) — it enters strictly AFTER Pass-1.
    secret = "PRIOR-ONLY-SENTINEL-rollback-path"
    concern = {"finding": secret, "criteria": ["E2"], "norm_id": "n-secret"}
    monkeypatch.setattr(sidecar, "prior_concerns", lambda tid, repo_root=None, **_: [concern])
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
