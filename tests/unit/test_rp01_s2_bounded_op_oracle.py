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
    from rebar.llm.anthropic_model import _pai_model

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


def test_unknown_count_response_is_omitted_but_retained_in_full_history():
    """AC#3 (unknown-count + retention invariant): a failed response whose token count is
    UNKNOWN (no usage metadata) is omitted WHOLE from the projected retry wire — the fit rule
    fails safe, never guessing its size onto the wire — while the NON-MUTATING projection
    leaves it in the underlying history (``all_messages()`` retains all of it). Exercised
    through the public ``wire_history_processor`` seam: the returned wire is a filtered COPY,
    so the omitted response is absent from the wire yet still present in the original message
    list. A fitting (known-small) response is kept — the teeth separating unknown from fits."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
    from pydantic_ai.usage import RequestUsage

    from rebar.llm.anthropic_model import _pai_model
    from rebar.llm.pai_retry import wire_history_processor

    cfg = LLMConfig(repo_path=".")
    proc = wire_history_processor([_pai_model(cfg)], 16000).processor

    unknown = ModelResponse(parts=[TextPart("UNKNOWN-USAGE-BODY-73")])  # no usage → unknown
    request = ModelRequest(parts=[UserPromptPart(content="hi")])
    history = [request, unknown]
    wire = proc(history)

    def _texts(msgs):
        return [
            p.content
            for m in msgs
            if isinstance(m, ModelResponse)
            for p in m.parts
            if isinstance(getattr(p, "content", None), str)
        ]

    assert "UNKNOWN-USAGE-BODY-73" not in _texts(wire), (
        "an unknown-count failed response must be omitted WHOLE from the projected wire"
    )
    assert "UNKNOWN-USAGE-BODY-73" in _texts(history), (
        "the projection must NOT mutate stored history — all_messages() retains the full response"
    )

    fits = ModelResponse(
        parts=[TextPart("KNOWN-SMALL-BODY-11")],
        usage=RequestUsage(input_tokens=10, output_tokens=10),
    )
    assert "KNOWN-SMALL-BODY-11" in _texts(proc([request, fits])), (
        "a known, provably-fitting response IS carried — the teeth separating unknown from fits"
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


def test_transient_error_is_reclassified_into_a_bounded_output_retry():
    """AC#5 (concision-guard delegation branch): a transient ``finish_reason='error'`` is NOT
    a truncation and NOT terminal — the guard delegates to ``structured.check_response``, which
    raises the RETRYABLE ``StructuredOutputError``, so it is translated into a bounded in-run
    ``ModelRetry`` and the run recovers on the good turn-2. Separates the transient-retry branch
    (recovers) from the terminal refusal branch (aborts) that share the same guard."""
    model, state = _scripted_model([{"text": "boom", "finish_reason": "error"}, {"text": _VALID}])
    result = _run(model)

    assert result["verdict"] == "PASS"
    assert state["calls"] == 2, "a transient error must drive exactly one bounded retry"


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


def test_lowering_structured_retry_limit_lowers_the_constructed_allowance():
    """AC#8 (one value seeds BOTH budgets, non-default): lowering ``req.structured_retry_limit``
    lowers the allowance N, and that SAME lowered N is the addend on the constructed
    ``UsageLimits.request_limit`` (base + N) — proving the addend tracks the output-retry
    counter for a non-default budget, not just the default. The RETURNED req_limit stays the
    bare base regardless."""
    import dataclasses

    from pydantic_ai.usage import UsageLimits

    from rebar.llm.structured_run import build_usage_limits, output_retry_allowance

    cfg = LLMConfig(repo_path=".", max_iterations=10)
    low_req = dataclasses.replace(_req(cfg), structured_retry_limit=1)
    n = output_retry_allowance(low_req)
    limits, req_limit, eff_max_iter = build_usage_limits(cfg, low_req, UsageLimits)

    base = max(1, math.ceil(eff_max_iter / 2))
    assert n == 1, "structured_retry_limit=1 lowers the allowance to 1"
    assert req_limit == base, "the RETURNED req_limit stays the bare base regardless of N"
    assert limits.request_limit == base + n, (
        "the constructed request_limit addend tracks the LOWERED allowance, not the default"
    )


# ─────────────────────── NativeOutput branch + 895c fallback (AC#7) ───────────────────────


def _native_cfg():
    """A config whose resolved candidate is a native-structured-output provider, so the bounded
    operation routes through the NativeOutput branch (``openai:gpt-4o`` reports
    ``native_structured_output=True``; the default Anthropic config resolves to the PROMPTED
    branch). ``completion_verdict`` is a SMALL contract, so ``output_mode`` keeps it ON the
    native path — the schema-complexity gate (bug 895c) only diverts the large verification
    contracts."""
    return LLMConfig(repo_path=".", model="openai:gpt-4o")


def test_native_branch_runs_under_the_bounded_op_and_keeps_truncation_terminal():
    """AC#7: on a native-capable provider the bounded operation routes through the NativeOutput
    branch (constrained decoding) — a clean native turn yields the validated verdict, and a
    truncated (``length``) native turn stays TERMINAL after exactly ONE call. The native branch
    attaches ``pai_output.guard_capability()`` (terminal), NOT the concision guard, so — unlike
    the prompted branch, where an identical ``length`` turn is reclassified into a bounded
    concision retry — a truncated native turn is never retried."""
    cfg = _native_cfg()

    ok, _state = _scripted_model([{"text": _VALID}])
    assert _run(ok, cfg=cfg)["verdict"] == "PASS", "a clean native turn yields the verdict"

    trunc, state = _scripted_model([{"text": '{"verdict": "PA', "finish_reason": "length"}])
    with pytest.raises(LLMRunnerError):
        _run(trunc, cfg=cfg)
    assert state["calls"] == 1, (
        "a truncated NATIVE turn is terminal (guard_capability), never a concision retry"
    )


def test_native_grammar_compilation_rejection_falls_back_to_the_prompted_path(monkeypatch):
    """AC#7 (bug-895c fallback preserved): when the provider 400s compiling this contract's JSON
    Schema into a decoding grammar (a schema-complexity rejection the gate under-predicted for
    THIS model/contract pair), the bounded operation falls back to the PROMPTED path and
    completes — rather than losing the step to a request that can never succeed as configured.
    The rejection is injected at the native run (simulating the provider's 400); the observable
    outcome is a validated verdict produced by the prompted turn."""
    from botocore.exceptions import ClientError

    from rebar.llm import structured_run

    def _reject_native(*_a, **_k):
        raise ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Grammar compilation timed out."}},
            "Converse",
        )

    monkeypatch.setattr(structured_run, "_run_native_output", _reject_native)

    prompted, state = _scripted_model([{"text": _VALID}])
    result = _run(prompted, cfg=_native_cfg())

    assert result["verdict"] == "PASS", "the prompted fallback produced the validated verdict"
    assert state["calls"] == 1, "exactly the prompted turn ran (the native run never billed one)"
