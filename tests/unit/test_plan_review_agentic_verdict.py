"""The agentic engine's verdict adapter (ADR 0123): severity → decision, the field mapping,
PASS signing, the classified failure of a failed agent run, the engine stamp, and the
engine-matched BLOCK reuse. Offline: every review runs on a counting FakeRunner.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar.llm import errors
from rebar.llm.findings import FindingsError
from rebar.llm.plan_review import agentic_verdict
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


def _f(fid: str, severity: str, **extra) -> dict:
    return {
        "id": fid,
        "title": f"Defect {fid}",
        "description": f"The plan gets {fid} wrong.",
        "severity": severity,
        "evidence": f"src/thing.py shows {fid}.",
        "suggested_fix": f"Fix {fid}.",
        "criteria": [],
        **extra,
    }


class _Fake(FakeRunner):
    """Answers the agentic contract with canned findings (or raises); a BLOCK-free pipeline."""

    name = "fake"

    def __init__(self, findings=None, *, error: BaseException | None = None) -> None:
        super().__init__()
        self.calls = 0
        self._agent_findings = findings or []
        self._error = error

    def run(self, req) -> dict:  # type: ignore[override]
        from rebar.llm import findings as _f

        self.calls += 1
        if req.output_schema == "plan_review_agentic_findings":
            if self._error is not None:
                raise self._error
            payload: dict = {"findings": [dict(f) for f in self._agent_findings]}
        elif req.mode == "text":
            return {"text": "", "runner": self.name, "model": None, "trace_id": None}
        elif req.output_schema == "plan_review_verification":
            payload = {"verifications": []}
        elif req.output_schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _f.validate_structured(dict(payload), req.output_schema)
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


def _ticket(repo: Path, desc: str = _PASSING_DESC) -> str:
    nonce = f"\nMaterial nonce (test isolation): {uuid.uuid4().hex}.\n"
    return rebar.create_ticket(
        "task", "agentic plan", description=desc + nonce, repo_root=str(repo)
    )


def _review(repo: Path, tid: str, runner, engine: str = "agentic", **kw):
    return rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), engine=engine, **kw)


def _agentic(findings: list[dict]) -> list[dict]:
    """The adapted agent findings (the DET floor's own advisories also ride the verdict)."""
    return [f for f in findings if f.get("tier") == "AGENTIC"]


def _status(repo: Path, tid: str) -> str:
    return rebar.llm.plan_review_status(tid, repo_root=str(repo))["verdict"]


# ── severity → verdict and the field mapping ────────────────────────────────────
def test_a_blocker_blocks_and_other_severities_are_advisory(repo: Path) -> None:
    tid = _ticket(repo)
    v = _review(repo, tid, _Fake([_f("F1", "blocker"), _f("F2", "major")]))
    assert v["verdict"] == "BLOCK"
    assert [f["severity"] for f in v["blocking"]] == ["blocker"]
    assert [f["severity"] for f in _agentic(v["advisory"])] == ["major"]
    assert v["signature"]["signed"] is False
    assert _status(repo, tid) == "unsigned"


def test_no_blocker_passes_signed_and_certified(repo: Path) -> None:
    tid = _ticket(repo)
    v = _review(repo, tid, _Fake([_f("F1", "major"), _f("F2", "trivial")]))
    assert v["verdict"] == "PASS"
    assert [f["agentic_id"] for f in _agentic(v["advisory"])] == ["F1", "F2"]  # agent's order
    assert v["signature"]["signed"] is True
    assert _status(repo, tid) == "certified"


def test_no_findings_passes(repo: Path) -> None:
    v = _review(repo, _ticket(repo), _Fake([]))
    assert v["verdict"] == "PASS"
    assert v["blocking"] == [] and _agentic(v["advisory"]) == []


def test_field_mapping_and_registry_filtered_criteria() -> None:
    mapped = agentic_verdict.map_finding(
        _f("F9", "minor", criteria=["E4", "not-a-criterion"]), {"E4", "A1"}
    )
    assert mapped == {
        "finding": "Defect F9: The plan gets F9 wrong.",
        "criteria": ["E4"],
        "evidence": ["src/thing.py shows F9."],
        "suggested_fix": "Fix F9.",
        "severity": "minor",
        "decision": "advisory",
        "tier": "AGENTIC",
        "agentic_id": "F9",
    }
    assert agentic_verdict.map_finding(_f("F1", "blocker"), set())["decision"] == "block"


def test_incomplete_hierarchy_is_indeterminate() -> None:
    v = agentic_verdict.adapt_findings(
        [_f("F1", "major")],
        ticket_id="t",
        ticket_type="task",
        det_blocking=[],
        det_advisory=[],
        det_coverage={},
        hierarchy_incomplete=True,
        hierarchy_incomplete_detail=["children"],
        runner_name="fake",
        model="m",
        provider_provenance=None,
    )
    assert v["verdict"] == "INDETERMINATE"
    assert v["coverage"]["engine"] == "agentic"


def test_verdict_model_is_the_frontier_class(repo: Path) -> None:
    from rebar.llm.config import LLMConfig
    from rebar.llm.model_classes import resolve_model_string

    v = _review(repo, _ticket(repo), _Fake([]))
    assert v["model"] == resolve_model_string("frontier", LLMConfig.from_env().repo_path)


# ── failed agent runs: classified, engine-stamped, never signed ────────────────
def _with_outcome(exc: BaseException, cls) -> BaseException:
    from rebar.llm import failure

    exc.outcome = failure.LLMOutcome(cls, {}, retryable=True)  # type: ignore[attr-defined]
    return exc


def _unretryable(stop_reason: str | None) -> BaseException:
    exc = errors.UnretryableOutputError("unusable turn")
    exc.stop_reason = stop_reason
    return exc


@pytest.mark.parametrize(
    ("error", "resolution", "rejected"),
    [
        ("outcome", "WAIT_AND_RETRY", False),
        (lambda: _unretryable("refusal"), "CHANGE_INPUT", True),
        (lambda: _unretryable("content_filter"), "CHANGE_INPUT", True),
        (lambda: _unretryable("max_tokens"), "CHANGE_SETTINGS", False),
        (lambda: _unretryable("length"), "CHANGE_SETTINGS", False),
        (lambda: _unretryable(None), "NEEDS_INVESTIGATION", False),
        (lambda: errors.LLMBudgetExhaustedError("budget"), "FIX_AGENT_DESIGN", False),
        (lambda: errors.RunawayToolLoopError("loop"), "FIX_AGENT_DESIGN", False),
        (lambda: RuntimeError("provider exploded"), "NEEDS_INVESTIGATION", False),
        (lambda: FindingsError("schema"), "NEEDS_INVESTIGATION", False),
    ],
    ids=[
        "outcome",
        "refusal",
        "content-filter",
        "truncation",
        "truncation-length",
        "retries-exhausted",
        "budget",
        "tool-loop",
        "runtime-error",
        "schema-invalid",
    ],
)
def test_a_failed_agent_run_records_engine_and_class(
    repo: Path, error, resolution: str, rejected: bool
) -> None:
    from rebar.llm import failure

    if error == "outcome":
        exc = _with_outcome(
            errors.LLMUnavailableError("down"), failure.ResolutionClass.WAIT_AND_RETRY
        )
    else:
        exc = error()
    tid = _ticket(repo)
    v = _review(repo, tid, _Fake(error=exc))
    assert v["verdict"] == "INDETERMINATE"
    assert v["signature"]["signed"] is False
    assert v["coverage"]["engine"] == "agentic"
    assert v["coverage"]["resolution_class"] == resolution
    assert v["coverage"]["input_rejected"] is rejected
    assert _status(repo, tid) == "unsigned"


def test_a_failing_scripted_agentic_step_is_needs_investigation(repo: Path, monkeypatch) -> None:
    from rebar.llm.plan_review import agentic

    def _boom(*a, **kw):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(agentic, "criteria_context", _boom)
    runner = _Fake([])
    v = _review(repo, _ticket(repo), runner)
    assert v["verdict"] == "INDETERMINATE"
    assert v["coverage"]["resolution_class"] == "NEEDS_INVESTIGATION"
    assert runner.calls == 0


def test_floor_verdict_names_the_engine_and_pipeline_coverage_does_not(repo: Path) -> None:
    floor = _review(repo, _ticket(repo, "## What\nchange a thing.\n\n## Why\nbecause.\n"), _Fake())
    assert floor["verdict"] == "BLOCK"
    assert floor["coverage"]["engine"] == "agentic"
    pipeline = _review(repo, _ticket(repo), _Fake(), engine="pipeline")
    assert "engine" not in pipeline["coverage"]


# ── reuse stays engine-matched ──────────────────────────────────────────────────
def test_a_pipeline_review_never_replays_an_agentic_block(repo: Path) -> None:
    tid = _ticket(repo)
    assert _review(repo, tid, _Fake([_f("F1", "blocker")]))["verdict"] == "BLOCK"
    runner = _Fake()
    v = _review(repo, tid, runner, engine="pipeline")
    assert v["coverage"].get("verdict_reuse") is not True
    assert runner.calls > 0  # the pipeline ran


def test_an_agentic_pass_certifies_for_a_later_pipeline_review(repo: Path) -> None:
    tid = _ticket(repo)
    assert _review(repo, tid, _Fake([]))["signature"]["signed"] is True
    runner = _Fake()
    v = _review(repo, tid, runner, engine="pipeline")
    assert v["coverage"].get("idempotent_skip") is True
    assert runner.calls == 0
    assert _status(repo, tid) == "certified"


# ── the agent's run-shape diagnostic and the DET floor reach the adapted verdict ──
def test_a_budget_failure_keeps_the_sanitized_run_shape_diagnostic(repo: Path) -> None:
    exc = errors.LLMBudgetExhaustedError("budget")
    # Attached as an attribute, exactly as run_failure.interpret_failure does.
    exc.diagnostic = {"requests": 41, "tool_calls": 80, "input_tokens": 9}  # type: ignore[attr-defined]
    v = _review(repo, _ticket(repo), _Fake(error=exc))
    diagnostic = v["coverage"]["diagnostic"]
    assert v["coverage"]["resolution_class"] == "FIX_AGENT_DESIGN"
    assert (diagnostic["requests"], diagnostic["tool_calls"]) == (41, 80)
    assert diagnostic["exception_type"] == "LLMBudgetExhaustedError"
    assert "input_tokens" not in diagnostic  # off the sanitizer allowlist


_PROVENANCE = {"provider": "fake-provider", "model_id": "fake-model", "endpoint": "fake"}


class _ProvenanceFake(_Fake):
    """Stamps a provider record on the agent call, as the real runner does."""

    def run(self, req) -> dict:  # type: ignore[override]
        out = super().run(req)
        if req.output_schema == "plan_review_agentic_findings":
            out = {**out, "provider_provenance": dict(_PROVENANCE)}
        return out


def test_det_advisories_and_provider_provenance_reach_the_agentic_verdict(repo: Path) -> None:
    # No file_impact declared: the Layer-1 floor raises its P9 advisory, and the review still runs.
    v = _review(repo, _ticket(repo), _ProvenanceFake([]))
    det = [f for f in v["advisory"] if f.get("tier") == "DET"]
    assert any("P9" in (f.get("criteria") or []) for f in det), v["advisory"]
    assert v["provider_provenance"] == _PROVENANCE


# ── an agent that read no code is never signed (bug bridal-gifted-vampirebat) ───────────
class _UsageFake(_Fake):
    """Reports a run-shape ``_usage`` on the agent call, as the real runner does."""

    def __init__(self, usage: dict, findings=None) -> None:
        super().__init__(findings)
        self._agent_usage = usage

    def run(self, req) -> dict:  # type: ignore[override]
        out = super().run(req)
        if req.output_schema == "plan_review_agentic_findings":
            out = {**out, "_usage": dict(self._agent_usage)}
        return out


_READ = {"tool": "read_file", "target": "src/thing.py"}


@pytest.mark.parametrize(
    "usage",
    [
        {"requests": 1, "tool_calls": 0, "distinct_fetches": []},
        {"requests": 2, "tool_calls": 1, "distinct_fetches": []},  # a show_ticket-only run
        {"requests": 1, "input_tokens": 10},  # the run-shape reducer failed: fail closed
    ],
    ids=["no-tool-call", "no-code-read", "shape-missing"],
)
def test_an_agent_that_read_no_code_is_unsigned_indeterminate(repo: Path, usage: dict) -> None:
    tid = _ticket(repo)
    assert _status(repo, tid) == "unsigned"
    v = _review(repo, tid, _UsageFake(usage))
    assert v["verdict"] == "INDETERMINATE"
    assert v["signature"]["signed"] is False
    assert _status(repo, tid) == "unsigned"  # no attestation was written
    coverage = v["coverage"]
    assert coverage["engine"] == "agentic"
    assert coverage["resolution_class"] == "FIX_AGENT_DESIGN"
    assert coverage["retryable"] is False
    assert "read_file" in coverage["diagnostic"]["message"]
    assert coverage["diagnostic"].get("tool_calls") == usage.get("tool_calls")


def test_an_agent_that_read_code_still_passes_and_signs(repo: Path) -> None:
    tid = _ticket(repo)
    usage = {"requests": 3, "tool_calls": 2, "distinct_fetches": [dict(_READ)]}
    v = _review(repo, tid, _UsageFake(usage))
    assert v["verdict"] == "PASS"
    assert v["signature"]["signed"] is True
    assert _status(repo, tid) == "certified"


@pytest.mark.parametrize("usage", [None, {}], ids=["absent", "empty"])
def test_a_runner_that_reports_no_usage_is_unchanged(repo: Path, usage) -> None:
    runner = _Fake([]) if usage is None else _UsageFake(usage)
    v = _review(repo, _ticket(repo), runner)
    assert v["verdict"] == "PASS"
    assert v["signature"]["signed"] is True


def test_agentic_review_is_a_required_gate_step() -> None:
    from rebar.llm.workflow import plan_review_recovery as recovery

    assert recovery.STEP_AGENTIC_REVIEW == "agentic_review"
    assert recovery.STEP_AGENTIC_REVIEW in recovery._PLAN_REVIEW_REQUIRED_STEP_IDS
