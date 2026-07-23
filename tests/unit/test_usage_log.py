"""Offline tests for the token-usage sink (``rebar.llm.usage_log``) and its runner seam.

No live/billable call: the seam test drives ``PydanticAIRunner.run`` through the offline
``FunctionModel`` override with ``ALLOW_MODEL_REQUESTS = False`` (mirrors
``tests/unit/test_llm_temperature.py``).
"""

from __future__ import annotations

import json

import pytest

from rebar.llm import usage_log

pytestmark = pytest.mark.unit


# ── record() ──────────────────────────────────────────────────────────────────
def test_record_noop_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(usage_log.ENV_VAR, raising=False)
    target = tmp_path / "usage.jsonl"
    usage_log.record({"input_tokens": 5}, op="x")
    assert not target.exists()


def test_record_noop_on_empty_usage(tmp_path, monkeypatch):
    target = tmp_path / "usage.jsonl"
    monkeypatch.setenv(usage_log.ENV_VAR, str(target))
    usage_log.record({}, op="x")
    assert not target.exists()


def test_record_appends_jsonl(tmp_path, monkeypatch):
    target = tmp_path / "usage.jsonl"
    monkeypatch.setenv(usage_log.ENV_VAR, str(target))
    usage_log.record({"input_tokens": 5, "output_tokens": 3, "requests": 1}, op="plan-reviewer")
    usage_log.record(
        {"input_tokens": 7, "output_tokens": 2, "cache_read_tokens": 4}, op="completion-verifier"
    )
    lines = target.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["op"] == "plan-reviewer"
    assert first["input_tokens"] == 5
    assert first["output_tokens"] == 3
    assert first["requests"] == 1
    # A missing field defaults to 0.
    assert first["cache_read_tokens"] == 0


# ── summarize() ───────────────────────────────────────────────────────────────
def test_summarize_missing_file():
    assert usage_log.summarize("/no/such/file.jsonl") == "No LLM calls recorded."


def test_summarize_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert usage_log.summarize(str(path)) == "No LLM calls recorded."


def test_summarize_aggregates(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text(
        json.dumps({"op": "a", "input_tokens": 10, "output_tokens": 2, "requests": 1})
        + "\n"
        + json.dumps({"op": "a", "input_tokens": 5, "output_tokens": 1, "requests": 1})
        + "\n"
        + json.dumps({"op": "b", "input_tokens": 3, "output_tokens": 3, "requests": 2})
        + "\n"
    )
    out = usage_log.summarize(str(path))
    assert "LLM token usage" in out
    # per-op fold for `a`: 2 calls, input 15, output 3
    assert "| a | 2 | 15 | 3 |" in out
    # totals row: 3 calls, input 18, output 6, requests 4
    assert "| **total** | **3** | **18** | **6** |" in out
    assert "**4** |" in out  # requests total


def test_summarize_skips_malformed_line(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text(json.dumps({"op": "a", "input_tokens": 1}) + "\nnot-json\n")
    out = usage_log.summarize(str(path))
    assert "| a | 1 |" in out


# ── CLI ───────────────────────────────────────────────────────────────────────
def test_cli_summarize(tmp_path, capsys):
    path = tmp_path / "usage.jsonl"
    path.write_text(
        json.dumps({"op": "a", "input_tokens": 4, "output_tokens": 1, "requests": 1}) + "\n"
    )
    rc = usage_log.main(["summarize", str(path)])
    assert rc == 0
    assert "LLM token usage" in capsys.readouterr().out


# ── the runner records at the _usage seam (offline FunctionModel) ─────────────
def test_runner_records_usage_at_seam(tmp_path, monkeypatch):
    pytest.importorskip("pydantic_ai")
    import pydantic_ai.models
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    target = tmp_path / "usage.jsonl"
    monkeypatch.setenv(usage_log.ENV_VAR, str(target))
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

    def gen(messages, info):
        return ModelResponse(parts=[TextPart("hi")])

    cfg = LLMConfig(repo_path=".")
    req = RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], mode="text")
    PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)

    assert target.exists()
    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["op"] == "v"  # _call_label = reviewers joined
    assert set(usage_log._FIELDS).issubset(rows[0])
