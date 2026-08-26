"""6cd0 (smart-evadable-teledu): the per-gate-run correlation identity.

`trace_id` is declared in review_result.schema.json and completion_verdict.schema.json
but every emitting site in runner.py hardcoded `None`, so a verdict could not name the
run that produced it. This mints one identity per gate run — read from the active
OpenTelemetry span when one is recording, else `secrets.token_hex(16)` — and threads it
into all three `runner.py` emitters via `req.config`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rebar.llm import config as llm_config
from rebar.llm.config import LLMConfig
from rebar.llm.runner import FakeRunner, RunRequest

pytestmark = pytest.mark.unit

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _ident(**kw) -> LLMConfig:
    """An LLMConfig carrying a run identity, as a gate boundary would build it."""
    return LLMConfig(**kw)


def _req(cfg: LLMConfig, **kw) -> RunRequest:
    return RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], **kw)


# ══════════════════════════ HAPPY PATH ══════════════════════════


def test_identity_stable_within_one_scope() -> None:
    """AC1: every read inside one gate run returns the same trace_id."""
    cfg = _ident(trace_id="a" * 32, ticket_id="7e9e", operation="review-plan")
    with llm_config.gate_config(cfg):
        seen = {llm_config.resolve_gate_config().trace_id for _ in range(3)}
    assert seen == {"a" * 32}


def test_trace_id_is_32_lowercase_hex() -> None:
    """AC3: the W3C trace-id shape Langfuse v3+ requires."""
    from rebar.llm.run_identity import mint_run_identity

    assert _HEX32.match(mint_run_identity(ticket_id="7e9e", operation="review-plan")[0])


def test_identity_absent_outside_a_gate_run(tmp_path: Path) -> None:
    """AC14: a standalone op carries no identity, so verdicts are unchanged."""
    assert LLMConfig.from_env(repo_root=tmp_path).trace_id is None


# ══════════════════════════ HELD-OUT ORACLE ══════════════════════════


def test_two_runs_differ_absent_enclosing_span() -> None:
    """AC2: distinct runs get distinct ids.

    Qualified deliberately: under one coarse caller-owned span every run inside it reads
    that span's id, so the values legitimately match. This asserts the no-span case.
    """
    from rebar.llm.run_identity import mint_run_identity

    a = mint_run_identity(ticket_id="t", operation="review-plan")[0]
    b = mint_run_identity(ticket_id="t", operation="review-plan")[0]
    assert a != b


class _StubSpanContext:
    def __init__(self, trace_id: int) -> None:
        self.trace_id = trace_id


class _StubSpan:
    def __init__(self, trace_id: int, recording: bool = True) -> None:
        self._ctx = _StubSpanContext(trace_id)
        self._recording = recording

    def is_recording(self) -> bool:
        return self._recording

    def get_span_context(self) -> _StubSpanContext:
        return self._ctx


def test_trace_id_taken_from_recording_span(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: with a recording span active, the id IS that span's trace id.

    Stubbed at `trace.get_current_span` rather than built with a real TracerProvider:
    the SDK ships in the optional `[tracing]` extra, and the API alone only ever returns
    a non-recording span, so a real-span test cannot run in the lean environment. This
    exercises rebar's own branch (is_recording -> get_span_context -> 032x) directly.
    """
    from opentelemetry import trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: _StubSpan(0x1234ABCD))
    from rebar.llm.run_identity import mint_run_identity

    got = mint_run_identity(ticket_id="t", operation="review-plan")[0]
    assert got == format(0x1234ABCD, "032x")
    assert _HEX32.match(got)


def test_invalid_zero_trace_id_falls_through_to_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recording span carrying the INVALID trace id (0) must NOT yield 32 zeroes."""
    from opentelemetry import trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: _StubSpan(0))
    from rebar.llm.run_identity import mint_run_identity

    got = mint_run_identity(ticket_id="t", operation="review-plan")[0]
    assert got != "0" * 32
    assert _HEX32.match(got)


def test_non_recording_span_falls_through_to_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API's default no-op span is not recording; minting must take over."""
    from opentelemetry import trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: _StubSpan(0x99, recording=False))
    from rebar.llm.run_identity import mint_run_identity

    assert mint_run_identity(ticket_id="t", operation="review-plan")[0] != format(0x99, "032x")


def test_minted_when_no_span_recording() -> None:
    """AC5: minting is the fallback, not the only path."""
    from rebar.llm.run_identity import mint_run_identity

    got = mint_run_identity(ticket_id="t", operation="verify-completion")[0]
    assert _HEX32.match(got)


def test_identity_crosses_submit_ctx_pool() -> None:
    """AC6: the contextvar reaches pool workers through the real _submit_ctx seam."""
    from concurrent.futures import ThreadPoolExecutor

    from rebar.llm.plan_review.generation import _submit_ctx

    cfg = _ident(trace_id="b" * 32, ticket_id="t", operation="review-plan")
    with llm_config.gate_config(cfg), ThreadPoolExecutor(max_workers=1) as ex:
        inner = _submit_ctx(ex, lambda: llm_config.resolve_gate_config().trace_id).result()
    assert inner == "b" * 32


@pytest.mark.parametrize("operation", ["review-plan", "verify-completion"])
def test_ticket_id_and_operation_ride_the_scope(operation: str) -> None:
    """AC7: both boundary-only values are readable by every op in the run."""
    cfg = _ident(trace_id="c" * 32, ticket_id="7e9e-07f7", operation=operation)
    with llm_config.gate_config(cfg):
        got = llm_config.resolve_gate_config()
    assert (got.ticket_id, got.operation) == ("7e9e-07f7", operation)


def test_findings_branch_carries_trace_id() -> None:
    """AC8: runner.py's FakeRunner findings branch reads req.config.trace_id."""
    cfg = _ident(trace_id="d" * 32, ticket_id="t", operation="review-plan")
    out = FakeRunner(findings=[]).run(_req(cfg))
    assert out["trace_id"] == "d" * 32


def test_structured_branch_carries_trace_id() -> None:
    """AC9: the mode='structured' branch — the one the completion verifier returns from.

    Selected by req.mode, so the findings-branch test above says nothing about it.
    """
    cfg = _ident(trace_id="e" * 32, ticket_id="t", operation="verify-completion")
    payload = {"verdict": "PASS", "findings": [], "summary": "ok"}
    out = FakeRunner(structured=payload).run(_req(cfg, mode="structured"))
    assert out["trace_id"] == "e" * 32


def test_identity_survives_per_request_config_derivation() -> None:
    """AC13: max_output_cfg uses dataclasses.replace, so the identity rides through."""
    from rebar.llm.review_kernel.verify import max_output_cfg

    cfg = _ident(trace_id="f" * 32, ticket_id="t", operation="review-plan", max_tokens=10)
    assert max_output_cfg(cfg).trace_id == cfg.trace_id


@pytest.mark.parametrize("schema", ["review_result.schema.json", "completion_verdict.schema.json"])
def test_schema_describes_correlation_id_not_langfuse(schema: str) -> None:
    """AC11: the documented contract must match the emitted value.

    The old wording ("Langfuse trace id when tracing is enabled") is now false, and
    "present inside any gate run" would ALSO be false — deterministic and fallback
    verdicts emit null from sites outside this change's scope.
    """
    root = Path(__file__).resolve().parents[2] / "src" / "rebar" / "schemas"
    desc = json.loads((root / schema).read_text())["properties"]["trace_id"]["description"]
    assert "langfuse" not in desc.lower(), desc
    assert "present inside any gate run" not in desc.lower(), desc
    assert "null" in desc.lower(), desc
