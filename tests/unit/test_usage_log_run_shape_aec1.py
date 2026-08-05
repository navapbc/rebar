"""A gate run must durably record its RUN SHAPE — tool calls, not just requests (bug aec1).

``failure_usage()`` already reduces the accumulated pydantic-ai messages to the full
loop-versus-breadth signal — ``tool_calls``, ``tool_calls_distinct``,
``max_consecutive_repeat``, ``top_repeated_tool_calls``, plus both limits. ``record()`` then
built its row from ``_FIELDS`` alone, which carries ``requests`` and the four token counters
and nothing else, so **all seven of those keys were computed and discarded one frame from the
write**. A 125-call/1-distinct loop landed on disk indistinguishable from 125 requests of
genuine breadth, which is precisely the discrimination the gate's step budget was raised five
times without.

Two further gaps in the same defect: the SUCCESS path never computed the shape at all
(``_extract_usage`` returns five keys), and neither row carried wall-clock duration.

Every test here drives the REAL runner and then READS THE FILE, because the defect is about
what reaches the file — asserting on ``failure_usage()``'s return in isolation is exactly what
the pre-aec1 suite did (``test_usage_log_repetition_a89d.py``) and it could not see the drop.
The fixture deliberately VARIES THE PATH AXIS — success, breadth, and budget exhaustion — since
a success-only fixture would be vacuous for a ticket whose whole point is that the failure path
is the case with no data. Offline throughout: a ``FunctionModel`` override with
``ALLOW_MODEL_REQUESTS = False`` (the seam ``test_usage_log_failed_calls.py`` established).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from rebar.llm import usage_log  # noqa: E402
from rebar.llm.config import LLMConfig, gate_session  # noqa: E402
from rebar.llm.errors import LLMError  # noqa: E402
from rebar.llm.runner import PydanticAIRunner, RunRequest  # noqa: E402

pytestmark = pytest.mark.unit


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


def _run(model, *, ticket_id: str | None = None):
    cfg = LLMConfig(repo_path=".")
    req = RunRequest(
        system_prompt="s",
        instructions="i",
        config=cfg,
        reviewers=["verify"],
        mode="text",
        target={"ticket_id": ticket_id} if ticket_id else {},
    )
    return PydanticAIRunner(cfg, model_override=model).run(req)


def _looping_model(calls: dict):
    """Never terminates, and repeats ONE signature: burns budget in a pure loop."""

    def loop(messages, info: AgentInfo):
        calls["n"] += 1
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.function_tools[0].name, args={"path": "."})]
        )

    return FunctionModel(loop)


def _breadth_model(steps: int):
    """Terminates after ``steps`` DISTINCT tool calls: genuine exploration, then an answer."""
    seen = {"n": 0}

    def breadth(messages, info: AgentInfo):
        seen["n"] += 1
        if seen["n"] <= steps:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=info.function_tools[0].name, args={"path": f"./p{seen['n']}"}
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(breadth)


# ── the defect: the shape the reducer computes must survive the write ─────────────────────


def test_the_exhaustion_row_records_the_tool_calls_it_burned(monkeypatch, tmp_path):
    """The case with NO data today. A budget exhaustion is exactly when the numbers are
    needed, and `tool_calls` is the second, independent kill path (`tool_calls_limit` is 2x
    `request_limit`), so a row carrying `requests` alone cannot say which ceiling was hit."""
    target = _log(monkeypatch, tmp_path)
    calls = {"n": 0}
    with pytest.raises(LLMError):
        _run(_looping_model(calls))
    assert calls["n"] > 1, "fixture did not actually burn multiple model requests"
    (row,) = _rows(target)
    assert row["outcome"] == usage_log.OUTCOME_FAILED
    assert row["tool_calls"] == calls["n"], "the burned tool calls never reached the file"
    assert row["request_limit"] > 0, "the applicable request limit was not recorded"
    assert row["tool_calls_limit"] > 0, "the applicable tool-call limit was not recorded"


def test_the_success_row_records_the_tool_call_shape_too(monkeypatch, tmp_path):
    """The success path used to compute no shape at all (`_extract_usage` returns five keys),
    so a clean run could not be used as the control group a budget decision needs."""
    target = _log(monkeypatch, tmp_path)
    _run(_breadth_model(3))
    (row,) = _rows(target)
    assert row["outcome"] == usage_log.OUTCOME_OK
    assert row["tool_calls"] == 3, "a SUCCESSFUL run recorded no tool-call count"
    assert row["tool_calls_distinct"] == 3


def test_a_loop_and_a_breadth_run_are_separable_from_the_record_alone(monkeypatch, tmp_path):
    """THE acceptance criterion, and the reason the axis must vary: two runs that look
    identical in `requests` must be told apart by the recorded distinct-vs-total ratio,
    WITHOUT re-running either. The measured live examples this reproduces are 238/258 = 0.922
    (healthy) against 76/257 = 0.296, 167/270 = 0.619 and 135/264 = 0.511 (pathological)."""
    target = _log(monkeypatch, tmp_path)
    _run(_breadth_model(5))
    with pytest.raises(LLMError):
        _run(_looping_model({"n": 0}))
    breadth, loop = _rows(target)

    def ratio(row):
        return row["tool_calls_distinct"] / row["tool_calls"]

    assert ratio(breadth) == 1.0, "an all-distinct exploration did not read as breadth"
    assert ratio(loop) < 0.1, "a single-signature loop did not read as a loop"
    assert loop["max_consecutive_repeat"] == loop["tool_calls"]
    assert loop["top_repeated_tool_calls"], "the repeated signature was not named"


def test_both_paths_record_wall_clock_duration_and_the_ticket(monkeypatch, tmp_path):
    """Duration and ticket id are two of the five things the ticket asks to be queryable, and
    a spend record that cannot be attributed to a ticket cannot be aggregated per epic."""
    target = _log(monkeypatch, tmp_path)
    _run(_breadth_model(1), ticket_id="aec1-76e7-bb95-4c86")
    with pytest.raises(LLMError):
        _run(_looping_model({"n": 0}), ticket_id="aec1-76e7-bb95-4c86")
    for row in _rows(target):
        assert isinstance(row["duration_s"], (int, float))
        assert row["duration_s"] >= 0.0
        assert row["ticket"] == "aec1-76e7-bb95-4c86"


# ── retrievability: a gate run must record WITHOUT the operator instrumenting it ──────────


def test_a_gate_run_records_with_no_env_var_set(monkeypatch, tmp_path):
    """A default-off recorder is functionally the same defect as no recorder: it is why five
    budget raises happened blind. Inside a gate session the sink defaults to the repo-local
    `.rebar/usage.jsonl`, so the operator retrieves a run they never instrumented."""
    monkeypatch.delenv(usage_log.ENV_VAR, raising=False)
    monkeypatch.setattr(usage_log, "_repo_root_for_default_sink", lambda: str(tmp_path))
    (tmp_path / ".rebar").mkdir()
    with gate_session():
        with pytest.raises(LLMError):
            _run(_looping_model({"n": 0}))
    rows = _rows(tmp_path / ".rebar" / "usage.jsonl")
    assert rows, "a gate run recorded nothing without REBAR_USAGE_LOG — still un-tunable"
    assert rows[0]["tool_calls"] > 1


def test_a_non_gate_run_still_writes_nothing_when_unset(monkeypatch, tmp_path):
    """The control in the other direction: the no-op-when-unset guarantee is preserved for
    exactly the callers it was written for — library consumers, spec scans, ordinary tests.
    Without this, the previous test would pass for the trivial reason that everything writes."""
    monkeypatch.delenv(usage_log.ENV_VAR, raising=False)
    monkeypatch.setattr(usage_log, "_repo_root_for_default_sink", lambda: str(tmp_path))
    (tmp_path / ".rebar").mkdir()
    with pytest.raises(LLMError):
        _run(_looping_model({"n": 0}))
    assert not (tmp_path / ".rebar" / "usage.jsonl").exists(), (
        "a non-gate call wrote a spend row — the no-op-when-unset guarantee regressed"
    )


def test_summarize_still_folds_rows_carrying_the_new_shape(monkeypatch, tmp_path):
    """`summarize()` feeds `$GITHUB_STEP_SUMMARY` on the billable weekly jobs; the added keys
    must not choke it, and the retrieval command in the ticket is exactly this call."""
    target = _log(monkeypatch, tmp_path)
    _run(_breadth_model(2))
    with pytest.raises(LLMError):
        _run(_looping_model({"n": 0}))
    out = usage_log.summarize(str(target))
    assert "| **total** | **2** |" in out
