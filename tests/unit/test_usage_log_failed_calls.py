"""A FAILED LLM call must still reach the usage log (bug 8455).

``PydanticAIRunner.run()`` recorded usage only at the END of the method, after the
``try/except`` whose spine (``interpret_failure``) ALWAYS re-raises — so a call that failed
wrote NO row at all. Observed live: a Bedrock review whose ``verify`` step 400'd produced five
healthy rows and nothing whatsoever about the two passes that failed, so the JSONL read as a
clean five-call review.

Every test here drives the real runner with a model that RAISES and then READS the file,
because the defect is about what reaches the FILE — reading ``usage_log.py``, or exercising
``run_shape()`` in isolation, cannot show it. Offline throughout: a ``FunctionModel``
override with ``ALLOW_MODEL_REQUESTS = False`` (the seam
``tests/unit/test_usage_log.py::test_runner_records_usage_at_seam`` already uses).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rebar.llm import usage_log
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit

_BOOM = "the provided model identifier is invalid"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No live/billable call can escape this module."""
    import pydantic_ai.models

    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", False)


def _log(monkeypatch, tmp_path):
    target = tmp_path / "usage.jsonl"
    monkeypatch.setenv(usage_log.ENV_VAR, str(target))
    return target


def _rows(target) -> list[dict]:
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text().splitlines() if line.strip()]


def _run(model, *, reviewers=("verify",)):
    """Drive the real runner once through ``model``; return the result or raise."""
    cfg = LLMConfig(repo_path=".")
    req = RunRequest(
        system_prompt="s",
        instructions="i",
        config=cfg,
        reviewers=list(reviewers),
        mode="text",
    )
    return PydanticAIRunner(cfg, model_override=model).run(req)


def _raising_model():
    def blow_up(messages, info: AgentInfo):
        raise RuntimeError(_BOOM)

    return FunctionModel(blow_up)


def _ok_model():
    def answer(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(answer)


def _looping_model(calls: dict):
    """Never terminates: burns real model REQUESTS until the step budget trips."""

    def loop(messages, info: AgentInfo):
        calls["n"] += 1
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.function_tools[0].name, args={"path": "."})]
        )

    return FunctionModel(loop)


# ── the defect: a raising call must not be invisible ──────────────────────────────────────


def test_a_failed_call_writes_a_usage_row(monkeypatch, tmp_path):
    """The Bedrock case: the pass was attempted and lost, and the log must say so."""
    target = _log(monkeypatch, tmp_path)
    with pytest.raises(LLMError):
        _run(_raising_model())
    assert _rows(target), "a failed LLM call wrote NO usage row — as if it never happened"


def test_the_failure_row_carries_the_same_identifying_fields_as_a_success_row(
    monkeypatch, tmp_path
):
    """`op`, `step`, `model_class`, `model`, `provider` — all available at failure time, since
    `step_identity` wraps the whole step execution INCLUDING the raise."""
    target = _log(monkeypatch, tmp_path)
    with usage_log.step_identity("verify", "standard"):
        with pytest.raises(LLMError):
            _run(_raising_model())
    (row,) = _rows(target)
    assert row["op"] == "verify"
    assert row["step"] == "verify"
    assert row["model_class"] == "standard"
    assert row["model"] == "test:FunctionModel"
    # No provider backs a FunctionModel double — `test` is not a provider name. `record()` omits
    # an unknown optional identity field rather than writing null, so absence IS the assertion.
    assert "provider" not in row
    assert set(usage_log._FIELDS).issubset(row), "the failure row lacks the token counters"


def test_the_failure_row_is_explicitly_distinguishable_from_a_successful_call(
    monkeypatch, tmp_path
):
    """Burned budget must be separable from delivered work — by an EXPLICIT field on both
    rows, never inferred from something being absent."""
    target = _log(monkeypatch, tmp_path)
    with pytest.raises(LLMError):
        _run(_raising_model())
    _run(_ok_model())
    failed, ok = _rows(target)
    assert failed["outcome"] == usage_log.OUTCOME_FAILED
    assert ok["outcome"] == usage_log.OUTCOME_OK
    assert failed["outcome"] != ok["outcome"]


def test_a_retry_to_exhaustion_records_the_tokens_it_burned(monkeypatch, tmp_path):
    """The expensive failure the ticket cares about: a step-budget exhaustion made several real
    model requests, and every one of them was billed."""
    target = _log(monkeypatch, tmp_path)
    calls = {"n": 0}
    with pytest.raises(LLMError):
        _run(_looping_model(calls))
    assert calls["n"] > 1, "fixture did not actually burn multiple model requests"
    (row,) = _rows(target)
    assert row["requests"] == calls["n"]
    assert row["input_tokens"] > 0, "burned input tokens were not recorded"


# ── no double-counting ────────────────────────────────────────────────────────────────────


def test_a_failing_run_writes_one_row_not_one_per_attempt(monkeypatch, tmp_path):
    """One row PER CALL, not per internal request/retry attempt — otherwise a runaway agent's
    spend would be multiplied by its own step count."""
    target = _log(monkeypatch, tmp_path)
    calls = {"n": 0}
    with pytest.raises(LLMError):
        _run(_looping_model(calls))
    assert calls["n"] > 1
    assert len(_rows(target)) == 1


def test_a_retried_call_is_not_counted_twice_as_delivered_work(monkeypatch, tmp_path):
    """A call that fails and is then retried successfully leaves exactly ONE row attributable
    to delivered work; the burned attempt stays on its own, explicitly-failed row."""
    target = _log(monkeypatch, tmp_path)
    with pytest.raises(LLMError):
        _run(_raising_model())
    _run(_ok_model())
    rows = _rows(target)
    assert len(rows) == 2
    delivered = [r for r in rows if r["outcome"] == usage_log.OUTCOME_OK]
    assert len(delivered) == 1, "the same spend was recorded twice as delivered work"


# ── the standing guarantees ───────────────────────────────────────────────────────────────


def test_a_failed_call_writes_nothing_when_the_log_is_not_configured(monkeypatch, tmp_path):
    """The no-op-when-unset guarantee: every normal library/test run takes this path."""
    monkeypatch.delenv(usage_log.ENV_VAR, raising=False)
    with pytest.raises(LLMError):
        _run(_raising_model())
    assert not list(tmp_path.iterdir())


def test_summarize_still_folds_a_log_containing_a_failure_row(monkeypatch, tmp_path):
    """`summarize()` feeds `$GITHUB_STEP_SUMMARY` on the two billable weekly jobs, so the spend
    total must fold the failure row rather than choke on it."""
    target = _log(monkeypatch, tmp_path)
    with pytest.raises(LLMError):
        _run(_raising_model())
    _run(_ok_model())
    out = usage_log.summarize(str(target))
    assert out != "No LLM calls recorded."
    assert "| **total** | **2** |" in out, "the failed call is missing from the spend total"


def test_recording_the_failure_does_not_convert_or_swallow_the_provider_error(
    monkeypatch, tmp_path
):
    """Telemetry must never break the call path: the original provider failure still surfaces,
    unchanged, through the same `interpret_failure` spine."""
    _log(monkeypatch, tmp_path)
    with pytest.raises(LLMError) as caught:
        _run(_raising_model())
    assert _BOOM in str(caught.value)
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_an_unwritable_log_still_lets_the_provider_error_through(monkeypatch, tmp_path):
    """The discipline `record()` already keeps for its own OSError, held on the failure path
    too: a broken sink degrades telemetry, never the run's error."""
    monkeypatch.setenv(usage_log.ENV_VAR, str(tmp_path / "no" / "such" / "dir" / "u.jsonl"))
    with pytest.raises(LLMError) as caught:
        _run(_raising_model())
    assert _BOOM in str(caught.value)
