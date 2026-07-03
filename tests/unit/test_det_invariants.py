"""Project DET-invariant scan + per-criterion fail_mode (story 7f0d).

Pins the generalization of the DET-invariant consumer:

* the code-review consumer is DATA-DRIVEN (``run_detectors`` ≡ its ``run_security_detectors``
  alias) and honors a per-criterion ``fail_mode`` (open records coverage; closed blocks on an
  abstain);
* plan-review learns an ``exec: "DET"`` descriptor branch (prompt-less), keeps DET criteria OUT of
  the LLM batch, and runs a dynamic project-DET phase after the static P1–P9 floor (empty for a
  repo with no activated DET criterion; a file_impact-scoped match → a blocking DetResult).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm.prompting import prompt_library

pytestmark = pytest.mark.unit


# ── overlay fixture (mirrors tests/unit/test_criteria_overlay.py) ────────────────
_DET_ROUTING = {
    "exec": "DET",
    "facet": "project-invariants",
    "applies_at": {"scope": ["container", "leaf"]},
    "block_threshold": 0.5,
    "default_posture": "blocking",
    "fail_mode": "closed",
    "detector": {"id": "project.no-eval"},
    "name": "No eval() in library code",
}


def _make_repo(tmp_path: Path, *, overlay: dict | None) -> str:
    root = tmp_path
    if overlay is not None:
        rebar_dir = root / ".rebar"
        rebar_dir.mkdir(parents=True, exist_ok=True)
        (rebar_dir / "criteria_routing.json").write_text(json.dumps(overlay), encoding="utf-8")
    return str(root)


@pytest.fixture(autouse=True)
def _clear_caches():
    prompt_library._invalidate_caches()
    yield
    prompt_library._invalidate_caches()


def _ctx(repo_root, *, file_impact=None):
    from rebar.llm.plan_review.det_floor import PlanContext

    state = {"file_impact": file_impact or []}
    return PlanContext(
        ticket_id="abcd-0000-0000-0001",
        ticket_type="task",
        title="T",
        description="## Acceptance Criteria\n- [ ] does a thing\n",
        state=state,
        repo_root=repo_root,
    )


# ── (a) run_detectors / run_security_detectors alias equivalence ─────────────────
def test_run_detectors_alias_equivalence(monkeypatch):
    from rebar.llm.code_review import detectors

    class _Res:
        records = (
            {
                "detector_id": "rebar.builtin.security.python-eval-exec-injection",
                "outcome": "match",
                "location": {"file": "app.py"},
            },
            {
                "detector_id": "rebar.builtin.security.secrets-gitleaks",
                "outcome": "abstain",
                "reason": "no_tool",
            },
        )

    monkeypatch.setattr("rebar.grounding.engine_b.scan", lambda *a, **k: _Res())
    a = detectors.run_detectors(changed_files=["app.py"], repo_root=None)
    b = detectors.run_security_detectors(changed_files=["app.py"], repo_root=None)
    assert a == b
    # And it routed by the data-driven selector: gitleaks → secret-detection (exact id wins),
    # the eval rule → high-critical-security (prefix class).
    assert a["high-critical-security"]["matches"][0]["location"]["file"] == "app.py"
    assert a["secret-detection"]["abstained"][0]["reason"] == "no_tool"


# ── (b) per-criterion fail_mode: open does NOT block on abstain, closed DOES ──────
def _pass_verdict() -> dict:
    return {"verdict": "PASS", "blocking": [], "advisory": [], "coaching": [], "coverage": {}}


def test_fail_mode_open_does_not_block_on_abstain(monkeypatch):
    from rebar.llm.code_review import detectors, registry

    monkeypatch.setattr(
        registry,
        "det_criteria",
        lambda: {"project.inv": {"detector": {"id": "x"}, "fail_mode": "open"}},
    )
    monkeypatch.setattr(registry, "threshold_for", lambda crits: (0.5, True))
    monkeypatch.setattr(
        detectors,
        "run_security_detectors",
        lambda **kw: {"project.inv": {"abstained": [{"reason": "no_tool"}], "matches": []}},
    )
    v = detectors.apply_failclosed(_pass_verdict(), changed_files=["app.py"], repo_root=None)
    assert v["verdict"] == "PASS"  # fail-OPEN: coverage recorded, no block on absence
    note = v["coverage"]["security_detectors"][0]
    assert note["reason"] == "fail-open-abstain" and note["blocking"] is False


def test_fail_mode_closed_blocks_on_abstain(monkeypatch):
    from rebar.llm.code_review import detectors, registry

    monkeypatch.setattr(
        registry,
        "det_criteria",
        lambda: {"project.inv": {"detector": {"id": "x"}, "fail_mode": "closed"}},
    )
    monkeypatch.setattr(registry, "threshold_for", lambda crits: (0.5, True))
    monkeypatch.setattr(
        detectors,
        "run_security_detectors",
        lambda **kw: {"project.inv": {"abstained": [{"reason": "no_tool"}], "matches": []}},
    )
    v = detectors.apply_failclosed(_pass_verdict(), changed_files=["app.py"], repo_root=None)
    assert v["verdict"] == "BLOCK"  # fail-CLOSED: unestablished coverage blocks
    note = v["coverage"]["security_detectors"][0]
    assert note["reason"] == "fail-closed-abstain" and note["blocking"] is True


# ── (c) the exec:DET descriptor branch builds a prompt-less descriptor ───────────
def test_det_descriptor_branch_needs_no_prompt(tmp_path):
    from rebar.llm.plan_review import registry

    root = _make_repo(
        tmp_path,
        overlay={
            "plan_review": {"project.no-eval": _DET_ROUTING},
            "activate": ["project.no-eval"],
        },
    )
    # NO .rebar/prompts/plan-review-project-no-eval.md file exists — load_criteria must NOT blow up.
    descriptors = registry.by_id(root)
    assert "project.no-eval" in descriptors
    d = descriptors["project.no-eval"]
    assert d["exec"] == "DET"
    assert d["fail_mode"] == "closed"
    assert d["scenario"]  # falls back to the criterion name when the detector is unresolvable
    assert d["detector"] == {"id": "project.no-eval"}


# ── (d) a DET criterion never reaches the LLM batch ──────────────────────────────
def test_det_criterion_absent_from_llm_batch(tmp_path, monkeypatch):
    from rebar.llm.plan_review import orchestrator, registry, workflow_ops

    root = _make_repo(
        tmp_path,
        overlay={
            "plan_review": {"project.no-eval": _DET_ROUTING},
            "activate": ["project.no-eval"],
        },
    )
    ctx = _ctx(root)
    single, agent = orchestrator.route_criteria(ctx)
    ids = {c["id"] for c in single + agent}
    assert "project.no-eval" not in ids  # NOT routed to any LLM tier

    # And absent from the assemble op's include_ vocabulary too.
    import rebar

    monkeypatch.setattr(
        rebar,
        "show_ticket",
        lambda tid, **k: {
            "ticket_id": ctx.ticket_id,
            "ticket_type": "task",
            "title": "T",
            "description": ctx.description,
            "file_impact": [],
        },
    )
    monkeypatch.setattr(rebar, "list_tickets", lambda **k: [])

    class _SC:
        inputs = {"target_ticket": ctx.ticket_id, "ticket_id": ctx.ticket_id}
        repo_root = root

    out = workflow_ops.plan_review_assemble_criteria(_SC())
    assert "include_project_no_eval" not in out
    # sanity: effective_routing saw it as DET (so the exclusion, not absence, dropped it)
    assert registry.effective_routing(root)["project.no-eval"]["exec"] == "DET"


# ── (e) no project DET criterion ⇒ the static floor is byte-identical (zero added) ─
def test_run_det_floor_adds_zero_without_project_criterion(tmp_path):
    from rebar.llm.plan_review.det_floor import DET_CHECKS, run_det_floor

    root = _make_repo(tmp_path, overlay=None)  # no overlay
    results = run_det_floor(_ctx(root))
    assert len(results) == len(DET_CHECKS)  # exactly P1–P9, nothing appended
    assert [r.id for r in results] == ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]


# ── (f) a project DET match on a declared file yields a blocking DetResult ────────
def test_project_det_match_on_file_impact_blocks(tmp_path, monkeypatch):
    from rebar.grounding.detectors import Detector, Registry
    from rebar.llm.plan_review import det_invariants

    root = _make_repo(
        tmp_path,
        overlay={
            "plan_review": {"project.no-eval": _DET_ROUTING},
            "activate": ["project.no-eval"],
        },
    )
    fake_det = Detector(
        id="project.no-eval",
        backend="astgrep",
        namespace="project",
        source_path="x",
        rule={"message": "no eval() in library code"},
    )
    monkeypatch.setattr(
        det_invariants,
        "_matching_detectors",
        lambda selector, rr: Registry(detectors=(fake_det,)),
    )

    class _Res:
        records = (
            {
                "detector_id": "project.no-eval",
                "outcome": "match",
                "location": {"file": "src/app.py"},
            },
        )

    monkeypatch.setattr("rebar.grounding.engine_b.scan", lambda *a, **k: _Res())

    ctx = _ctx(root, file_impact=[{"path": "src/app.py", "reason": "impl"}])
    results = det_invariants.run_project_det_checks(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.id == "project.no-eval" and r.status == "fail" and r.blocking is True
    assert r.blocked  # blocking fail
    assert "eval" in r.finding["finding"]


def test_project_det_match_off_file_impact_is_advisory(tmp_path, monkeypatch):
    from rebar.grounding.detectors import Detector, Registry
    from rebar.llm.plan_review import det_invariants

    root = _make_repo(
        tmp_path,
        overlay={
            "plan_review": {"project.no-eval": _DET_ROUTING},
            "activate": ["project.no-eval"],
        },
    )
    fake_det = Detector(
        id="project.no-eval",
        backend="astgrep",
        namespace="project",
        source_path="x",
        rule={"message": "no eval()"},
    )
    monkeypatch.setattr(
        det_invariants,
        "_matching_detectors",
        lambda selector, rr: Registry(detectors=(fake_det,)),
    )

    class _Res:
        records = (
            {
                "detector_id": "project.no-eval",
                "outcome": "match",
                "location": {"file": "other.py"},
            },  # NOT in file_impact
        )

    monkeypatch.setattr("rebar.grounding.engine_b.scan", lambda *a, **k: _Res())
    ctx = _ctx(root, file_impact=[{"path": "src/app.py"}])
    r = det_invariants.run_project_det_checks(ctx)[0]
    assert r.status == "fail" and r.blocking is False  # advisory, not blocking
