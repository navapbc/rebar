"""Happy-path contract for the gate ERROR sidecar (ticket 8bc5).

Tier: unit (real store + injected infra outage; no network — the runner is stubbed
to raise ``LLMUnavailableError``, so ``get_runner`` is never called). This pins the
core new behavior: when a gate hits an infrastructure exception, a dedicated
``gate_error_v1`` sidecar record (verdict ``ERROR`` with a non-empty ``error.cause``)
is persisted — ADDITIVELY, without changing the gate's existing outcome (plan-review
still degrades to INDETERMINATE). Completion-path / reader-isolation / no-false-positive
contracts live in the held-out companion.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

import rebar
from rebar.llm.config import LLMConfig
from rebar.llm.errors import (
    CompletionRecoveryError,
    LLMError,
    LLMUnavailableError,
    UnretryableOutputError,
)

pytestmark = pytest.mark.unit


class _OutageRunner:
    """A runner whose infra calls raise LLMUnavailableError — drives the gate's
    ``except LLMUnavailableError`` (infra) path without any network."""

    name = "outage"

    def preflight(self) -> None:
        raise LLMUnavailableError("simulated systemic provider outage")

    def run(self, req):
        raise LLMUnavailableError("simulated systemic provider outage")


class _AlwaysTruncatedRunner:
    name = "truncated"

    def preflight(self) -> None:
        return None

    def run(self, req):
        exc = UnretryableOutputError("finish_reason=length token cap")
        exc.trace_id = "trace-cap-123"
        exc.usage = {
            "requests": 66,
            "tool_calls": 117,
            "input_tokens": 3_350_000,
            "output_tokens": 4_096,
        }
        raise exc


class _RefusalRunner:
    name = "refusal"

    def preflight(self) -> None:
        return None

    def run(self, req):
        raise UnretryableOutputError("the model refused to answer")


@pytest.fixture
def store(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    rebar.init_repo(repo_root=str(repo))
    return str(repo)


def scan_event_payloads(ticket_id: str, repo_root: str, suffix: str) -> list[dict]:
    """Raw sidecar ``data`` payloads for a ticket (bypasses the schema-guarded
    verdict readers, so a gate_error_v1 record is visible)."""
    from rebar import config as _config
    from rebar._engine_support.resolver import resolve_ticket_dir_name

    tracker = str(_config.tracker_dir(repo_root))
    ticket_dir = os.path.join(tracker, resolve_ticket_dir_name(ticket_id, tracker))
    out = []
    for f in sorted(os.listdir(ticket_dir)):
        if f.endswith(f"-{suffix}.json") and not f.startswith("."):
            with open(os.path.join(ticket_dir, f), encoding="utf-8") as fh:
                out.append(json.load(fh)["data"])
    return out


def _gate_errors(ticket_id: str, repo_root: str, suffix: str) -> list[dict]:
    return [
        p
        for p in scan_event_payloads(ticket_id, repo_root, suffix)
        if p.get("schema") == "gate_error_v1"
    ]


def test_plan_review_outage_writes_gate_error_and_still_degrades(store):
    tid = rebar.create_ticket(
        "task",
        "work ticket",
        description="A well-formed ticket.\n\n## Acceptance Criteria\n- [ ] x passes `pytest -q`",
        repo_root=store,
    )

    from rebar.llm.plan_review import review_plan

    verdict = review_plan(
        tid,
        source="local",
        repo_root=store,
        config=LLMConfig.from_env(repo_root=store),
        runner=_OutageRunner(),
        sign=False,
        emit_sidecar=True,
    )

    # 1) The pre-existing plan-review outcome is preserved: soft-degrade to INDETERMINATE.
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["coverage"]["llm_unavailable"] is True

    # 2) A dedicated gate_error_v1 record is persisted on the REVIEW_RESULT stream.
    errs = _gate_errors(tid, store, "REVIEW_RESULT")
    assert errs, "an infra outage at the plan-review gate must persist a gate_error_v1 record"
    rec = errs[0]
    assert rec["verdict"] == "ERROR"
    assert rec.get("error", {}).get("cause"), "gate_error record must carry a non-empty error.cause"


def test_completion_recovery_failure_persists_bounded_diagnostic(store):
    criteria = "\n".join(f"- [ ] criterion {index}" for index in range(1, 7))
    tid = rebar.create_ticket(
        "bug",
        "completion recovery",
        description=f"## Acceptance Criteria\n{criteria}",
        repo_root=store,
    )

    from rebar.llm.workflow.gate_dispatch import produce_completion_verdict

    with pytest.raises(CompletionRecoveryError):
        produce_completion_verdict(
            tid,
            graph=False,
            repo_root=store,
            cfg=LLMConfig.from_env(repo_root=store),
            runner=_AlwaysTruncatedRunner(),
        )

    records = _gate_errors(tid, store, "COMPLETION_VERDICT")
    assert len(records) == 1
    error = records[0]["error"]
    assert "raise max_tokens" not in error["cause"]
    assert error["evidence_ref"] == "completion-verification/recovery"
    diagnostic = error["diagnostic"]
    assert diagnostic["stage"] == "evidence"
    assert diagnostic["criteria_total"] == 7
    assert diagnostic["criteria_completed"] == 0
    assert diagnostic["tool_step_limit"] == 16
    assert "requests" in diagnostic
    assert "tool_calls" in diagnostic
    assert diagnostic["trace_id"] == "trace-cap-123"
    assert diagnostic["requests"] == 66
    assert diagnostic["tool_calls"] == 117
    assert diagnostic["input_tokens"] == 3_350_000
    assert diagnostic["output_tokens"] == 4_096


def test_completion_non_recovery_failure_does_not_persist_gate_error(store):
    tid = rebar.create_ticket(
        "bug",
        "completion refusal",
        description="## Acceptance Criteria\n- [ ] refusal is reported",
        repo_root=store,
    )

    from rebar.llm.workflow.gate_dispatch import produce_completion_verdict

    with pytest.raises(LLMError):
        produce_completion_verdict(
            tid,
            graph=False,
            repo_root=store,
            cfg=LLMConfig.from_env(repo_root=store),
            runner=_RefusalRunner(),
        )

    assert _gate_errors(tid, store, "COMPLETION_VERDICT") == []
