"""Held-out contracts for completion-verifier token-exhaustion recovery (bug 9a08)."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

import rebar
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMError, UnretryableOutputError

pytestmark = pytest.mark.unit

_CRITERIA = [f"criterion {index}" for index in range(1, 7)]


def _token_cap_error() -> UnretryableOutputError:
    error = UnretryableOutputError(
        "the model hit the token cap before finishing (finish_reason=length)"
    )
    error.trace_id = "trace-cap-123"  # type: ignore[attr-defined]
    error.usage = {  # type: ignore[attr-defined]
        "requests": 66,
        "tool_calls": 117,
        "input_tokens": 3_350_000,
        "output_tokens": 4_096,
    }
    return error


class _RecoveryRunner:
    """The agentic history exhausts; only a fresh tool-free finalizer can finish."""

    name = "heldout-recovery"

    def __init__(
        self,
        *,
        finalizer_exhausts: bool = False,
        finalizer_reports_unmet: bool = False,
    ) -> None:
        self.finalizer_exhausts = finalizer_exhausts
        self.finalizer_reports_unmet = finalizer_reports_unmet
        self.calls = []
        self.agentic_calls = 0

    def preflight(self) -> None:
        return None

    def run(self, request):
        self.calls.append(request)
        if request.execution_mode == "agentic":
            self.agentic_calls += 1
            if self.agentic_calls == 1:
                # The ordinary aggregate history reproduces the reported cap exhaustion.
                raise _token_cap_error()
            # The frozen contract's recovery precondition: isolated bounded histories can
            # complete one criterion apiece and return compact structured evidence.
            criterion = request.instructions.split("(evaluate only this):\n", 1)[1].split(
                "\n\nTicket context:", 1
            )[0]
            return {
                "verdict": "PASS",
                "findings": [],
                "criteria": [
                    {
                        "criterion": criterion,
                        "met": True,
                        "citation": {
                            "kind": "source",
                            "description": f"bounded evidence for {criterion}",
                        },
                        "kind": "codebase-verifiable",
                    }
                ],
                "summary": f"{criterion} is met.",
                "runner": self.name,
                "model": "fake",
                "trace_id": f"trace-evidence-{self.agentic_calls - 1}",
            }
        if self.finalizer_exhausts:
            raise _token_cap_error()
        expected = json.loads(request.instructions)["expected_criteria"]
        return {
            # Deliberately inconsistent when finalizer_reports_unmet=True: deterministic
            # recovery must never accept PASS while a criterion record says unmet.
            "verdict": " pass " if self.finalizer_reports_unmet else "PASS",
            "findings": [],
            "criteria": [
                {
                    "criterion": criterion,
                    "met": not (self.finalizer_reports_unmet and criterion == expected[-1]),
                    "citation": {
                        "kind": "source",
                        "description": f"held-out evidence for {criterion}",
                    },
                    "kind": "codebase-verifiable",
                }
                for criterion in expected
            ],
            "summary": "All six criteria are met.",
            "runner": self.name,
            "model": "fake",
            "trace_id": "trace-recovered-456",
        }


@pytest.fixture
def store(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "initial"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "heldout-key")
    rebar.init_repo(repo_root=str(repo))
    description = "\n".join(
        [
            "A normal multi-criterion completion ticket.",
            "",
            "## Acceptance Criteria",
            *[f"- [ ] {criterion}" for criterion in _CRITERIA],
        ]
    )
    ticket_id = rebar.create_ticket(
        "bug",
        "bounded completion recovery",
        description=description,
        repo_root=str(repo),
    )
    return str(repo), ticket_id


def _gate_errors(ticket_id: str, repo_root: str) -> list[dict]:
    from rebar import config as _config
    from rebar._engine_support.resolver import resolve_ticket_dir_name

    tracker = str(_config.tracker_dir(repo_root))
    ticket_dir = os.path.join(tracker, resolve_ticket_dir_name(ticket_id, tracker))
    records = []
    for filename in sorted(os.listdir(ticket_dir)):
        if not filename.endswith("-COMPLETION_VERDICT.json") or filename.startswith("."):
            continue
        with open(os.path.join(ticket_dir, filename), encoding="utf-8") as handle:
            payload = json.load(handle)["data"]
        if payload.get("schema") == "gate_error_v1":
            records.append(payload)
    return records


def test_completion_token_cap_recovers_with_fresh_structured_verdict(store):
    """A capped agentic history must not strand an otherwise verifiable close."""
    from rebar.llm.workflow import gate_dispatch

    repo_root, ticket_id = store
    runner = _RecoveryRunner()

    verdict = gate_dispatch.produce_completion_verdict(
        ticket_id,
        graph=False,
        repo_root=repo_root,
        cfg=LLMConfig.from_env(repo_root=repo_root),
        runner=runner,
    )

    assert runner.calls[0].execution_mode == "agentic", (
        "precondition: the production completion evidence-gathering path exhausted"
    )
    assert any(call.execution_mode == "single_turn" for call in runner.calls[1:]), (
        "recovery must finalize from a fresh tool-free history, not retry the exhausted agent"
    )
    assert verdict["verdict"] == "PASS"
    assert verdict["findings"] == []
    returned_criteria = [item["criterion"] for item in verdict["criteria"]]
    assert returned_criteria[: len(_CRITERIA)] == _CRITERIA
    assert any(
        "bug" in criterion.lower() and "resolv" in criterion.lower()
        for criterion in returned_criteria[len(_CRITERIA) :]
    ), "bug recovery must independently verify that the reported defect is resolved"
    assert verdict["trace_id"] == "trace-recovered-456"


def test_completion_recovery_never_passes_with_an_unmet_criterion(store):
    """Deterministic reconciliation must veto an internally inconsistent PASS."""
    from rebar.llm.workflow import gate_dispatch

    repo_root, ticket_id = store
    runner = _RecoveryRunner(finalizer_reports_unmet=True)

    try:
        verdict = gate_dispatch.produce_completion_verdict(
            ticket_id,
            graph=False,
            repo_root=repo_root,
            cfg=LLMConfig.from_env(repo_root=repo_root),
            runner=runner,
        )
    except LLMError as exc:
        assert "unmet criterion" in str(exc).lower(), (
            "a typed fail-closed veto is valid only when it identifies the contradiction"
        )
        return

    assert verdict["verdict"] == "FAIL"
    assert any(item["met"] is False for item in verdict["criteria"])
    assert verdict["findings"], "an unmet criterion must produce a fail-closed finding"


def test_completion_token_cap_recovery_exhaustion_fails_closed_with_diagnostics(store):
    """If the safe finalizer also exhausts, preserve diagnostics and never invent PASS."""
    from rebar.llm.workflow import gate_dispatch

    repo_root, ticket_id = store
    runner = _RecoveryRunner(finalizer_exhausts=True)

    with pytest.raises(LLMError) as caught:
        gate_dispatch.produce_completion_verdict(
            ticket_id,
            graph=False,
            repo_root=repo_root,
            cfg=LLMConfig.from_env(repo_root=repo_root),
            runner=runner,
        )

    message = str(caught.value)
    assert "recovery" in message.lower()
    assert "raise max_tokens" not in message
    assert "gate_error_v1" in message or "diagnostic" in message.lower()
    assert any(call.execution_mode == "single_turn" for call in runner.calls[1:])

    errors = _gate_errors(ticket_id, repo_root)
    assert errors, "exhausted safe recovery must persist a diagnostic sidecar"
    diagnostic = json.dumps(errors[-1], sort_keys=True)
    assert "trace-cap-123" in diagnostic
    assert "requests" in diagnostic and "tool_calls" in diagnostic
    assert errors[-1]["verdict"] == "ERROR", "exhaustion remains fail-closed, never PASS"
