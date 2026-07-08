"""Live criterion preview + atomic routing-overlay authoring (story 6e31).

Exercises :mod:`rebar.llm.workflow.criterion_preview`:

* the LLM preview (existing ``criterion_id`` via the ``eval_solver.run_case`` arm, AND an
  unsaved ``inline`` criterion via an ad-hoc ``pass1_chunk``) mapping non-empty findings →
  ``fire``;
* the DET preview mapping a detector ``match`` → ``fire`` and an ``abstain`` → ``no-fire``
  (reporting the coverage gap per ``fail_mode``), with a monkeypatched scan;
* container/ISF → :class:`PreviewError`;
* the timeout falling back to a no-fire ``timed_out`` verdict (never blocking);
* :func:`write_criterion_overlay` / :func:`author_criterion_overlay` making a project
  criterion active in a single atomic write;
* the editor wiring (``/criterion/preview`` in the guarded POST set + the response shim).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from rebar.grounding.detectors import Detector, Registry
from rebar.grounding.engine_b import ScanResult
from rebar.llm.plan_review import det_invariants, registry
from rebar.llm.prompting import prompt_library
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow.criterion_preview import (
    PreviewError,
    author_criterion_overlay,
    handle_preview_post,
    poll_job,
    preview_criterion,
    preview_or_job,
    write_criterion_overlay,
)

# The project rubric + routing reused from the overlay tests.
_RUBRIC = """\
---
schema_version: 1
title: No bare print() in library code
description: Project invariant — library code must not call print().
execution_mode: single_turn
category: plan-review-criterion
dimension: project-invariants
---
Flag any plan that introduces a bare print() call in importable library code.
"""

_LLM_ROUTING = {
    "exec": "1-TURN",
    "facet": "project-invariants",
    "applies_at": {"scope": ["container", "leaf"]},
    "block_threshold": 0.9,
    "default_posture": "advisory",
    "checklist": [],
}


@pytest.fixture(autouse=True)
def _clear_caches():
    prompt_library._invalidate_caches()
    yield
    prompt_library._invalidate_caches()


def _make_repo(
    tmp_path: Path, *, overlay: dict | None, prompts: dict[str, str] | None = None
) -> str:
    if overlay is not None:
        d = tmp_path / ".rebar"
        d.mkdir(parents=True, exist_ok=True)
        (d / "criteria_routing.json").write_text(json.dumps(overlay), encoding="utf-8")
    for pid, body in (prompts or {}).items():
        pd = tmp_path / ".rebar" / "prompts"
        pd.mkdir(parents=True, exist_ok=True)
        (pd / f"{pid}.md").write_text(body, encoding="utf-8")
    return str(tmp_path)


def _fake(findings: list[dict]) -> FakeRunner:
    return FakeRunner(structured={"analysis": "", "findings": findings})


# ── LLM preview: existing criterion (run_case arm) ───────────────────────────────
def test_llm_preview_fires_for_existing_criterion(tmp_path):
    root = _make_repo(
        tmp_path,
        overlay={
            "plan_review": {"project.no-print": _LLM_ROUTING},
            "activate": ["project.no-print"],
        },
        prompts={"plan-review-project-no-print": _RUBRIC},
    )
    fake = _fake([{"finding": "bare print()", "criteria": ["project.no-print"]}])
    out = preview_criterion(
        {"criterion_id": "project.no-print", "fixture": {"input": "add print() to lib.py"}},
        repo_root=root,
        runner=fake,
    )
    assert out["verdict"] == "fire"
    assert out["finding"]["criteria"] == ["project.no-print"]


def test_llm_preview_no_fire_when_findings_empty(tmp_path):
    root = _make_repo(
        tmp_path,
        overlay={
            "plan_review": {"project.no-print": _LLM_ROUTING},
            "activate": ["project.no-print"],
        },
        prompts={"plan-review-project-no-print": _RUBRIC},
    )
    out = preview_criterion(
        {"criterion_id": "project.no-print", "fixture": {"input": "clean plan, uses logging"}},
        repo_root=root,
        runner=_fake([]),
    )
    assert out["verdict"] == "no-fire"
    assert out["finding"] is None


# ── LLM preview: unsaved inline criterion (ad-hoc pass1_chunk) ────────────────────
def test_llm_preview_inline_unsaved_criterion_fires(tmp_path):
    fake = _fake([{"finding": "matches the draft rubric", "criteria": ["preview"]}])
    out = preview_criterion(
        {
            "inline": {
                "id": "preview",
                "prompt": "flag anything scary",
                "routing": {"exec": "1-TURN"},
            },
            "fixture": {"input": "a scary plan"},
        },
        repo_root=str(tmp_path),
        runner=fake,
    )
    assert out["verdict"] == "fire"
    assert out["finding"]["finding"] == "matches the draft rubric"


# ── DET preview: match → fire, abstain → no-fire (monkeypatched scan) ─────────────
def _det_request(fail_mode: str = "open") -> dict:
    return {
        "inline": {"routing": {"exec": "DET", "detector": {"id": "x"}, "fail_mode": fail_mode}},
        "fixture": {"input": "eval(user_input)"},
    }


def _one_detector_slice() -> Registry:
    det = Detector(
        id="rebar.builtin.security.eval",
        backend="opengrep",
        namespace="rebar.builtin",
        source_path="",
        rule={"languages": ["python"], "message": "no eval() in library code"},
        envelope={},
    )
    return Registry(detectors=(det,))


def test_det_preview_match_fires(monkeypatch):
    monkeypatch.setattr(
        det_invariants, "_matching_detectors", lambda sel, rr: _one_detector_slice()
    )
    rec = {"outcome": "match", "location": {"file": "preview.py"}, "message": "no eval() in lib"}
    monkeypatch.setattr(
        "rebar.grounding.engine_b.scan", lambda root, *, registry=None: ScanResult(records=(rec,))
    )
    out = preview_criterion(_det_request(), repo_root=None, runner=None)
    assert out["verdict"] == "fire"
    assert out["finding"]["tier"] == "DET"
    assert out["finding"]["location"] == "preview.py"


def test_det_preview_clean_does_not_fire(monkeypatch):
    monkeypatch.setattr(
        det_invariants, "_matching_detectors", lambda sel, rr: _one_detector_slice()
    )
    monkeypatch.setattr(
        "rebar.grounding.engine_b.scan", lambda root, *, registry=None: ScanResult(records=())
    )
    out = preview_criterion(_det_request(), repo_root=None, runner=None)
    assert out["verdict"] == "no-fire"
    assert out["finding"] is None


def test_det_preview_abstain_reports_fail_mode(monkeypatch):
    monkeypatch.setattr(
        det_invariants, "_matching_detectors", lambda sel, rr: _one_detector_slice()
    )
    rec = {"outcome": "abstain", "reason": "tool_unavailable"}
    monkeypatch.setattr(
        "rebar.grounding.engine_b.scan", lambda root, *, registry=None: ScanResult(records=(rec,))
    )
    out = preview_criterion(_det_request(fail_mode="closed"), repo_root=None, runner=None)
    assert out["verdict"] == "no-fire"
    assert "would block" in out["rationale"]


def test_det_preview_no_matching_detector_reports_coverage_gap(monkeypatch):
    monkeypatch.setattr(
        det_invariants, "_matching_detectors", lambda sel, rr: Registry(detectors=())
    )
    out = preview_criterion(_det_request(fail_mode="closed"), repo_root=None, runner=None)
    assert out["verdict"] == "no-fire"
    assert "coverage gap" in out["rationale"]


# ── container / ISF → PreviewError ───────────────────────────────────────────────
@pytest.mark.parametrize("cid", ["G3", "G4", "ISF"])
def test_container_isf_not_previewable(cid):
    with pytest.raises(PreviewError, match="not previewable inline"):
        preview_criterion({"criterion_id": cid, "fixture": {"input": "x"}}, repo_root=None)


def test_unknown_criterion_raises(tmp_path):
    with pytest.raises(PreviewError, match="unknown criterion"):
        preview_criterion(
            {"criterion_id": "project.nope", "fixture": {"input": "x"}}, repo_root=str(tmp_path)
        )


def test_neither_id_nor_inline_raises():
    with pytest.raises(PreviewError, match="criterion_id.*inline"):
        preview_criterion({"fixture": {"input": "x"}}, repo_root=None)


# ── timeout: no-fire + timed_out, never blocks ───────────────────────────────────
class _SlowRunner:
    name = "slow"

    def run(self, req):  # noqa: ANN001, ANN201
        time.sleep(1.0)
        return {"findings": []}


def test_preview_times_out(tmp_path):
    out = preview_criterion(
        {
            "inline": {"id": "preview", "prompt": "slow", "routing": {"exec": "1-TURN"}},
            "fixture": {"input": "x"},
        },
        repo_root=str(tmp_path),
        runner=_SlowRunner(),
        timeout=0.2,
    )
    assert out["verdict"] == "no-fire"
    assert out["timed_out"] is True
    assert "timed out" in out["rationale"]


# ── atomic overlay authoring round-trip ──────────────────────────────────────────
def test_write_criterion_overlay_activates(tmp_path):
    root = _make_repo(tmp_path, overlay=None, prompts={"plan-review-project-foo": _RUBRIC})
    write_criterion_overlay(root, "project.foo", _LLM_ROUTING)
    prompt_library._invalidate_caches()
    # active = both routed AND in `activate`, written in one file
    assert "project.foo" in registry.effective_criteria(root)
    assert registry.effective_routing(root)["project.foo"]["block_threshold"] == 0.9
    data = json.loads((tmp_path / ".rebar" / "criteria_routing.json").read_text())
    assert data["plan_review"]["project.foo"] == _LLM_ROUTING
    assert "project.foo" in data["activate"]


def test_write_criterion_overlay_merges_into_existing(tmp_path):
    root = _make_repo(
        tmp_path,
        overlay={"plan_review": {"project.a": _LLM_ROUTING}, "activate": ["project.a"]},
        prompts={"plan-review-project-a": _RUBRIC, "plan-review-project-b": _RUBRIC},
    )
    write_criterion_overlay(root, "project.b", _LLM_ROUTING)
    prompt_library._invalidate_caches()
    eff = set(registry.effective_criteria(root))
    assert {"project.a", "project.b"} <= eff  # the first entry survives the read-modify-write


def test_author_criterion_overlay_keys_by_dotted_id(tmp_path):
    # author_criterion_overlay takes the DOTTED criterion id directly (task stew-kid-motif); the
    # rubric lives at the sanitized prompt id `criterion_prompt_id('project.bar')`.
    root = _make_repo(tmp_path, overlay=None, prompts={"plan-review-project-bar": _RUBRIC})
    author_criterion_overlay(root, "project.bar", _LLM_ROUTING)
    data = json.loads((tmp_path / ".rebar" / "criteria_routing.json").read_text())
    assert "project.bar" in data["plan_review"]  # keyed by the dotted logical id
    assert "project.bar" in registry.effective_criteria(root)


def test_netnew_project_criterion_round_trips_via_library_create(tmp_path):
    """THE editor-UX bug this task fixes: a net-new `project.<name>` criterion authored through the
    `/library/create` handler's authoring path (`author_criterion` — exactly what
    `editor._library_create` invokes for a `kind == "criterion"` POST) round-trips: the rubric
    lands at the sanitized `.rebar/prompts/plan-review-project-<name>.md`, and the gate then loads
    + resolves it. Previously `create_prompt` rejected the dotted `plan-review-project.<name>`
    (`_valid_id` forbids '.'), dead-ending the UX at a 4xx."""
    from rebar.llm.workflow.criterion_preview import author_criterion

    root = str(tmp_path)
    cid = "project.no-print"

    # Drive the criterion-authoring path `/library/create` runs (JSON parse → author_criterion):
    meta = {"title": "No bare print", "description": "no print in library code"}
    body = "Flag any bare print() in importable library code (use logging)."
    path = author_criterion(root, cid, meta, body, _LLM_ROUTING)
    # 1) the rubric landed at the SANITIZED prompt filename (dotted id decoupled from filesystem)
    assert path.name == "plan-review-project-no-print.md"
    # 2) the gate now sees it (activated by the same atomic write) and resolves its rubric
    assert cid in registry.effective_criteria(root)
    descs = {c["id"]: c for c in registry.load_criteria(root)}
    assert cid in descs and descs[cid]["scenario"]  # rubric body resolved from the sanitized file


def test_builtin_prompt_id_unchanged(tmp_path):
    """Back-compat sanity (not required, but asserted): a built-in id maps to plan-review-<id>."""
    from rebar.llm.criteria.ids import criterion_prompt_id

    assert criterion_prompt_id("F1") == "plan-review-F1"
    assert criterion_prompt_id("T5a") == "plan-review-T5a"


def test_author_criterion_overlay_rolls_back_invalid_netnew(tmp_path):
    """A net-new id that is NOT `project.`-prefixed is invalid (ef7e); author_criterion_overlay
    must roll the overlay back rather than persist a load-breaking file."""
    root = _make_repo(tmp_path, overlay=None, prompts={"plan-review-myrule": _RUBRIC})
    with pytest.raises(registry.RegistryError):
        author_criterion_overlay(root, "myrule", _LLM_ROUTING)
    # no overlay file was left behind (created-then-removed) → the repo stays packaged-only
    assert not (tmp_path / ".rebar" / "criteria_routing.json").is_file()
    assert set(registry.effective_criteria(root)) == set(registry.CANONICAL_LLM)


def test_author_criterion_overlay_rollback_preserves_prior(tmp_path):
    """When a PRIOR valid overlay exists, a subsequent invalid authoring attempt restores it
    exactly (the prior project criterion stays active)."""
    root = _make_repo(
        tmp_path,
        overlay={"plan_review": {"project.ok": _LLM_ROUTING}, "activate": ["project.ok"]},
        prompts={"plan-review-project-ok": _RUBRIC, "plan-review-bad": _RUBRIC},
    )
    prompt_library._invalidate_caches()
    with pytest.raises(registry.RegistryError):
        author_criterion_overlay(root, "bad", _LLM_ROUTING)
    # the prior overlay is intact; the bad id was not added
    data = json.loads((tmp_path / ".rebar" / "criteria_routing.json").read_text())
    assert "project.ok" in data["plan_review"] and "bad" not in data["plan_review"]
    assert "project.ok" in registry.effective_criteria(root)


# ── editor wiring ────────────────────────────────────────────────────────────────
def test_criterion_preview_in_guarded_post_set():
    from rebar.llm.workflow import editor

    assert "/criterion/preview" in editor._POST_WRITE_PATHS
    # the async poll path is guarded too, and both preview paths are grouped
    assert "/criterion/preview/status" in editor._POST_WRITE_PATHS
    assert set(editor._PREVIEW_PATHS) == {"/criterion/preview", "/criterion/preview/status"}


# ── GAP 2: spike-gate async job + poll ───────────────────────────────────────────
def test_preview_or_job_fast_is_sync(tmp_path):
    """A preview that finishes within the timeout returns the verdict inline (HTTP 200)."""
    fake = _fake([{"finding": "matches", "criteria": ["preview"]}])
    code, body = preview_or_job(
        {
            "inline": {"id": "preview", "prompt": "flag it", "routing": {"exec": "1-TURN"}},
            "fixture": {"input": "a plan"},
        },
        repo_root=str(tmp_path),
        runner=fake,
        timeout=10.0,
    )
    assert code == 200
    assert body["verdict"] == "fire"
    assert "job_id" not in body


def test_preview_or_job_slow_returns_job_then_polls_to_result(tmp_path):
    """A preview that exceeds the timeout returns a 202 pending job_id; polling eventually
    yields the completed result (the background thread continued past the sync budget)."""

    class _SlowFire:
        name = "slow-fire"

        def run(self, req):  # noqa: ANN001, ANN201
            time.sleep(0.5)
            from rebar.llm import findings as _f

            return _f.validate_structured(
                {"analysis": "", "findings": [{"finding": "late", "criteria": ["preview"]}]},
                req.output_schema,
            )

    code, body = preview_or_job(
        {
            "inline": {"id": "preview", "prompt": "slow", "routing": {"exec": "1-TURN"}},
            "fixture": {"input": "x"},
        },
        repo_root=str(tmp_path),
        runner=_SlowFire(),
        timeout=0.1,
    )
    assert code == 202
    assert body["status"] == "pending"
    job_id = body["job_id"]
    assert job_id

    # poll until the background job finishes
    result = None
    for _ in range(50):
        pcode, pbody = poll_job(job_id)
        assert pcode == 200
        if pbody["status"] == "done":
            result = pbody
            break
        assert pbody["status"] == "pending"
        time.sleep(0.05)
    assert result is not None, "job never completed"
    assert result["result"]["verdict"] == "fire"
    # the job is popped once collected → a second poll is 404 (unknown/already-collected)
    assert poll_job(job_id)[0] == 404


def test_poll_unknown_job_is_404():
    code, body = poll_job("deadbeefdeadbeef")
    assert code == 404
    assert "unknown job" in body["error"]


def test_poll_missing_job_id_is_400():
    code, body = poll_job("")
    assert code == 400


def test_preview_or_job_error_is_400(tmp_path):
    """A PreviewError surfaced by the background worker within the timeout maps to a 400."""
    code, body = preview_or_job(
        {"criterion_id": "G3", "fixture": {"input": "x"}}, repo_root=None, timeout=10.0
    )
    assert code == 400
    assert "not previewable" in body["error"]


def test_handle_preview_post_routes_status_and_preview(tmp_path):
    """handle_preview_post routes …/status → poll and the base path → preview_or_job."""
    # base path: a container criterion → 400 (goes through preview_or_job)
    code, body = handle_preview_post(
        "/criterion/preview",
        json.dumps({"criterion_id": "G4", "fixture": {"input": "x"}}).encode(),
        repo_root=None,
    )
    assert code == 400 and "not previewable" in body["error"]
    # status path: an unknown job id → 404 (goes through poll_job)
    code, body = handle_preview_post(
        "/criterion/preview/status", json.dumps({"job_id": "nope"}).encode(), repo_root=None
    )
    assert code == 404
    # bad JSON → 400
    assert handle_preview_post("/criterion/preview", b"{bad", repo_root=None)[0] == 400


def test_default_timeout_env_override(monkeypatch):
    from rebar.llm.workflow import criterion_preview as cp

    monkeypatch.setenv("REBAR_PREVIEW_TIMEOUT", "3.5")
    assert cp._default_timeout() == 3.5
    monkeypatch.setenv("REBAR_PREVIEW_TIMEOUT", "not-a-number")
    assert cp._default_timeout() == cp._DEFAULT_TIMEOUT
    monkeypatch.delenv("REBAR_PREVIEW_TIMEOUT", raising=False)
    assert cp._default_timeout() == cp._DEFAULT_TIMEOUT


# ── GAP 3: DET preview REALLY materializes the fixture before scanning ────────────
def test_det_preview_materializes_fixture_to_disk(monkeypatch):
    """Monkeypatch ONLY engine_b.scan (NOT _matching_detectors, which runs for real over the
    built-in registry) with a fake that INSPECTS its repo_root arg — proving _preview_det's
    tempdir+git-init+write chain actually wrote the fixture file to disk before the scan."""
    captured: dict = {}

    def fake_scan(root, *, registry=None):
        p = Path(root, "preview.py")
        captured["exists"] = p.is_file()
        captured["content"] = p.read_text(encoding="utf-8") if p.is_file() else None
        captured["registry_nonempty"] = bool(registry and registry.detectors)
        rec = {"outcome": "match", "location": {"file": "preview.py"}, "message": "no eval()"}
        return ScanResult(records=(rec,))

    monkeypatch.setattr("rebar.grounding.engine_b.scan", fake_scan)
    req = {
        "inline": {
            "routing": {
                "exec": "DET",
                # a real selector that matches built-in detectors (id_prefix class)
                "detector": {"id_prefix": "rebar.builtin"},
                "fail_mode": "open",
            }
        },
        "fixture": {"input": "eval(user_input)\n", "filename": "preview.py"},
    }
    out = preview_criterion(req, repo_root=None, runner=None)
    assert out["verdict"] == "fire"
    # the real materialization chain ran: the fixture file existed on disk when scan saw it
    assert captured["exists"] is True
    assert captured["content"] == "eval(user_input)\n"
    assert captured["registry_nonempty"] is True
