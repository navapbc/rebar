"""Both plan-review engines satisfy one contract (ADR 0123): their verdicts validate against
``plan_review_verdict.schema.json`` and their REVIEW_RESULT sidecars share the payload schema
id and top-level keys and read back through ``sidecar.latest_review_result``.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar.llm import findings as _findings
from rebar.llm.plan_review import sidecar
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit

_DESC = (
    "A plan body that clears the deterministic readiness floor so the LLM tier runs.\n\n"
    "## What\nchange a thing in `src/thing.py`.\n\n"
    "## Why\nbecause the current behavior is wrong.\n\n"
    "## Acceptance Criteria\n"
    "- [ ] the thing is observably changed\n"
    "- [ ] `pytest tests/unit` proves the change\n"
)
_AGENT_FINDING = {
    "id": "F1",
    "title": "Missing call site",
    "description": "The plan never names the caller it changes.",
    "severity": "major",
    "evidence": "src/thing.py has two callers.",
    "suggested_fix": "Name both callers.",
    "criteria": [],
}


class _Fake(FakeRunner):
    """Answers every plan-review contract with an empty, schema-valid payload, and the agentic
    contract with one advisory finding."""

    name = "fake"

    def run(self, req) -> dict:  # type: ignore[override]
        if req.mode == "text":
            return {"text": "", "runner": self.name, "model": None, "trace_id": None}
        if req.output_schema == "plan_review_agentic_findings":
            payload: dict = {"findings": [dict(_AGENT_FINDING)]}
        elif req.output_schema == "plan_review_verification":
            payload = {"verifications": []}
        elif req.output_schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _findings.validate_structured(dict(payload), req.output_schema)
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
    rebar.init_repo(repo_root=str(repo))
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _reviewed(repo: Path, engine: str) -> tuple[dict, dict]:
    nonce = f"\nMaterial nonce (test isolation): {uuid.uuid4().hex}.\n"
    tid = rebar.create_ticket("task", "parity plan", description=_DESC + nonce, repo_root=str(repo))
    verdict = rebar.llm.review_plan(tid, runner=_Fake(), repo_root=str(repo), engine=engine)
    stored = sidecar.latest_review_result(tid, repo_root=str(repo))
    assert stored is not None, f"{engine}: no REVIEW_RESULT sidecar read back"
    return verdict, stored


def test_both_engines_satisfy_one_verdict_and_sidecar_contract(repo: Path) -> None:
    pipeline, pipeline_stored = _reviewed(repo, "pipeline")
    agentic, agentic_stored = _reviewed(repo, "agentic")

    for verdict in (pipeline, agentic):
        _findings.validate_structured(verdict, "plan_review_verdict")
        assert verdict["verdict"] == "PASS"
        assert verdict["sidecar_emitted"] is True
    assert agentic["coverage"]["engine"] == "agentic"

    assert pipeline_stored["schema"] == agentic_stored["schema"] == "plan_review_result_v2"
    assert set(pipeline_stored) == set(agentic_stored)
    assert agentic_stored["verdict"] == "PASS"
    assert agentic_stored["coverage"]["engine"] == "agentic"
    assert any(f.get("tier") == "AGENTIC" for f in agentic_stored["findings"])
