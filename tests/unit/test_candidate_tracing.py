"""RP-01 S4 — fallback/downgrade candidate tracing: HAPPY PATH + shared harness.

This module holds the shared, env-independent test harness for candidate tracing and the
happy-path contract the implementer builds against. The FULL oracle (edge cases, secret
sanitization, best-effort swallow, usage non-interference, real-OTel nesting) lives in the
held-out ``test_candidate_tracing_oracle.py``.

Design constraint (load-bearing): the OpenTelemetry *SDK* is only in the optional ``[tracing]``
extra and is ABSENT from the default test env / CI (`uv sync --extra dev`). So the core
behavioral contract is asserted through a rebar-owned RECORDING TRACER injected at
``rebar.llm.tracing._candidate_tracer`` — env-independent, runs in CI. Real OTel span emission
and parent nesting are proven separately, under ``importorskip('opentelemetry.sdk')``, in the
held-out oracle.

The recording tracer mimics only the sliver of the OTel API the candidate seam uses:
``start_as_current_span(name)`` as a context manager yielding a span with ``set_attribute``.
Assertions land on the recorded span attributes (an emitted contract), never on private
structure.
"""

from __future__ import annotations

import contextlib
import json

import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models

from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit

_PRIMARY = "claude-sonnet-4-6"
_FALLBACK = "claude-opus-4-8"
_PRIMARY_QUALIFIED = f"anthropic:{_PRIMARY}"
_FALLBACK_QUALIFIED = f"anthropic:{_FALLBACK}"

# ── the emitted candidate-span contract (attribute keys the implementer must set) ──────────
ATTR_ORDER = "rebar.candidate.order"
ATTR_MODEL = "gen_ai.request.model"  # OTel GenAI convention, provider-qualified value
ATTR_OUTCOME = "rebar.candidate.outcome"
ATTR_ERROR = "rebar.candidate.error"
OUTCOME_RETURNED = "returned"
OUTCOME_RAISED = "raised"


class RecordingSpan:
    """A minimal stand-in for an OTel span: records ``set_attribute`` into ``attrs``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.attrs: dict = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attrs[key] = value

    # tolerated no-ops the seam may call
    def set_status(self, *a, **k) -> None:  # pragma: no cover - tolerated
        pass

    def record_exception(self, *a, **k) -> None:  # pragma: no cover - tolerated
        pass


class RecordingTracer:
    """Records every span opened via ``start_as_current_span`` into ``spans`` (completion order)."""

    def __init__(self) -> None:
        self.spans: list[RecordingSpan] = []

    @contextlib.contextmanager
    def start_as_current_span(self, name: str, *a, **k):
        span = RecordingSpan(name)
        self.spans.append(span)
        yield span

    def candidate_spans(self) -> list[RecordingSpan]:
        """The subset that carry the rebar candidate marker attribute, in completion order."""
        return [s for s in self.spans if ATTR_ORDER in s.attrs]


def install_recording_tracer(monkeypatch) -> RecordingTracer:
    """Point the candidate-span seam at a recording tracer; return it for assertions."""
    import rebar.llm.tracing as tracing_mod

    tracer = RecordingTracer()
    monkeypatch.setattr(tracing_mod, "_candidate_tracer", lambda: tracer)
    return tracer


def _ok_body(model: str, text: str = "OK") -> dict:
    return {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _err_body(message: str = "nope") -> dict:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


def _transport_http_module():
    from anthropic import AsyncAnthropic

    from rebar.llm.anthropic_model import _anthropic_http_client_module

    return _anthropic_http_client_module(AsyncAnthropic)


def install_failover(
    monkeypatch, *, status: dict[str, int], err_message: str = "nope"
) -> list[str]:
    """Wire a real Anthropic MockTransport whose per-model HTTP ``status`` drives failover.

    Returns the mutable ``seen`` list (model ids the transport was asked for, in order).
    ``status`` maps model id -> HTTP code (default 200). A non-200 makes that candidate raise,
    exercising the real ``FallbackModel.request`` loop (not a spy)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    for name in ("FRONTIER", "STANDARD", "TRIVIAL"):
        for field in ("MODEL", "PROVIDER", "ENDPOINT"):
            monkeypatch.delenv(f"REBAR_LLM_{name}_{field}", raising=False)

    seen: list[str] = []
    transport_http = _transport_http_module()

    def _handler(request) -> object:
        name = json.loads(request.content).get("model", "")
        seen.append(name)
        code = status.get(name, 200)
        if code == 200:
            return transport_http.Response(200, json=_ok_body(name))
        return transport_http.Response(code, json=_err_body(err_message))

    monkeypatch.setattr(
        transport_http,
        "AsyncHTTPTransport",
        lambda *a, **kw: transport_http.MockTransport(_handler),
    )
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
    return seen


def run_standard() -> dict:
    """Run one single-turn text op through the configured ``standard`` chain; return the result."""
    cfg = LLMConfig(repo_path=".", model=_PRIMARY_QUALIFIED, llm_retry_max_attempts=1)
    req = RunRequest(
        system_prompt="sys",
        instructions="go",
        config=cfg,
        mode="text",
        execution_mode="single_turn",
    )
    return PydanticAIRunner(cfg).run(req)


# ── HAPPY PATH ─────────────────────────────────────────────────────────────────────────────


def test_primary_failure_then_fallback_success_emits_two_ordered_candidate_spans(monkeypatch):
    """The core contract: when the primary fails (529) and the fallback answers, BOTH candidates
    are visible as ordered spans — the primary ``raised``, the fallback ``returned`` — each
    tagged with its provider-qualified model identity. Pre-S4 only the answering model's single
    ``chat`` span exists, so the failed primary is invisible; this is what S4 fixes."""
    tracer = install_recording_tracer(monkeypatch)
    install_failover(monkeypatch, status={_PRIMARY: 529})

    result = run_standard()
    assert result["_usage"]  # the run produced a real answer

    spans = tracer.candidate_spans()
    assert len(spans) == 2, f"expected two candidate spans, saw {[s.attrs for s in spans]}"

    assert [s.attrs[ATTR_ORDER] for s in spans] == [0, 1]
    assert [s.attrs[ATTR_MODEL] for s in spans] == [_PRIMARY_QUALIFIED, _FALLBACK_QUALIFIED]
    assert [s.attrs[ATTR_OUTCOME] for s in spans] == [OUTCOME_RAISED, OUTCOME_RETURNED]
