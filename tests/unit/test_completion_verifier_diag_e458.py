"""Completion-verifier diagnosability + determinism scoping (bug e458).

Root cause (proven by instrumented trace): the verifier's test-existence judgment depends on the
agent's variable, fallible free-form search (it silently no-matches on regex/semantic queries
against the literal-substring `search_files` tool), so it non-deterministically reports a test
absent when the tool provably retrieves it. Two of the three scoped fixes are deterministic code
changes pinned here:

* the COMPLETION_VERDICT sidecar records `verified_at_sha` (+ `trace_id`) so any residual drift is
  provably on identical input (closes the diagnosability gap: today the record can't distinguish
  non-determinism from input drift).
* the completion verifier pins sampling temperature to 0 by default (variance mitigation), unless
  an operator explicitly set one.

(The third fix — the prompt guidance to search by ticket-id/exact-symbols, not regex/semantic
phrases — is an LLM-surface prompt change, validated behaviorally, not by a unit test.)
"""

from __future__ import annotations

from dataclasses import replace

from rebar.llm import completion, completion_sidecar
from rebar.llm.config import LLMConfig
from rebar.llm.workflow import gate_dispatch


def test_sidecar_records_verified_at_sha_and_trace_id() -> None:
    """Both the PASS and FAIL sidecar payloads carry verified_at_sha + trace_id."""
    fail = completion_sidecar.build_payload(
        {
            "verdict": "FAIL",
            "ticket_id": "x",
            "findings": [{"criterion": "c", "detail": "d"}],
            "verified_at_sha": "abc123def456",
            "trace_id": "trace-1",
        }
    )
    assert fail["verified_at_sha"] == "abc123def456", fail
    assert fail["trace_id"] == "trace-1", fail

    passp = completion_sidecar.build_payload(
        {
            "verdict": "PASS",
            "ticket_id": "x",
            "criteria": [],
            "verified_at_sha": "abc123def456",
            "trace_id": "trace-1",
        }
    )
    assert passp["verified_at_sha"] == "abc123def456", passp
    assert passp["trace_id"] == "trace-1", passp


def test_completion_verifier_pins_temperature_zero_by_default(monkeypatch) -> None:
    """_verify_completion_inner tunes cfg.temperature to 0 when unset, and preserves an explicit
    operator temperature (mirrors the model / step-floor 'explicit wins' tuning already there)."""
    captured: dict = {}

    import rebar._reads

    monkeypatch.setattr(
        rebar._reads,
        "show_ticket",
        lambda tid, repo_root=None: {"ticket_id": tid, "ticket_type": "task"},
    )
    monkeypatch.setattr(
        gate_dispatch,
        "produce_completion_verdict",
        lambda *a, cfg=None, **k: (
            captured.update(temp=cfg.temperature) or {"verdict": "PASS", "findings": []}
        ),
    )

    # default (temperature unset) -> pinned to 0.0
    completion._verify_completion_inner(
        "t", graph=False, repo_root=None, config=replace(LLMConfig(), temperature=None), runner=None
    )
    assert captured["temp"] == 0.0, captured

    # explicit operator temperature wins
    captured.clear()
    completion._verify_completion_inner(
        "t", graph=False, repo_root=None, config=replace(LLMConfig(), temperature=0.7), runner=None
    )
    assert captured["temp"] == 0.7, captured
