"""The plan-review attestation records which engine produced it (ADR 0123 §5).

An agentic PASS signs an ``engine: agentic`` manifest line inside the signed bytes; a pipeline
manifest never carries the line and stays byte-identical; a manifest without it reads as
``pipeline``. Claim, close, ``review-plan --status``, ``sign-review`` and drift refresh accept
and preserve it. Offline: a FakeRunner and real signing in a temp repo.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar import schemas, signing
from rebar.llm import findings as _findings
from rebar.llm.plan_review import attest, manifest
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
_BLOCKER = {
    "id": "F1",
    "title": "Wrong file",
    "description": "The plan edits a file the change does not live in.",
    "severity": "blocker",
    "evidence": "src/thing.py has no such function.",
    "suggested_fix": "Name the right module.",
    "criteria": [],
}


class _Fake(FakeRunner):
    """Empty, schema-valid answers for every plan-review contract; canned agent findings."""

    name = "fake"

    def __init__(self, agent_findings=None, *, error: BaseException | None = None) -> None:
        super().__init__()
        self._agent = agent_findings or []
        self._error = error

    def run(self, req) -> dict:  # type: ignore[override]
        if req.mode == "text":
            return {"text": "", "runner": self.name, "model": None, "trace_id": None}
        if req.output_schema == "plan_review_agentic_findings":
            if self._error is not None:
                raise self._error
            payload: dict = {"findings": [dict(f) for f in self._agent]}
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
    _commit(repo, "init")
    return repo


def _commit(repo: Path, message: str) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _ticket(repo: Path) -> str:
    nonce = f"\nMaterial nonce (test isolation): {uuid.uuid4().hex}.\n"
    return rebar.create_ticket(
        "task", "engine plan", description=_DESC + nonce, repo_root=str(repo)
    )


def _review(repo: Path, tid: str, engine: str, runner=None):
    return rebar.llm.review_plan(tid, runner=runner or _Fake(), repo_root=str(repo), engine=engine)


def _signed_manifest(repo: Path, tid: str) -> list[str]:
    sig = signing.verify_signature(tid, kind="plan-review", repo_root=str(repo))
    assert sig.get("verified"), sig
    return attest._authoritative_manifest(sig)


def _status(repo: Path, tid: str) -> dict:
    return rebar.llm.plan_review_status(tid, repo_root=str(repo))


# ── happy path: signed, surfaced, accepted by claim and close ───────────────────
def test_an_agentic_pass_signs_and_surfaces_its_engine(repo: Path) -> None:
    from rebar._commands.gates import close_plan_review_gate_check

    tid = _ticket(repo)
    assert _review(repo, tid, "agentic")["signature"]["signed"] is True
    assert "engine: agentic" in _signed_manifest(repo, tid)

    status = _status(repo, tid)
    assert (status["verdict"], status["engine"]) == ("certified", "agentic")
    schemas.validator("plan_review_status").validate(json.loads(json.dumps(status)))

    rebar.claim(tid, repo_root=str(repo))
    state = rebar.show_ticket(tid, repo_root=str(repo))
    gate = close_plan_review_gate_check(tid, state, repo_root=str(repo))
    assert gate.get("ok") is True, gate


# ── carried through every re-sign ───────────────────────────────────────────────
def test_sign_review_keeps_the_engine(repo: Path) -> None:
    from rebar.llm.plan_review.resign import resign_plan_review

    tid = _ticket(repo)
    _review(repo, tid, "agentic")
    result = resign_plan_review(tid, repo_root=str(repo))
    assert result["signed"] is True, result
    assert "engine: agentic" in _signed_manifest(repo, tid)


def test_refresh_carries_the_prior_engine(monkeypatch) -> None:
    built: dict = {}

    def _capture(fields, **kw):
        built.update(fields)
        raise RuntimeError("stop before signing")

    monkeypatch.setattr(attest, "build_manifest", _capture)
    monkeypatch.setattr(attest, "_rehash", lambda paths, repo_root=None: {})
    monkeypatch.setattr(signing, "verify_signature", lambda *a, **k: {"key_id": "prior"})
    monkeypatch.setattr("rebar.llm.gate_context.current_code_sha", lambda: "a" * 40)
    prior = ["plan-review: PASS", "ticket: t", "model: m", "runner: r", "engine: agentic"]
    with pytest.raises(RuntimeError, match="stop before signing"):
        attest.refresh_attestation("t", prior, probe="E4", relation_snapshot_value=_NoPins())
    assert built["coverage"]["engine"] == "agentic"


class _NoPins:
    related_material: tuple = ()


# ── back-compat, unknown values, tamper, refusal ────────────────────────────────
def test_a_pipeline_manifest_is_byte_identical_and_reads_pipeline() -> None:
    verdict = {"verdict": "PASS", "ticket_id": "t", "coverage": {"counts": {}}}
    base = manifest.build_manifest(verdict, material="m")
    stamped = manifest.build_manifest(
        {**verdict, "coverage": {"counts": {}, "engine": "pipeline"}}, material="m"
    )
    assert stamped == base
    assert not any(line.startswith("engine:") for line in base)
    assert manifest.manifest_engine(base) == "pipeline"
    assert attest.manifest_engine is manifest.manifest_engine


def test_unknown_engine_values_write_nothing_and_read_pipeline() -> None:
    verdict = {"verdict": "PASS", "ticket_id": "t", "coverage": {"engine": "other"}}
    lines = manifest.build_manifest(verdict, material="m")
    assert not any(line.startswith("engine:") for line in lines)
    assert manifest.manifest_engine(["plan-review: PASS", "engine: other"]) == "pipeline"


def test_a_pipeline_attestation_reports_pipeline(repo: Path) -> None:
    tid = _ticket(repo)
    assert _review(repo, tid, "pipeline")["signature"]["signed"] is True
    assert not any(line.startswith("engine:") for line in _signed_manifest(repo, tid))
    assert _status(repo, tid)["engine"] == "pipeline"


def _rewrite_signed_payload(repo: Path, tid: str) -> int:
    """Rewrite ``engine: agentic`` inside each stored SIGNATURE envelope's signed payload."""
    import base64

    edited = 0
    for path in (repo / ".tickets-tracker" / tid).glob("*.json"):
        record = json.loads(path.read_text())
        envelope = (record.get("data") or record).get("envelope")
        if not isinstance(envelope, str):
            continue
        env = json.loads(envelope)
        payload = base64.b64decode(env["payload"]).decode()
        if "engine: agentic" not in payload:
            continue
        env["payload"] = base64.b64encode(
            payload.replace("engine: agentic", "engine: pipeline").encode()
        ).decode()
        target = record.get("data") or record
        target["envelope"] = json.dumps(env)
        path.write_text(json.dumps(record))
        edited += 1
    return edited


def test_the_engine_line_cannot_be_rewritten(repo: Path) -> None:
    tid = _ticket(repo)
    _review(repo, tid, "agentic")
    verified = signing.verify_signature(tid, kind="plan-review", repo_root=str(repo))
    # The plaintext manifest copy is not authoritative: rewriting it changes nothing.
    tampered = dict(verified)
    tampered["manifest"] = [
        "engine: pipeline" if line == "engine: agentic" else line for line in verified["manifest"]
    ]
    assert manifest.manifest_engine(attest._authoritative_manifest(tampered)) == "agentic"
    # Rewriting the signed payload itself breaks verification.
    assert _rewrite_signed_payload(repo, tid), "no stored signed payload carries the line"
    after = signing.verify_signature(tid, kind="plan-review", repo_root=str(repo))
    assert not after.get("verified")


@pytest.mark.parametrize("outcome", ["block", "indeterminate"])
def test_no_attestation_for_an_agentic_block_or_indeterminate(repo: Path, outcome: str) -> None:
    tid = _ticket(repo)
    runner = _Fake([_BLOCKER]) if outcome == "block" else _Fake(error=RuntimeError("provider down"))
    verdict = _review(repo, tid, "agentic", runner)
    assert verdict["signature"]["signed"] is False
    status = _status(repo, tid)
    assert (status["verdict"], status["engine"]) == ("unsigned", None)
    schemas.validator("plan_review_status").validate(json.loads(json.dumps(status)))
