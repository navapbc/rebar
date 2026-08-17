"""RP-01 S2 — HELD-OUT oracle for the bounded structured-output operation
(ticket [rebar:kingsize-unfair-blackbird], 66b3-4214-c70b-4a9a).

Withheld from the implementer (who sees only ``test_rp01_s2_bounded_op_happy.py``). Pins the
edge/boundary/terminal behavior the happy path cannot: wire-context projection (complete-or-
omit on proven fit), the context-window overflow error, concision reclassification of
truncation and its bounded exhaustion, terminal refusal, and the shared request/output budget.

Every test drives the REAL ``PydanticAIRunner`` over an offline ``FunctionModel`` (or calls the
pure budget helper directly). No live/billable call can escape (ALLOW_MODEL_REQUESTS off).

RED-first behavioral tests (fail against today's manual scheduler for the right reason):
  * wire INCLUDE on fit — today's separate ``run_sync`` calls never carry the prior response
  * ContextWindowExceededError — today's path never raises it (it just succeeds on retry)
  * concision on truncation — today a ``length`` turn is TERMINAL, so recovery cannot succeed
  * shared request budget — today ``request_limit`` == base, with no output-retry allowance
Preservation guards (must stay green — a regression here breaks a landed contract):
  * terminal refusal / content-filter, and the #5221 provider_details-only refusal shape
Boundary teeth (separate a correct fit-rule from a naive always-include):
  * wire OMIT when the failed response does not fit
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm.config import LLMConfig
from rebar.llm.errors import ContextWindowExceededError, LLMRunnerError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit

_CONCISION = (
    "Make your response as concise as possible while fulfilling the prompt "
    "and output format requirements."
)
_VALID = '{"verdict": "PASS"}'


def _candidate_window(cfg):
    """The context window the wire fit-rule is evaluated against for ``cfg`` — the OWN window of
    the resolved candidate model (``candidates = [resolved]`` in the runner), exactly as
    :func:`rebar.llm.pai_retry._make_wire_projection` computes it. Derived here so the
    boundary tests size their token usage against the REAL window (a known config model has a
    large window, not the ladder minimum) rather than a hardcoded assumption."""
    from rebar.llm import model_classes
    from rebar.llm.runner import _pai_model

    return model_classes.own_window_tokens(_pai_model(cfg))


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    import pydantic_ai.models

    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", False)


def _scripted_model(script):
    """A ``FunctionModel`` scripted per call and recording the wire it saw each call.

    ``script`` is a list of dicts: ``text`` / ``finish_reason`` / ``provider_details`` /
    ``usage`` (an ``(input_tokens, output_tokens)`` pair). Clamped to the last entry.
    ``state['wires'][i]`` is the message list the i-th model call received on the wire.
    """
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    state = {"calls": 0, "wires": []}

    def gen(messages, info):
        state["wires"].append(list(messages))
        i = state["calls"]
        state["calls"] += 1
        spec = script[min(i, len(script) - 1)]
        kwargs = {
            "parts": [TextPart(spec.get("text", ""))],
            "finish_reason": spec.get("finish_reason"),
            "provider_details": spec.get("provider_details"),
        }
        if spec.get("usage") is not None:
            inp, out = spec["usage"]
            kwargs["usage"] = RequestUsage(input_tokens=inp, output_tokens=out)
        return ModelResponse(**kwargs)

    return FunctionModel(gen), state


def _req(cfg):
    return RunRequest(
        system_prompt="x",
        instructions="y",
        config=cfg,
        reviewers=["v"],
        mode="structured",
        output_schema="completion_verdict",
    )


def _run(model, *, cfg=None):
    cfg = cfg or LLMConfig(repo_path=".")
    return PydanticAIRunner(cfg, model_override=model).run(_req(cfg))


def _wire_texts(wire):
    """TextPart contents carried by projected MODEL-RESPONSE messages in a wire snapshot.

    Restricted to ``ModelResponse`` messages on purpose: the bounded operation projects a
    failed turn back onto the retry wire AS A RESPONSE (via ``history_processors``). The old
    per-attempt scheduler instead embedded a truncated faulty-reply SNIPPET into the next
    USER prompt — a different message kind — so reading only response messages is what makes
    the include/omit assertions test the projection, not the reask prompt text."""
    from pydantic_ai.messages import ModelResponse

    out = []
    for msg in wire:
        if not isinstance(msg, ModelResponse):
            continue
        for part in getattr(msg, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                out.append(content)
    return out


def _wire_request_texts(wire):
    """Retry-prompt (``RetryPromptPart``) string contents in a wire snapshot.

    A capability ``ModelRetry`` (the concision guard) lands its instruction as a
    ``RetryPromptPart`` inside the next ``ModelRequest`` — a REQUEST part, not a response —
    so the concision assertion reads request parts, complementary to ``_wire_texts`` (which
    reads projected response text). Kept separate so include/omit assertions stay scoped to
    the response projection and the concision assertion stays scoped to the reask prompt."""
    from pydantic_ai.messages import ModelRequest

    out = []
    for msg in wire:
        if not isinstance(msg, ModelRequest):
            continue
        for part in getattr(msg, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                out.append(content)
    return out


# ─────────────────────────── wire-context projection (AC#2, AC#3, AC#4) ──────────────────


def test_failed_response_is_projected_on_the_retry_wire_when_it_fits():
    """AC#2 (RED-first): a small failed response provably fits the window, so its complete
    text is carried on the SECOND request's wire — the bounded operation keeps the failed
    turn in context. Today's per-attempt scheduler runs a fresh ``run_sync``, so the prior
    response is never on the retry wire."""
    marker = "FAILED-TURN-BODY-fits-42"
    model, state = _scripted_model([{"text": marker, "usage": (10, 10)}, {"text": _VALID}])
    result = _run(model)

    assert result["verdict"] == "PASS"
    assert state["calls"] == 2
    assert any(marker in t for t in _wire_texts(state["wires"][1])), (
        "a failed response that provably fits the window must be on the retry wire"
    )


def test_failed_response_is_omitted_whole_when_it_does_not_fit():
    """AC#3 (boundary teeth): when input+output+reserve exceeds the window but the next
    request itself still fits (input+reserve <= window), the failed response is omitted
    WHOLE from the retry wire — never partially — yet recovery still completes. Separates a
    correct fit-rule from a naive always-include."""
    marker = "FAILED-TURN-BODY-too-big-99"
    # Size against the REAL resolved-candidate window: input alone still fits the next request
    # (input + reserve <= window), but input + output + reserve overflows it -> omit WHOLE.
    cfg = LLMConfig(repo_path=".")
    window = _candidate_window(cfg)
    model, state = _scripted_model(
        [{"text": marker, "usage": (window // 2, window)}, {"text": _VALID}]
    )
    result = _run(model, cfg=cfg)

    assert result["verdict"] == "PASS"
    assert state["calls"] == 2
    assert not any(marker in t for t in _wire_texts(state["wires"][1])), (
        "a failed response that does not fit must be omitted whole from the retry wire"
    )


def test_authoritative_input_over_window_raises_context_window_exceeded():
    """AC#4 (RED-first): when the authoritative input alone plus the output reserve exceeds
    the window, the next request cannot run at all — fail closed with
    ``ContextWindowExceededError``, never a silent truncation of authoritative input. Today's
    path has no such check and just succeeds on the retry."""
    # Authoritative input ALONE (input + reserve) overflows the real resolved-candidate window.
    cfg = LLMConfig(repo_path=".")
    window = _candidate_window(cfg)
    model, _state = _scripted_model([{"text": "bad", "usage": (window + 1, 5)}, {"text": _VALID}])
    with pytest.raises(ContextWindowExceededError):
        _run(model, cfg=cfg)


# ─────────────────────────── concision reclassification (AC#5) ───────────────────────────


def test_truncation_is_reclassified_into_a_bounded_concision_retry():
    """AC#5 (RED-first): a ``length`` truncation on the prompted branch is reclassified into
    a bounded in-run retry carrying the EXACT concision instruction, and recovery completes.
    Today a ``length`` finish_reason is TERMINAL (UnretryableOutputError), so no recovery."""
    model, state = _scripted_model(
        [{"text": "half an ans", "finish_reason": "length"}, {"text": _VALID}]
    )
    result = _run(model)

    assert result["verdict"] == "PASS"
    assert state["calls"] == 2
    assert any(_CONCISION in t for t in _wire_request_texts(state["wires"][1])), (
        "the retry wire must carry the exact concision instruction"
    )


def test_repeated_truncation_exhausts_the_bounded_allowance_and_aborts():
    """AC#5 (RED-first): a model that truncates every turn cannot loop unboundedly — the
    shared output-retry allowance (OUTPUT_RETRIES == 2) bounds it to 1 + 2 == 3 calls, then
    aborts as a runner error. Today the first ``length`` is already terminal (1 call)."""
    model, state = _scripted_model([{"text": "x", "finish_reason": "length"}])
    with pytest.raises(LLMRunnerError):
        _run(model)
    assert state["calls"] == 3, "1 initial + OUTPUT_RETRIES(2) truncation retries, then abort"


# ─────────────────────────── terminal refusal (AC#6, preservation) ───────────────────────


def test_content_filter_refusal_stays_terminal_after_one_call():
    """AC#6 (preservation): a content-filter/refusal turn is a complete, unusable response —
    it must stay terminal after exactly ONE model call, triggering neither an output retry
    nor a concision retry. A regression that made it retryable would burn the allowance."""
    model, state = _scripted_model([{"text": "no", "finish_reason": "content_filter"}])
    with pytest.raises(LLMRunnerError):
        _run(model)
    assert state["calls"] == 1, "a refusal must not be retried"


def test_provider_details_only_refusal_stays_terminal():
    """AC#6 (preservation, #5221 shape): a refusal signalled ONLY in provider_details (with
    finish_reason absent) is still terminal after one call — the two-layer guard is not
    fooled by an unmapped normalized finish_reason."""
    model, state = _scripted_model(
        [{"text": "sure", "finish_reason": None, "provider_details": {"refusal": "policy"}}]
    )
    with pytest.raises(LLMRunnerError):
        _run(model)
    assert state["calls"] == 1


# ─────────────────────────── shared request/output budget (AC#8) ─────────────────────────


def test_build_usage_limits_returns_bare_base_but_constructs_base_plus_allowance():
    """AC#8 (RED-first): ``build_usage_limits`` RETURNS the bare base req_limit
    (``ceil(eff_max_iter/2)`` — the value ``completion_banking`` inverts as ``2*B``), while
    the CONSTRUCTED ``UsageLimits.request_limit`` adds the output-retry allowance so the
    request budget can never trip before the output-retry counter. Today both equal the base
    (no allowance)."""
    from pydantic_ai.usage import UsageLimits

    from rebar.llm import structured
    from rebar.llm.structured_run import build_usage_limits

    cfg = LLMConfig(repo_path=".", max_iterations=10)
    limits, req_limit, eff_max_iter = build_usage_limits(cfg, _req(cfg), UsageLimits)

    base = max(1, math.ceil(eff_max_iter / 2))
    assert req_limit == base, "the RETURNED req_limit stays the bare base (banking inverse)"
    assert limits.request_limit == base + structured.OUTPUT_RETRIES, (
        "the CONSTRUCTED request_limit adds the output-retry allowance"
    )
