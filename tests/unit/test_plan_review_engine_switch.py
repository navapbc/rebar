"""The plan-review engine switch (ADR 0123): ``[verify] plan_review_engine``.

Pins the resolution order (explicit argument > config key > ``"pipeline"``, no environment
override), the Layer-1-floor-first dispatch (a floor block or a not-claimable ticket returns
the same result under both engines with zero LLM calls), and that ``"agentic"`` on a
DET-passing plan is an unsigned INDETERMINATE naming the engine with zero LLM calls -- even
when a pipeline attestation or stored verdict would otherwise be reused or re-signed.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import rebar
import rebar.llm
from rebar import config as rconfig
from rebar.llm.plan_review import orchestrator, resolve_plan_review_engine
from rebar.llm.plan_review.workflow_ops import plan_review_engine_route
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit

_PASSING_DESC = (
    "A plan body that clears the deterministic readiness floor so the LLM tier runs.\n\n"
    "## What\nchange a thing in `src/thing.py`.\n\n"
    "## Why\nbecause the current behavior is wrong.\n\n"
    "## Acceptance Criteria\n"
    "- [ ] the thing is observably changed\n"
    "- [ ] `pytest tests/unit` proves the change\n"
)
# No `## Acceptance Criteria` checklist: the P1 readiness floor blocks before any LLM pass.
# Exactly ONE DET blocker (the named verification keeps P10 passing), so the pre-existing
# fix-unit grouping sub-call -- which runs only for >= 2 blocking findings -- stays out.
_FLOOR_BLOCKED_DESC = (
    "## What\nchange a thing in `src/thing.py`.\n\n## Why\nbecause.\n\n"
    "## Verification\n`pytest tests/unit` proves the change.\n"
)


class _CountingFake(FakeRunner):
    """Offline plan-review runner that counts every model call. ``block=True`` emits one
    verified F1 finding (a BLOCK); ``block=False`` emits none (a signed PASS)."""

    name = "fake"

    def __init__(self, *, block: bool) -> None:
        super().__init__()
        self.calls = 0
        self.schemas: list[str | None] = []
        self.block = block

    def run(self, req) -> dict:  # type: ignore[override]
        from rebar.llm import findings as _f
        from rebar.llm.review_kernel import decide as kdecide

        self.calls += 1
        self.schemas.append(req.output_schema)
        schema = req.output_schema
        instructions = req.instructions or ""
        if req.mode == "text":
            return {"text": "[fake summary]", "runner": self.name, "model": None, "trace_id": None}
        payload: dict = {"analysis": "", "findings": []}
        if schema == "plan_review_verification":
            idxs = [int(x) for x in re.findall(r"finding index (\d+)", instructions)]
            binary = {q: "yes" for q in kdecide.GRADED_BINARY}
            binary["cited_reference_accurate"] = "na"
            payload = {
                "verifications": [
                    {
                        "index": i,
                        "binary": binary,
                        "severity_attributes": {"vague_directive": "high"},
                    }
                    for i in idxs
                ]
            }
        elif schema == "plan_review_agentic_findings":
            payload = {"findings": []}
        elif schema == "plan_review_coach":
            payload = {"notes": []}
        elif schema == "plan_review_findings" and self.block:
            m = re.search(r"\(ids: ([^)]*)\)", instructions)
            ids = [x.strip() for x in (m.group(1).split(",") if m else [])]
            if "F1" in ids:
                payload["findings"] = [
                    {
                        "finding": "The stated change is too vague to execute as written.",
                        "criteria": ["F1"],
                        "location": "## What",
                        "evidence": ["change a thing in `src/thing.py`."],
                        "impact": "An executor cannot tell what done looks like.",
                        "suggested_fix": "Name the exact behavior change and its call sites.",
                    }
                ]
        payload = _f.validate_structured(dict(payload), schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    monkeypatch.delenv("REBAR_USAGE_LOG", raising=False)
    monkeypatch.delenv("REBAR_PLAN_REVIEW_BUDGET", raising=False)
    monkeypatch.delenv("REBAR_VERIFY_PLAN_REVIEW_ENGINE", raising=False)
    rebar.init_repo(repo_root=str(repo))
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _ticket(repo: Path, desc: str = _PASSING_DESC) -> str:
    nonce = f"\nMaterial nonce (test isolation): {uuid.uuid4().hex}.\n"
    return rebar.create_ticket("task", "engine plan", description=desc + nonce, repo_root=str(repo))


def _write_config(repo: Path, body: str) -> None:
    (repo / "rebar.toml").write_text(body)
    rconfig.reset_config_cache()


def _review(repo: Path, tid: str, runner, **kw):
    return rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), **kw)


def _assert_agentic_indeterminate(verdict: dict, runner, calls_before: int) -> None:
    """A DET-passing agentic review: exactly ONE agent call, no pipeline call, and (until the
    verdict adapter lands) an unsigned INDETERMINATE carrying the agent's findings."""
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["signature"]["signed"] is False
    assert runner.calls == calls_before + 1
    assert runner.schemas[calls_before:] == ["plan_review_agentic_findings"]
    assert verdict["coverage"]["engine"] == "agentic"
    assert [f["id"] for f in verdict["indeterminate"]] == ["agentic-adapter-pending"]
    assert verdict["coverage"].get("idempotent_skip") is not True
    assert verdict["coverage"].get("verdict_reuse") is not True


# ── resolution ──────────────────────────────────────────────────────────────────
def test_default_engine_is_pipeline(repo: Path) -> None:
    assert rconfig.compose_config(str(repo)).verify.plan_review_engine == "pipeline"
    assert resolve_plan_review_engine(None, str(repo)) == "pipeline"


def test_configured_engine_is_read_and_explicit_argument_wins(repo: Path) -> None:
    _write_config(repo, '[verify]\nplan_review_engine = "agentic"\n')
    assert resolve_plan_review_engine(None, str(repo)) == "agentic"
    assert resolve_plan_review_engine("pipeline", str(repo)) == "pipeline"


def test_environment_variable_is_never_read(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_VERIFY_PLAN_REVIEW_ENGINE", "agentic")
    assert rconfig.compose_config(str(repo)).verify.plan_review_engine == "pipeline"
    assert resolve_plan_review_engine(None, str(repo)) == "pipeline"
    from rebar._config_sources import env_overridable_keys, env_overrides

    assert ("verify", "plan_review_engine") not in env_overridable_keys()
    assert "plan_review_engine" not in env_overrides().get("verify", {})


def test_unknown_configured_value_fails_config_load(repo: Path) -> None:
    _write_config(repo, '[verify]\nplan_review_engine = "turbo"\n')
    with pytest.raises(rconfig.ConfigError, match="plan_review_engine"):
        rconfig.compose_config(str(repo))


def test_unknown_explicit_value_raises_before_any_llm_call(repo: Path) -> None:
    tid = _ticket(repo)
    runner = _CountingFake(block=False)
    with pytest.raises(rconfig.ConfigError, match="turbo"):
        _review(repo, tid, runner, engine="turbo")
    assert runner.calls == 0


def test_generated_surfaces_omit_the_env_name() -> None:
    root = Path(__file__).resolve().parents[2]
    assert "REBAR_VERIFY_PLAN_REVIEW_ENGINE" not in (root / "docs" / "env-vars.md").read_text()
    golden = json.loads((root / "tests" / "golden" / "config_surface.json").read_text())
    assert "REBAR_VERIFY_PLAN_REVIEW_ENGINE" not in golden["canonical_env_vars"]
    assert "verify.plan_review_engine" in golden["config_keys"]


# ── the route op ────────────────────────────────────────────────────────────────
def _route(run_llm: bool, engine: str) -> dict:
    return plan_review_engine_route(
        SimpleNamespace(inputs={"run_llm": run_llm, "engine": engine}, step_id="engine_route")
    )


def test_route_selects_one_arm_only_for_a_det_passing_review() -> None:
    assert _route(True, "pipeline") == {"run_pipeline": True, "run_agentic": False}
    assert _route(True, "agentic") == {"run_pipeline": False, "run_agentic": True}
    assert _route(False, "pipeline") == {"run_pipeline": False, "run_agentic": False}
    assert _route(False, "agentic") == {"run_pipeline": False, "run_agentic": False}


def test_route_rejects_an_unknown_engine_and_defaults_an_empty_one() -> None:
    with pytest.raises(ValueError, match="turbo"):
        _route(True, "turbo")
    assert _route(True, "") == {"run_pipeline": True, "run_agentic": False}


# ── floor first: identical results, zero LLM calls ─────────────────────────────
def _comparable(verdict: dict) -> tuple:
    return (
        verdict["verdict"],
        [f.get("criteria") for f in verdict.get("blocking") or []],
        [f.get("criteria") for f in verdict.get("advisory") or []],
        verdict["signature"]["signed"],
    )


def test_floor_blocked_review_is_identical_under_both_engines(repo: Path) -> None:
    tid = _ticket(repo, _FLOOR_BLOCKED_DESC)
    results = {}
    for engine in ("pipeline", "agentic"):
        runner = _CountingFake(block=False)
        results[engine] = _review(repo, tid, runner, engine=engine)
        assert runner.calls == 0
    assert results["pipeline"]["verdict"] == "BLOCK"
    assert _comparable(results["pipeline"]) == _comparable(results["agentic"])


def test_not_claimable_review_is_identical_under_both_engines(repo: Path) -> None:
    blocker = _ticket(repo)
    tid = _ticket(repo)
    rebar.link(tid, blocker, "depends_on", repo_root=str(repo))
    results = {}
    for engine in ("pipeline", "agentic"):
        runner = _CountingFake(block=False)
        results[engine] = _review(repo, tid, runner, engine=engine)
        assert runner.calls == 0
    assert results["pipeline"]["verdict"] == "INDETERMINATE"
    assert _comparable(results["pipeline"]) == _comparable(results["agentic"])


def test_det_passing_agentic_review_is_unsigned_indeterminate(repo: Path) -> None:
    tid = _ticket(repo)
    runner = _CountingFake(block=False)
    _assert_agentic_indeterminate(_review(repo, tid, runner, engine="agentic"), runner, 0)


def test_configured_agentic_engine_applies_without_an_argument(repo: Path) -> None:
    _write_config(repo, '[verify]\nplan_review_engine = "agentic"\n')
    tid = _ticket(repo)
    runner = _CountingFake(block=False)
    _assert_agentic_indeterminate(_review(repo, tid, runner), runner, 0)


# ── pipeline-only shortcuts ─────────────────────────────────────────────────────
def test_valid_pipeline_attestation_is_not_reused_under_agentic(repo: Path) -> None:
    tid = _ticket(repo)
    v1 = _review(repo, tid, _CountingFake(block=False))
    assert v1["verdict"] == "PASS" and v1["signature"]["signed"] is True

    runner = _CountingFake(block=False)
    _assert_agentic_indeterminate(_review(repo, tid, runner, engine="agentic"), runner, 0)
    reused = _review(repo, tid, runner, engine="pipeline")
    assert reused["coverage"].get("idempotent_skip") is True
    assert runner.calls == 1  # only the agent's call; the pipeline reuse made none


def test_stored_block_is_not_reused_under_agentic(repo: Path) -> None:
    tid = _ticket(repo)
    assert _review(repo, tid, _CountingFake(block=True))["verdict"] == "BLOCK"

    runner = _CountingFake(block=True)
    # Under pipeline the stored BLOCK is still reused (zero calls) ...
    reused = _review(repo, tid, runner, engine="pipeline")
    assert reused["coverage"].get("verdict_reuse") is True
    assert runner.calls == 0
    # ... and under agentic it is not: the agent runs (one call), no reused pipeline verdict.
    _assert_agentic_indeterminate(_review(repo, tid, runner, engine="agentic"), runner, 0)


def test_drift_refresh_runs_only_under_pipeline(repo: Path, monkeypatch) -> None:
    tid = _ticket(repo)
    assert _review(repo, tid, _CountingFake(block=False))["verdict"] == "PASS"
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "drift"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    probes: list[str] = []

    def _spy(ctx, cfg, **kw):
        probes.append(ctx.ticket_id)
        return None  # "not applicable" -> the caller proceeds to the full review

    monkeypatch.setattr(orchestrator, "drift_refresh", _spy)
    runner = _CountingFake(block=False)
    _assert_agentic_indeterminate(_review(repo, tid, runner, engine="agentic"), runner, 0)
    assert probes == []
    _review(repo, tid, runner, engine="pipeline")
    assert len(probes) == 1


# ── produce_plan_review_verdict defaults the workflow input ─────────────────────
def test_produce_plan_review_verdict_defaults_engine_to_pipeline(repo: Path, monkeypatch) -> None:
    from rebar.llm.plan_review import _verifier_cfg, context_assembly
    from rebar.llm.workflow import executor, gate_dispatch

    sig = inspect.signature(gate_dispatch.produce_plan_review_verdict)
    assert sig.parameters["engine"].default == "pipeline"

    captured: dict = {}

    class _Stop(Exception):
        pass

    def _capture(doc, inputs, **kw):
        captured.update(inputs)
        raise _Stop

    monkeypatch.setattr(executor, "run_workflow", _capture)
    tid = _ticket(repo)
    ctx = context_assembly.assemble_context(tid, repo_root=str(repo))
    from rebar.llm.config import LLMConfig

    with pytest.raises(_Stop):
        gate_dispatch.produce_plan_review_verdict(
            ctx,
            _verifier_cfg(LLMConfig()),
            runner=FakeRunner(),
            advisory_cap=orchestrator.DEFAULT_ADVISORY_CAP,
            repo_root=str(repo),
        )
    assert captured["engine"] == "pipeline"
