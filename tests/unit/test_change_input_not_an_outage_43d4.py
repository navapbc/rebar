"""Bug 43d4 — a ``CHANGE_INPUT`` provider failure must not wear provider-OUTAGE clothes.

``interpret_failure``'s broad arm classifies every unrecognised provider exception and then
raises ``LLMUnavailableError`` regardless of what the classification said. For a
``CHANGE_INPUT`` failure — a context-length 400, a 413, a content-filter refusal — that is the
wrong type: the provider is healthy, the *input* is not, and no amount of waiting will help.

The consequence is not cosmetic. ``plan_review.sizing.pass1_with_ladder`` orders its handlers
``except LLMUnavailableError: raise`` BEFORE ``except Exception: if is_context_limit_error(...)``,
and Python matches top-to-bottom — so the size ladder that exists precisely for this provider
signal can never see it. §C below is the mechanism test: it drives the REAL ladder and observes
the fallback that the wrong type suppresses.

Every assertion here is on observable behaviour — a raised type, a message, a call count, the
ladder's own event strings — never a private name or a grep of source text.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

import httpx
from pydantic_ai.exceptions import ContentFilterError, ModelHTTPError

from rebar.llm.errors import (
    LLMError,
    LLMInputRejectedError,
    LLMRunnerError,
    LLMUnavailableError,
)
from rebar.llm.failure import ResolutionClass
from rebar.llm.run_failure import FailureContext, interpret_failure

pytestmark = pytest.mark.unit

_CONTEXT_400_TEXT = "prompt is too long: 206826 tokens > 200000 maximum"


def _ctx() -> FailureContext:
    return FailureContext(
        call_label="verifier",
        execution_mode="direct",
        ran_model="claude-sonnet-4-5",
        req_limit=3,
        eff_max_iter=6,
        started_at=0.0,
    )


def _http(status: int, message: str, err_type: str = "invalid_request_error") -> ModelHTTPError:
    return ModelHTTPError(
        status_code=status,
        model_name="claude-sonnet-4-5",
        body={"error": {"type": err_type, "message": message}},
    )


def _raised(exc: BaseException) -> BaseException:
    """Run the seam and return the exception it raises (it always raises)."""
    with pytest.raises(LLMError) as caught:
        interpret_failure(exc, [], _ctx())
    return caught.value


# ── §A AC#1 — a CHANGE_INPUT failure is typed and worded as a caller-fixable input problem ──


@pytest.mark.parametrize(
    "exc",
    [
        _http(400, _CONTEXT_400_TEXT),
        _http(413, "request_too_large: the payload exceeds the per-request cap"),
        ContentFilterError("the model refused to continue"),
    ],
    ids=["context-length-400", "413-too-large", "content-filter"],
)
def test_a_change_input_failure_is_not_raised_as_a_provider_outage(exc):
    """``type(...) is`` is mandatory, not stylistic: if the new class were made a SUBCLASS of
    ``LLMUnavailableError`` to keep the old catchers working, an isinstance check would pass
    while the defect — and the dead size ladder below — survived untouched."""
    err = _raised(exc)
    assert type(err) is LLMInputRejectedError, (
        f"a CHANGE_INPUT failure surfaced as {type(err).__name__}; the caller cannot tell "
        "'shrink your input' from 'the provider is down'"
    )
    assert not isinstance(err, LLMUnavailableError), (
        "the input-rejection type inherits from LLMUnavailableError, so every "
        "`except LLMUnavailableError` still swallows it and nothing observable changed"
    )


def test_the_providers_own_text_survives_the_rewrap():
    """The provider's sentence is the only thing that tells an operator WHICH bound was hit.
    It must ride through verbatim, and the message must also say the input is at fault."""
    err = _raised(_http(400, _CONTEXT_400_TEXT))
    assert _CONTEXT_400_TEXT in str(err), "the provider's own diagnosis was discarded"
    assert "provider call failed" not in str(err), (
        "the message still reads as an outage; the wording asserts the opposite of the "
        "classification the seam computed"
    )
    assert "input" in str(err).lower(), "the message does not identify the input as the problem"


def test_the_classified_disposition_still_rides_on_the_raised_error():
    """25+ sites (enrich_drain's tombstone, both CLIs' exit codes, the MCP failure envelope,
    the close gate's remedy sentence) read the disposition off ``.outcome`` rather than off the
    type. Re-typing must not drop it."""
    err = _raised(_http(400, _CONTEXT_400_TEXT))
    outcome = getattr(err, "outcome", None)
    assert outcome is not None, "the new arm dropped the classified disposition"
    assert outcome.resolution_class is ResolutionClass.CHANGE_INPUT
    assert outcome.retryable is False
    assert getattr(err, "diagnostic", None) is not None, "the new arm dropped its counters"


def test_the_original_provider_error_stays_in_the_cause_chain():
    original = _http(400, _CONTEXT_400_TEXT)
    err = _raised(original)
    assert err.__cause__ is original, "the provider error was dropped from the chain"


# ── §B AC#2 + AC#3 — containment, and the discrimination that makes the fix worth having ──


def test_the_new_type_stays_inside_the_shared_error_vocabulary():
    """AC#2. The 13 broad ``except LLMError`` handlers (both CLIs, MCP, the close gate,
    completion recovery) must keep catching, or a rejected input becomes an uncaught crash."""
    err = _raised(_http(400, _CONTEXT_400_TEXT))
    assert isinstance(err, LLMError)
    assert isinstance(err, LLMRunnerError), (
        "the input-rejection type sits outside LLMRunnerError, so it is not a sibling of the "
        "other 'rebar stopped itself, the provider is fine' errors"
    )


@pytest.mark.parametrize(
    "exc",
    [
        _http(500, "internal server error", "api_error"),
        _http(503, "overloaded", "overloaded_error"),
        _http(529, "overloaded", "overloaded_error"),
        _http(429, "slow down", "rate_limit_error"),
        _http(401, "invalid x-api-key", "authentication_error"),
        _http(400, "messages: field required"),
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        RuntimeError("connection reset by peer"),
    ],
    ids=[
        "500",
        "503",
        "529",
        "429",
        "401-auth",
        "400-malformed-not-a-size-problem",
        "transport-connect",
        "transport-timeout",
        "unknown-provider-fault",
    ],
)
def test_a_genuine_outage_still_raises_the_outage_type(exc):
    """AC#3 — THE test that matters. A change that relabels EVERYTHING as an input problem
    satisfies AC#1 and AC#2 while being strictly worse than today: it would tell an operator to
    shrink their prompt during a provider incident, and it would send a 5xx down the plan-review
    size ladder to burn the whole model ladder on an error no smaller input can fix.

    Both halves are load-bearing. ``type(...) is`` catches a relabel-everything change; the
    401 and the malformed-400 rows catch the subtler over-reach of keying on 'status is 4xx'
    or 'retryable is False' instead of on the CHANGE_INPUT classification the seam computes."""
    err = _raised(exc)
    assert type(err) is LLMUnavailableError, (
        f"a genuine outage surfaced as {type(err).__name__}; the change relabels rather than "
        "discriminates"
    )


# ── §C the proven mechanism: the size ladder the wrong type suppressed ─────────────────────


def _drive_the_real_ladder(monkeypatch, boom: BaseException) -> tuple[list, list, list]:
    """Drive the REAL ``pass1_with_ladder`` against a chunk call that always raises ``boom``.

    Returns ``(model_calls, ladder_events, findings)``. Only the exception TYPE is ever varied
    between the cases below — same message text, same ladder code — so any difference in the
    call count is attributable to the type and nothing else."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import passes, sizing

    calls: list[tuple[str, list[str]]] = []

    def _always_fails(runner, cfg, *, plan, chunk, agentic, extra_context="", tf_provider=None):
        calls.append((cfg.model, [c["id"] for c in chunk]))
        raise boom

    monkeypatch.setattr(passes, "pass1_chunk", _always_fails)
    events: list[str] = []
    findings, _records = sizing.pass1_with_ladder(
        None,
        LLMConfig(model="claude-sonnet-4-5"),
        "the plan",
        [{"id": "c1"}, {"id": "c2"}],
        False,
        events,
    )
    return calls, events, findings


def test_a_rejected_input_reaches_the_size_ladder_instead_of_aborting_the_review(monkeypatch):
    """The mechanism. ``pass1_with_ladder`` documents 'on a context-limit signal, fall back to
    ONE CRITERION PER CALL … ESCALATE up the model ladder … emit a FAILURE FINDING', but its
    ``except LLMUnavailableError: raise`` arm is evaluated BEFORE the arm that consults
    ``is_context_limit_error``. While the seam raises an outage type the documented fallback is
    unreachable: the batch call fails once and the whole review aborts unsigned.

    Asserting the batch fallback ACTUALLY RAN — three calls and the ladder's own event strings —
    is what separates this from a test that would pass on any renamed exception."""
    boom = _raised(_http(400, _CONTEXT_400_TEXT))
    calls, events, findings = _drive_the_real_ladder(monkeypatch, boom)

    assert len(calls) == 3, (
        f"the ladder made {len(calls)} call(s); the documented batch -> one-criterion-per-call "
        "fallback did not run, so the review aborted on the first failure"
    )
    assert [ids for _model, ids in calls] == [["c1", "c2"], ["c1"], ["c2"]]
    assert any("one-criterion-per-call" in e for e in events), (
        f"the ladder recorded no fallback event: {events}"
    )
    assert [f.get("_too_big") for f in findings] == [True, True], (
        "the ladder did not emit the P8 'reduce/decompose the ticket' findings, so the operator "
        "is told nothing actionable"
    )


def test_an_outage_still_aborts_the_review_rather_than_climbing_the_ladder(monkeypatch):
    """The other half of the discrimination, at the ladder rather than at the seam. A 503 must
    still propagate: sending an outage down the size ladder would burn a call per criterion and
    then blame the operator's ticket size for a provider incident."""
    boom = _raised(_http(503, "overloaded", "overloaded_error"))
    with pytest.raises(LLMUnavailableError):
        _drive_the_real_ladder(monkeypatch, boom)


def test_the_new_prefix_does_not_itself_look_like_a_context_limit():
    """A prefix-wording trap with real consequences. ``is_context_limit_error`` matches the
    WHOLE string, so a prefix containing any of its phrases ('context', 'input length',
    'token limit', 'exceeds the maximum', …) would make EVERY rejected input — a content-filter
    refusal included — read as a context limit and burn the full model ladder before emitting a
    bogus 'too big to review' finding.

    The context-400 must match on the PROVIDER's text; the content-filter refusal must not
    match at all."""
    from rebar.llm.plan_review.sizing import is_context_limit_error

    context_err = _raised(_http(400, _CONTEXT_400_TEXT))
    assert is_context_limit_error(context_err) is True, (
        "the ladder no longer recognises a context-limit error — the provider's text was "
        "dropped or reworded"
    )

    refusal = _raised(ContentFilterError("the model declined to answer"))
    assert is_context_limit_error(refusal) is False, (
        "the message PREFIX alone matches is_context_limit_error, so a content refusal will be "
        "mistaken for an oversized prompt and climb the whole model ladder"
    )


# ── §D AC#4 — retry and fallback eligibility are pinned, not touched ───────────────────────


def test_retry_statuses_are_unchanged():
    """AC#4. The transport must not start retrying a rejected input: a deterministic 400/413
    fails identically every time, so a retry is pure latency and spend."""
    from rebar.llm.anthropic_model import _RETRY_STATUSES

    assert 400 not in _RETRY_STATUSES
    assert 413 not in _RETRY_STATUSES
    assert {429, 529, 500, 502, 503, 504} <= _RETRY_STATUSES, (
        "an availability status was dropped from the transport retry set"
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http(400, _CONTEXT_400_TEXT), False),
        (_http(413, "request_too_large"), False),
        (ContentFilterError("refused"), False),
        (_http(503, "overloaded", "overloaded_error"), True),
        (httpx.ConnectError("refused"), True),
    ],
    ids=["context-400", "413", "content-filter", "503", "transport"],
)
def test_fallback_eligibility_is_unchanged(exc, expected):
    """AC#4. Another provider cannot fix an oversized or refused input, so a CHANGE_INPUT
    failure must not move the fallback chain to its next candidate — while the availability
    classes still must."""
    from rebar.llm.model_classes import should_fall_back

    assert should_fall_back(exc) is expected


# ── §E the fail-closed companions: a REJECTED input is just as BLIND as an outage ──────────


def test_a_rejected_input_still_fails_the_epic_bug_screen_closed():
    """Bug 1019's operator-ratified ruling is that a systemic provider error must propagate
    out of the epic-close bug screen rather than degrade each candidate to ``C``, because a
    screen that never ran must not report success.

    A prompt-too-long or content-refused screen is blind for exactly the same reason, but it
    arrives on a different type — so the narrow ``except LLMUnavailableError`` that enforced
    the ruling silently stops enforcing it. Without this assertion, an epic whose material
    outgrew the window would screen every candidate as ``C`` ("not a blocker") and close on
    evidence no model ever produced."""
    from rebar.llm import epic_bug_screen

    def _rejects(bug: dict, system_prompt: str) -> dict:
        raise LLMInputRejectedError(
            "the LLM provider rejected the request input: " + _CONTEXT_400_TEXT
        )

    bugs = [
        {"ticket_id": f"000{i}-0000-0000-0000", "title": f"bug {i}", "description": "d"}
        for i in range(2)
    ]
    with pytest.raises(LLMInputRejectedError, match="too long"):
        epic_bug_screen.screen_candidates(
            {"title": "an epic", "description": ""}, bugs, None, None, screen_fn=_rejects
        )
