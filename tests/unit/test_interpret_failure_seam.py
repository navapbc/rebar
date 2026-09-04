"""``interpret_failure()`` — the except spine lifted out of ``PydanticAIRunner.run()`` (task
e6bd, ADR 0056 decision 3).

The spine is three arms in a LOAD-BEARING order:

    except UsageLimitExceeded  -> LLMRunnerError   (a step-budget stop, not an outage)
    except LLMError            -> re-raise         (already typed; just enrich it)
    except Exception           -> sampling-parameter rejection FIRST, else LLMUnavailableError

Order is the whole contract, and Python's own type lattice hides the bug if it is lost:

* ``UsageLimitExceeded`` is a plain ``Exception``, so a broad-arm-first spine silently
  reclassifies a budget stop as a provider OUTAGE;
* ``LLMConfigError`` is a SUBCLASS of ``LLMUnavailableError``, so ``isinstance`` cannot tell a
  correctly-translated sampling rejection from the broad fallback. Every arm-ordering assertion
  here therefore checks ``type(...) is``, never ``isinstance``.

These are behavioural assertions on a callable's raised type and attached metadata — no private
names, no source text — so a later rename or re-extraction inside the cluster does not break them.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from pydantic_ai.exceptions import UsageLimitExceeded

from rebar.llm.errors import (
    LLMBudgetExhaustedError,
    LLMConfigError,
    LLMError,
    LLMRunnerError,
    LLMUnavailableError,
    StructuredOutputError,
)
from rebar.llm.structured_run import FailureContext, interpret_failure

pytestmark = pytest.mark.unit


def _ctx(*, model: str = "bedrock:us.anthropic.claude-sonnet-4-6") -> FailureContext:
    return FailureContext(
        call_label="verifier",
        execution_mode="direct",
        ran_model=model,
        req_limit=3,
        eff_max_iter=6,
        started_at=0.0,
    )


class _Rejection(Exception):
    """A provider rejecting a sampling parameter: a 400 naming the parameter and a rejection
    word — the three conjuncts ``translate_sampling_parameter_rejection`` requires."""

    status_code = 400

    def __init__(self):
        super().__init__("ValidationException: 'temperature' is deprecated for this model")


# ── §A happy path: each arm raises its own type ──────────────────────────────────────────


def test_step_budget_stop_becomes_a_runner_error():
    """A UsageLimitExceeded is rebar's own budget stop, and must surface as LLMRunnerError with
    an actionable message — not as a provider failure."""
    with pytest.raises(LLMRunnerError) as caught:
        interpret_failure(UsageLimitExceeded("limit"), [], _ctx())
    assert "max_iterations" in str(caught.value), (
        "the budget error must name the knob an operator raises"
    )


def test_step_budget_stop_raises_the_typed_budget_subclass():
    """fd84: the budget stop must be identifiable PURELY BY TYPE. interpret_failure attaches
    the same run_shape() dict (same key set) to every exception it raises, so diagnostic-
    shape sniffing cannot tell the budget branch from a sampling rejection or an outage; the
    subclass is the only reliable discriminator, and it is what routes the completion gate
    into bounded recovery instead of propagating verdict-less."""
    with pytest.raises(LLMRunnerError) as caught:
        interpret_failure(UsageLimitExceeded("limit"), [], _ctx())
    assert type(caught.value) is LLMBudgetExhaustedError, (
        f"budget stop raised {type(caught.value).__name__}; the typed subclass is the "
        "contract downstream recovery routing depends on"
    )
    assert "max_iterations" in str(caught.value)
    diagnostic = getattr(caught.value, "diagnostic", None)
    assert isinstance(diagnostic, dict)
    for key in ("requests", "tool_calls", "request_limit", "tool_calls_limit"):
        assert key in diagnostic, f"budget diagnostic lost its {key!r} counter"


def test_an_already_typed_llm_error_is_re_raised_unchanged():
    """The spine's middle arm enriches a typed failure; it never re-wraps it."""
    original = StructuredOutputError("bad shape")
    with pytest.raises(LLMError) as caught:
        interpret_failure(original, [], _ctx())
    assert caught.value is original, "the typed error must be the SAME object, not a copy"


def test_an_unknown_provider_failure_becomes_llm_unavailable():
    """The broad arm is the one recognizable 'the LLM could not run' signal every client
    catches; an unrecognized provider fault must land there."""
    with pytest.raises(LLMUnavailableError) as caught:
        interpret_failure(RuntimeError("connection reset"), [], _ctx())
    assert "connection reset" in str(caught.value), "the provider's own text must survive"


# ── §B arm ordering: the property the extraction can silently destroy ────────────────────


def test_a_sampling_rejection_is_translated_before_the_broad_classification():
    """THE ordering property. A sampling-parameter rejection must become the typed config
    error, which is actionable, rather than the opaque outage the broad arm produces.

    ``type(...) is`` is mandatory: LLMConfigError subclasses LLMUnavailableError, so an
    isinstance check would pass even if the translation were dropped entirely."""
    with pytest.raises(LLMUnavailableError) as caught:
        interpret_failure(_Rejection(), [], _ctx())
    assert type(caught.value) is LLMConfigError, (
        "a sampling rejection was classified as an opaque outage — the broad arm ran first, "
        "or the translation was dropped"
    )
    assert "temperature" in str(caught.value), "the rejected parameter must be named"


def test_a_budget_stop_is_not_swallowed_by_the_broad_arm():
    """UsageLimitExceeded IS an Exception subclass, so if the arms are reordered the budget
    stop is reclassified as an outage and an operator chases a provider that never failed."""
    with pytest.raises(LLMError) as caught:
        interpret_failure(UsageLimitExceeded("limit"), [], _ctx())
    assert type(caught.value) is LLMBudgetExhaustedError, (
        f"budget stop became {type(caught.value).__name__}; the broad arm ran first"
    )


def test_a_typed_llm_error_is_not_reclassified_by_the_broad_arm():
    """Same trap one arm down: every LLMError is an Exception, so a lost middle arm turns a
    precise StructuredOutputError into a generic outage."""
    with pytest.raises(LLMError) as caught:
        interpret_failure(StructuredOutputError("bad shape"), [], _ctx())
    assert type(caught.value) is StructuredOutputError, (
        f"typed error became {type(caught.value).__name__}"
    )


def test_an_unrelated_400_keeps_its_outage_classification():
    """The contrast that proves the translation is NARROW: a 400 that is not a sampling
    rejection must NOT be redirected, or genuine bad requests get a misleading remedy."""

    class _BadRequest(Exception):
        status_code = 400

    with pytest.raises(LLMUnavailableError) as caught:
        interpret_failure(_BadRequest("malformed request"), [], _ctx())
    assert type(caught.value) is LLMUnavailableError, (
        "an unrelated 400 was swallowed by the sampling translation"
    )


# ── §C the metadata each arm must still attach ───────────────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        UsageLimitExceeded("limit"),
        StructuredOutputError("bad shape"),
        RuntimeError("connection reset"),
        _Rejection(),
    ],
    ids=["budget", "typed", "broad", "sampling"],
)
def test_every_arm_attaches_bounded_diagnostic_counters(exc):
    """Bounded counters from the failed run are what makes a failure debuggable without
    prompt/tool content. Every arm attaches them — including the sampling arm, which an
    implementation can easily return early from."""
    with pytest.raises(LLMError) as caught:
        interpret_failure(exc, [], _ctx())
    assert getattr(caught.value, "diagnostic", None) is not None, (
        f"the {type(exc).__name__} arm dropped its diagnostic"
    )


def test_the_broad_arm_attaches_a_disposition_without_changing_the_raised_type():
    """The classified disposition rides as METADATA. If attaching it changed the raised type,
    every existing ``except LLMUnavailableError`` caller would stop catching."""
    with pytest.raises(LLMUnavailableError) as caught:
        interpret_failure(RuntimeError("overloaded"), [], _ctx())
    assert type(caught.value) is LLMUnavailableError, "the disposition changed the raised type"
    assert getattr(caught.value, "outcome", None) is not None, (
        "the broad arm must attach the classified disposition"
    )


def test_the_original_provider_error_is_preserved_as_the_cause():
    """The provider's own exception must stay reachable for debugging, not be discarded when
    the spine substitutes rebar's typed error."""
    original = RuntimeError("connection reset")
    with pytest.raises(LLMUnavailableError) as caught:
        interpret_failure(original, [], _ctx())
    assert caught.value.__cause__ is original, "the provider error was dropped from the chain"


# ── §D end to end: the runner still routes its failures through the seam ─────────────────


def _run_with_failing_model(boom):
    """Drive the REAL runner with a model that raises, so the assertion covers the wiring in
    run() and not just the extracted function in isolation."""
    from pydantic_ai.models.function import FunctionModel

    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    cfg = LLMConfig(repo_path=".", model="bedrock:us.anthropic.claude-sonnet-4-6")
    return PydanticAIRunner(cfg, model_override=FunctionModel(boom)).run(
        RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], mode="text")
    )


def test_runner_still_collapses_an_unknown_provider_failure():
    """The end-to-end net for the broad arm, through the real entry point."""

    def _boom(messages, info):
        raise RuntimeError("status_code: 500, body: internal failure")

    with pytest.raises(LLMUnavailableError) as caught:
        _run_with_failing_model(_boom)
    assert type(caught.value) is LLMUnavailableError


def test_runner_surfaces_a_sampling_rejection_as_the_typed_config_error():
    """The end-to-end net for the ordering property: it must hold through run(), not merely
    inside the helper — a run() that stopped calling the seam would pass §B and fail here."""

    def _boom(messages, info):
        raise _Rejection()

    with pytest.raises(LLMUnavailableError) as caught:
        _run_with_failing_model(_boom)
    assert type(caught.value) is LLMConfigError, (
        "run() no longer routes provider failures through the ordered spine"
    )
