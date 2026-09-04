"""A gate run must durably record its RUN SHAPE — tool calls, not just requests (bug aec1).

``run_shape()`` already reduces the accumulated pydantic-ai messages to the full
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
what reaches the file — asserting on ``run_shape()``'s return in isolation is exactly what
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

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rebar.llm import usage_log
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMError
from rebar.llm.gate_context import gate_session
from rebar.llm.runner import PydanticAIRunner, RunRequest

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
        target={"ticket_ids": [ticket_id]} if ticket_id else {},
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


def test_the_success_row_carries_the_limits_and_duration_it_ran_under(monkeypatch, tmp_path):
    """The Step-2 plumbing gap, pinned explicitly.

    `record_call_spend` receives neither the limits nor the call start time, so on the SUCCESS
    path these three arrive by a different route than on the failure path: the limits are
    ARGUMENTS to the `run_shape(...)` reducer call (which echoes them into its result, whence
    `shape_only` carries them into the row), and `duration_s` is computed in the runner from its
    own `time.monotonic()` marker. Nothing else in this file would fail if that plumbing
    regressed to failure-path-only, which is exactly how the gap would survive a green suite."""
    target = _log(monkeypatch, tmp_path)
    _run(_breadth_model(2))
    (row,) = _rows(target)
    assert row["outcome"] == usage_log.OUTCOME_OK
    assert row["request_limit"] > 0, "the success row lost the request limit it ran under"
    assert row["tool_calls_limit"] > 0, "the success row lost the tool-call limit it ran under"
    assert isinstance(row["duration_s"], (int, float)) and row["duration_s"] >= 0.0


def test_success_shape_capture_swallows_reducer_failure_and_warns(monkeypatch, caplog):
    """The success-path shape capture (extracted into `_merge_success_run_shape`) is telemetry:
    a failure in `run_shape`/`shape_only` must NEVER break the call path. It is swallowed with a
    single warning and leaves the authoritative `usage` dict untouched, so a reducer bug can
    never corrupt the billable figures `_extract_usage` already placed there."""
    from rebar.llm import runner_support

    def _boom(*_a, **_k):
        raise RuntimeError("reducer exploded")

    monkeypatch.setattr(usage_log, "run_shape", _boom)
    usage = {"input_tokens": 11, "output_tokens": 7}
    before = dict(usage)
    with caplog.at_level("WARNING"):
        runner_support._merge_success_run_shape(
            usage, [], request_limit=5, tool_calls_limit=8, call_label="op-x"
        )
    assert usage == before, "a telemetry failure must not mutate the authoritative usage dict"
    assert any(
        "run-shape capture failed" in rec.getMessage() and "op-x" in rec.getMessage()
        for rec in caplog.records
    ), "the swallowed reducer failure was not logged with the op label"


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


# ── the retrieval command must DISPLAY the signal, not merely carry it ────────────────────


def test_the_retrieval_command_renders_the_loop_signal(monkeypatch, tmp_path):
    """AC4, and the reason it is not satisfied by the row alone.

    `summarize()` IS the named retrieval command (`python -m rebar.llm.usage_log summarize`).
    Before this it folded only the token `_FIELDS`, so a row could carry
    `tool_calls=125, tool_calls_distinct=1` and the operator running the documented command
    still saw nothing but token counters — the signal written and never displayed, which is the
    same "computed then discarded" defect one layer out. Asserting on the RENDERED output is the
    whole point; asserting on the parsed row would pass while the operator still saw nothing."""
    target = _log(monkeypatch, tmp_path)
    with pytest.raises(LLMError):
        _run(_looping_model({"n": 0}))
    out = usage_log.summarize(str(target))
    assert "Run shape (loop vs breadth)" in out
    assert "tool_calls" in out, "the retrieval command does not display the tool-call count"
    assert "distinct" in out, "the retrieval command does not display the distinct count"
    # The loop must be READABLE as a loop from this text: many calls, one signature, ratio ~0.
    # Slice to the run-shape SECTION first — the token table above it also has a `| verify |`
    # row, and matching that one silently reads token counts as tool-call counts.
    section = out.split("#### Run shape (loop vs breadth)", 1)[1]
    body = [ln for ln in section.splitlines() if ln.startswith("| verify |")]
    assert body, "the looping run produced no run-shape row"
    cells = [c.strip() for c in body[0].strip("|").split("|")]
    _op, _calls, total, distinct, ratio = cells[0], cells[1], cells[2], cells[3], cells[4]
    assert int(total) > 1 and int(distinct) == 1
    assert float(ratio) < 0.1, f"a pure loop rendered as ratio {ratio}, which reads as breadth"


def test_rendering_the_run_shape_leaves_the_token_table_untouched(monkeypatch, tmp_path):
    """The additive requirement. That table feeds `$GITHUB_STEP_SUMMARY` on the two billable
    weekly jobs and several existing tests assert on it, so the new section must be appended,
    never merged into it as extra columns."""
    target = _log(monkeypatch, tmp_path)
    _run(_breadth_model(2))
    out = usage_log.summarize(str(target))
    header, sep = out.splitlines()[2], out.splitlines()[3]
    assert header.startswith(
        "| op | calls | input | output | cache_read | cache_write | requests |"
    )
    assert "tool_calls" not in header, "the run shape leaked into the token table's columns"
    assert set(sep.replace(" ", "")) <= set("|-:")
    assert "### LLM token usage" in out


def test_a_pre_aec1_log_with_no_shape_fields_summarizes_unchanged(tmp_path):
    """Every row written before this change carries no shape fields at all. Such a log must
    render exactly as it always did — no empty section, no zero-filled table implying those runs
    made no tool calls when the truth is that nobody measured."""
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps(
            {
                "op": "legacy",
                "outcome": "ok",
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "requests": 1,
            }
        )
        + "\n"
    )
    out = usage_log.summarize(str(path))
    assert "### LLM token usage" in out
    assert "Run shape" not in out, "a log that measured no shape rendered a shape section anyway"
