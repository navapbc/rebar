"""RP-01 S4 AC-1 / AC-2 — real-OpenTelemetry-SDK candidate-span NESTING proof.

The env-independent oracle (``tests/unit/test_candidate_tracing_oracle.py`` +
``tests/unit/test_candidate_tracing.py``) proves the candidate-span contract that CAN be
asserted against a recording fake: one span per attempted candidate, zero-based order, and the
outcome/model/usage/error attributes. What a fake tracer CANNOT observe is the real
ambient-context PARENTING — that every candidate span nests under the SINGLE Agent
model-request span that pydantic-ai's own OTel instrumentation opens. Proving that needs a real
``opentelemetry-sdk`` tracer provider shared by both rebar's candidate spans and pydantic-ai's
model-request span.

That SDK is the optional ``[tracing]`` extra, which the lean unit Verified lane deliberately
does not install (``.github/workflows/_build-and-test.yml`` installs only dev/reviewbot/ui, and
``tests/_extra_guard.py`` forbids ``importorskip``-dodging a missing extra there). So — exactly
like ``tests/external/test_llm_trace.py``, the one other real-OTel-SDK test — this proof lives
in the external tier: inert unless ``REBAR_RUN_EXTERNAL=1`` and the ``[tracing]`` extra is
installed. It makes NO live/billable call: a real Anthropic ``FallbackModel`` runs against an
httpx ``MockTransport`` (a dummy key, an in-memory span exporter), so it is fast and
network-free — it is external only because it needs the optional SDK, not because it hits a
service.
"""

from __future__ import annotations

import json

import httpx
import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.external

_PRIMARY = "claude-sonnet-4-6"
_FALLBACK = "claude-opus-4-8"
_PRIMARY_QUALIFIED = f"anthropic:{_PRIMARY}"
ATTR_ORDER = "rebar.candidate.order"


def _ok_body(model: str) -> dict:
    return {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _install_failover(monkeypatch, *, status: dict[str, int]) -> None:
    """Wire a real Anthropic ``FallbackModel`` over an httpx ``MockTransport`` whose per-model
    HTTP status drives failover — the same seam the unit oracle uses, inlined so this external
    module is self-contained."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    for name in ("FRONTIER", "STANDARD", "TRIVIAL"):
        for field in ("MODEL", "PROVIDER", "ENDPOINT"):
            monkeypatch.delenv(f"REBAR_LLM_{name}_{field}", raising=False)

    import pydantic_ai.models

    def _handler(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content).get("model", "")
        code = status.get(name, 200)
        if code == 200:
            return httpx.Response(200, json=_ok_body(name))
        return httpx.Response(code, json={"type": "error", "error": {"type": "x", "message": "no"}})

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **kw: httpx.MockTransport(_handler))
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)

    from rebar.llm import config as llm_config

    table = {
        "model_classes": {
            "standard": {
                "model": _PRIMARY,
                "provider": "anthropic",
                "fallback": [{"model": _FALLBACK, "provider": "anthropic"}],
            }
        }
    }
    monkeypatch.setattr(llm_config, "_read_llm_file_table", lambda repo_root=None: table)


def _run_standard() -> dict:
    cfg = LLMConfig(repo_path=".", model=_PRIMARY_QUALIFIED, llm_retry_max_attempts=1)
    req = RunRequest(
        system_prompt="sys",
        instructions="go",
        config=cfg,
        mode="text",
        execution_mode="single_turn",
    )
    return PydanticAIRunner(cfg).run(req)


def test_candidate_spans_nest_under_the_agent_model_request_span(monkeypatch):
    """With a REAL OTel SDK, the primary failing (529) then the fallback answering emits exactly
    two candidate spans, ordered zero-based, and BOTH parent to the SAME emitted Agent
    model-request span (ambient-context nesting; no duplicate from ``instrument_all``)."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai import Agent

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    Agent.instrument_all()

    _install_failover(monkeypatch, status={_PRIMARY: 529})
    _run_standard()

    spans = exporter.get_finished_spans()
    cand = [s for s in spans if s.attributes and ATTR_ORDER in dict(s.attributes)]
    assert len(cand) == 2, f"expected exactly two candidate spans, saw {[s.name for s in spans]}"
    assert sorted(dict(s.attributes)[ATTR_ORDER] for s in cand) == [0, 1]

    parents = {s.parent.span_id for s in cand if s.parent}
    assert len(parents) == 1, "all candidate spans must share one parent (the model-request span)"
    parent_id = next(iter(parents))
    by_id = {s.context.span_id: s for s in spans}
    assert parent_id in by_id, "the candidate spans' parent must be an emitted (rebar-visible) span"
