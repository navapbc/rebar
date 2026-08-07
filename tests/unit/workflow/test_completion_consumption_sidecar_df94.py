"""df94: gate-run CONSUMPTION (requests/tool_calls/duration) must survive to the completion
sidecar — for a FAILING gate run, not only a passing one.

Sibling of aec1 (which fixed the same observability defect on the `.rebar/usage.jsonl` usage-log
channel). This pins the SIDECAR channel that rides the ticket store: the COMPLETION_VERDICT
sidecar payload must carry a `metrics` block with the consumed `requests`/`tool_calls` and a
run duration, reaching parity with the plan-review REVIEW_RESULT sidecar's `metrics` block.

Offline only: a fake runner stands in for the LLM (no tokens, no network) and reports a real
run's consumption via its `_usage`; the reads the precheck performs are monkeypatched. The
assertions are on the OBSERVABLE sidecar payload (`completion_sidecar.build_payload`), never a
private name.
"""

from __future__ import annotations

import pytest

from rebar.llm import completion_sidecar
from rebar.llm.config import LLMConfig
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import gate_dispatch

pytestmark = pytest.mark.unit

# The consumption the agentic verifier actually spent on this run (mirrors the runner warning
# log line `requests=24 tool_calls=33 ... in 73.4s`).
_REQUESTS = 24
_TOOL_CALLS = 33


class _ConsumingRunner(FakeRunner):
    """A FakeRunner whose structured run reports real consumption on its `_usage` — exactly the
    key the real PydanticAIRunner stamps (`requests`/`tool_calls` merged from the run shape)."""

    name = "consuming-fake"

    def run(self, req):
        result = super().run(req)
        result["_usage"] = {"requests": _REQUESTS, "tool_calls": _TOOL_CALLS}
        return result


def _patch_reads(monkeypatch):
    import rebar

    monkeypatch.setattr(
        "rebar._reads.show_ticket",
        lambda tid, repo_root=None: {"ticket_id": "T-1", "ticket_type": "story"},
    )

    def _fake_list(*, parent=None, status=None, ticket_type=None, repo_root=None, **_kw):
        return []

    monkeypatch.setattr("rebar._reads.list_tickets", _fake_list)
    monkeypatch.setattr(
        rebar,
        "verify_signature",
        lambda cid, kind=None, repo_root=None: {"verdict": "certified"},
    )


def _failing_verdict(monkeypatch, tmp_path):
    """Produce a completion FAIL verdict through the REAL gate dispatch path, with the fake
    verifier reporting `_REQUESTS`/`_TOOL_CALLS` of consumption."""
    _patch_reads(monkeypatch)
    runner = _ConsumingRunner(
        structured={
            "verdict": "FAIL",
            "findings": [
                {
                    "criterion": "AC-1",
                    "detail": "unmet",
                    "severity": "high",
                    "dimension": "completion",
                }
            ],
        }
    )
    cfg = LLMConfig.from_env(repo_root=str(tmp_path))
    return gate_dispatch.produce_completion_verdict(
        "T-1", graph=False, repo_root=str(tmp_path), cfg=cfg, runner=runner
    )


def test_failing_run_consumption_survives_to_the_sidecar(monkeypatch, tmp_path):
    verdict = _failing_verdict(monkeypatch, tmp_path)
    assert verdict["verdict"] == "FAIL"

    payload = completion_sidecar.build_payload(verdict)
    assert payload["schema"] == "completion_verifier_fail_v1"
    metrics = payload.get("metrics")
    assert metrics is not None, (
        "the completion FAIL sidecar payload must carry a `metrics` block with the run's "
        "consumed counters (parity with the plan-review sidecar) — the gate records the LIMIT "
        "but silently dropped the CONSUMED requests/tool_calls"
    )
    assert metrics.get("requests") == _REQUESTS
    assert metrics.get("tool_calls") == _TOOL_CALLS
    # A run duration must be present too (parity with plan-review's total_ms).
    assert isinstance(metrics.get("total_ms"), (int, float))


def test_sanitize_diagnostic_admits_consumed_counters():
    # Part 1: the sanitization allowlist admitted the LIMITs but DROPPED the consumed counters,
    # so a durable diagnostic recorded the ceiling and silently lost the measurement. Assert the
    # observable output of the boundary: `requests`/`tool_calls` survive (plain integers, no
    # content), an unknown key is dropped, and string redaction still applies.
    from rebar.llm.failure import sanitize_diagnostic

    out = sanitize_diagnostic(
        {
            "requests": 24,
            "tool_calls": 33,
            "request_limit": True,
            "tool_calls_limit": False,
            "not_allowlisted": "drop-me",
        }
    )
    assert out["requests"] == 24
    assert out["tool_calls"] == 33
    assert out["request_limit"] is True
    assert "not_allowlisted" not in out
