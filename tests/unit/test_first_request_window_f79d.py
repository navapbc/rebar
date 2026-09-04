"""Story f79d — the context-window check covers the FIRST request (and every request).

The RCA on ticket [rebar:undamaged-adolescent-sable] proved `_make_wire_projection`'s
callable only descended into carried `ModelResponse` messages, so a first request — history
`[ModelRequest]` — was never measured and an input three times the window went straight to
the provider (a 400 surfaced as `LLMUnavailableError`). These tests pin the repaired class:
the projection estimates every kept request's authoritative content (instructions + part
content, at :data:`rebar.llm.pai_retry.CHARS_PER_TOKEN` chars/token) and fails closed with
:class:`ContextWindowExceededError` BEFORE the provider call.

Sizing note: the fit rule is `estimate + reserve > window` with `estimate = chars // 4`, so
the oversize fixtures use `window * 8` chars (~2x the window in tokens) — decisively over for
any reserve — and the admissibility fixture uses `window * 2` chars (enrich's maximal
first-line bound, ~window/2 tokens), which must pass.

Every runtime test drives the REAL ``PydanticAIRunner`` over an offline ``FunctionModel``
(``ALLOW_MODEL_REQUESTS`` off) — no live call can escape.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm.config import LLMConfig
from rebar.llm.errors import ContextWindowExceededError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit

_VALID = '{"verdict": "PASS"}'
_RESERVE = 16000


def _candidate_window(cfg):
    from rebar.llm import model_classes
    from rebar.llm.anthropic_model import _pai_model

    return model_classes.own_window_tokens(_pai_model(cfg))


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    import pydantic_ai.models

    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", False)


def _proc(cfg, reserve=_RESERVE):
    from rebar.llm.anthropic_model import _pai_model
    from rebar.llm.pai_retry import wire_history_processor

    return wire_history_processor([_pai_model(cfg)], reserve).processor


def _first_request(chars: int):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    return ModelRequest(parts=[UserPromptPart(content="x" * chars)])


# ─────────────────────────── seam-level: the projection measures requests ────────────────


def test_oversized_first_request_raises_before_any_call():
    """RED: a first request whose content alone estimates over the window must raise
    ``ContextWindowExceededError`` from the projection — today the callable never descends
    into a ``ModelRequest``, so the wire passes through untouched."""
    cfg = LLMConfig(repo_path=".")
    window = _candidate_window(cfg)
    proc = _proc(cfg)
    with pytest.raises(ContextWindowExceededError):
        proc([_first_request(window * 8)])


def test_enrich_maximal_bound_is_admissible():
    """AC#3 teeth: a request of exactly ``window * 2`` CHARS — enrich's maximal first-line
    bound — estimates at ~window/2 tokens and must PASS the seam. A chars/2 calibration
    would wrongly refuse it; this pins the chars/4 choice."""
    cfg = LLMConfig(repo_path=".")
    window = _candidate_window(cfg)
    proc = _proc(cfg)
    history = [_first_request(window * 2)]
    assert proc(history) == history


def test_oversized_request_on_a_retry_wire_also_raises():
    """The check is per-wire, not first-only: an over-window request raises even when the
    wire also carries a fitting projected response."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.usage import RequestUsage

    cfg = LLMConfig(repo_path=".")
    window = _candidate_window(cfg)
    proc = _proc(cfg)
    fits = ModelResponse(
        parts=[TextPart("small")], usage=RequestUsage(input_tokens=10, output_tokens=10)
    )
    with pytest.raises(ContextWindowExceededError):
        proc([_first_request(window * 8), fits])


def test_small_first_request_passes_untouched():
    """Preservation: an ordinary small first request is not refused and not mutated."""
    cfg = LLMConfig(repo_path=".")
    proc = _proc(cfg)
    history = [_first_request(64)]
    assert proc(history) == history


# ─────────────────────────── runtime oracle (mirrors the RCA's Experiment 1) ─────────────


def test_oversized_first_request_raises_typed_error_with_zero_model_calls():
    """RED (the bug's exact shape): the real runner with instructions sized over the resolved
    candidate window raises the typed error BEFORE the provider call — zero model calls —
    instead of handing ~2x-window tokens to the provider for a 400."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    state = {"calls": 0}

    def gen(messages, info):
        state["calls"] += 1
        return ModelResponse(parts=[TextPart(_VALID)])

    cfg = LLMConfig(repo_path=".")
    window = _candidate_window(cfg)
    req = RunRequest(
        system_prompt="x",
        instructions="y" * (window * 8),
        config=cfg,
        reviewers=["v"],
        mode="structured",
        output_schema="completion_verdict",
    )
    with pytest.raises(ContextWindowExceededError):
        PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)
    assert state["calls"] == 0, "the typed error must fire BEFORE any provider call"


def test_bounded_first_request_still_runs_to_verdict():
    """Preservation: a normally-sized run is untouched by the request-side check."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def gen(messages, info):
        return ModelResponse(parts=[TextPart(_VALID)])

    cfg = LLMConfig(repo_path=".")
    req = RunRequest(
        system_prompt="x",
        instructions="y",
        config=cfg,
        reviewers=["v"],
        mode="structured",
        output_schema="completion_verdict",
    )
    result = PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)
    assert result["verdict"] == "PASS"
