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


# ── record() metadata: model / provider / timestamp ───────────────────────────
def test_record_writes_model_provider_timestamp(tmp_path, monkeypatch):
    target = tmp_path / "usage.jsonl"
    monkeypatch.setenv(usage_log.ENV_VAR, str(target))
    usage_log.record(
        {"input_tokens": 5, "requests": 1},
        op="plan-reviewer",
        model="anthropic:claude-opus-4-8",
        provider="anthropic",
    )
    row = json.loads(target.read_text().splitlines()[0])
    assert row["model"] == "anthropic:claude-opus-4-8"
    assert row["provider"] == "anthropic"
    # UTC ISO-8601, parseable, and explicitly offset-aware.
    from datetime import datetime, timezone

    ts = datetime.fromisoformat(row["timestamp"])
    assert ts.tzinfo is not None
    assert ts.utcoffset() == timezone.utc.utcoffset(None)


def test_record_defaults_omit_model_provider(tmp_path, monkeypatch):
    target = tmp_path / "usage.jsonl"
    monkeypatch.setenv(usage_log.ENV_VAR, str(target))
    usage_log.record({"input_tokens": 1}, op="x")
    row = json.loads(target.read_text().splitlines()[0])
    assert "model" not in row
    assert "provider" not in row
    assert "timestamp" in row


# ── the dict-to-kwargs pricing adapter ────────────────────────────────────────
def test_usage_kwargs_maps_all_four_token_fields():
    row = {
        "op": "a",
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_tokens": 7,
        "cache_write_tokens": 3,
        "requests": 1,
        "model": "m",
    }
    kwargs = usage_log.usage_kwargs(row)
    assert kwargs == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_tokens": 7,
        "cache_write_tokens": 3,
    }


def test_usage_kwargs_defaults_missing_fields_to_zero():
    assert usage_log.usage_kwargs({"input_tokens": 4}) == {
        "input_tokens": 4,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert usage_log.usage_kwargs({"output_tokens": None}) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


# ── summarize() pricing (stubbed genai_prices; never a hard test dependency) ──
def _stub_pricing_module(monkeypatch, calc_price):
    """Install a fake ``genai_prices`` module exposing Usage + calc_price."""
    import sys
    import types

    stub = types.ModuleType("genai_prices")

    class Usage:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    stub.Usage = Usage
    stub.calc_price = calc_price
    monkeypatch.setitem(sys.modules, "genai_prices", stub)
    return stub


class _Price:
    def __init__(self, total):
        self.total_price = total


def test_summarize_prices_known_rows(tmp_path, monkeypatch):
    def calc_price(usage, model_ref, provider_id=None, genai_request_timestamp=None):
        assert usage.kwargs["input_tokens"] in (1000, 2000)
        assert model_ref == "claude-opus-4-8"
        assert provider_id == "anthropic"
        assert genai_request_timestamp is not None
        return _Price(0.05)

    _stub_pricing_module(monkeypatch, calc_price)
    path = tmp_path / "usage.jsonl"
    ts = "2026-07-30T00:00:00+00:00"
    path.write_text(
        json.dumps(
            {
                "op": "a",
                "input_tokens": 1000,
                "requests": 1,
                "model": "claude-opus-4-8",
                "provider": "anthropic",
                "timestamp": ts,
            }
        )
        + "\n"
        + json.dumps(
            {
                "op": "a",
                "input_tokens": 2000,
                "requests": 1,
                "model": "claude-opus-4-8",
                "provider": "anthropic",
                "timestamp": ts,
            }
        )
        + "\n"
    )
    out = usage_log.summarize(str(path))
    assert "est. cost" in out
    assert "$0.1000" in out  # per-op + total: 2 rows x 0.05
    # per-model rollup table
    assert "cost by model" in out
    assert "claude-opus-4-8" in out
    assert "unpriced" not in out


def test_summarize_marks_unknown_model_unpriced(tmp_path, monkeypatch, caplog):
    def calc_price(usage, model_ref, provider_id=None, genai_request_timestamp=None):
        if model_ref == "mystery-model":
            raise LookupError("unknown model")
        return _Price(0.01)

    _stub_pricing_module(monkeypatch, calc_price)
    path = tmp_path / "usage.jsonl"
    path.write_text(
        json.dumps({"op": "a", "input_tokens": 1, "model": "known", "requests": 1})
        + "\n"
        + json.dumps({"op": "a", "input_tokens": 1, "model": "mystery-model", "requests": 1})
        + "\n"
    )
    import logging

    with caplog.at_level(logging.WARNING, logger="rebar.llm.usage_log"):
        out = usage_log.summarize(str(path))
    assert "excludes 1 unpriced call" in out
    assert "$0.0100" in out
    # LookupError is the expected unknown-model signal: no WARNING for it.
    assert not caplog.records


def test_summarize_pricing_crash_warns_and_marks_unpriced(tmp_path, monkeypatch, caplog):
    def calc_price(usage, model_ref, provider_id=None, genai_request_timestamp=None):
        raise ValueError("boom")

    _stub_pricing_module(monkeypatch, calc_price)
    path = tmp_path / "usage.jsonl"
    path.write_text(json.dumps({"op": "a", "input_tokens": 1, "model": "m", "requests": 1}) + "\n")
    import logging

    with caplog.at_level(logging.WARNING, logger="rebar.llm.usage_log"):
        out = usage_log.summarize(str(path))
    assert "excludes 1 unpriced call" in out
    assert any("boom" in r.getMessage() for r in caplog.records)


def test_summarize_without_genai_prices_prints_unavailable(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.setitem(sys.modules, "genai_prices", None)  # forces ImportError
    path = tmp_path / "usage.jsonl"
    path.write_text(
        json.dumps({"op": "a", "input_tokens": 4, "output_tokens": 1, "requests": 1}) + "\n"
    )
    out = usage_log.summarize(str(path))
    assert "unavailable (install rebar[pricing])" in out
    assert "| a | 1 | 4 | 1 |" in out  # token totals still printed
    # exits 0 via the CLI too
    assert usage_log.main(["summarize", str(path)]) == 0


def test_summarize_old_format_rows_still_summarize(tmp_path, monkeypatch):
    def calc_price(usage, model_ref, provider_id=None, genai_request_timestamp=None):
        return _Price(0.01)

    _stub_pricing_module(monkeypatch, calc_price)
    path = tmp_path / "usage.jsonl"
    # Pre-pricing rows: no model/provider/timestamp.
    path.write_text(
        json.dumps({"op": "a", "input_tokens": 10, "output_tokens": 2, "requests": 1}) + "\n"
    )
    out = usage_log.summarize(str(path))
    assert "| a | 1 | 10 | 2 |" in out
    assert "excludes 1 unpriced call" in out


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
    # The runner call site passes model + inferred provider; record() stamps the time.
    assert rows[0]["model"] == "test:FunctionModel"
    # `test` is not a provider name (pydantic-ai builds a TestModel from the bare string `test`
    # and rejects `test:` as a qualifier), so there is no provider behind a FunctionModel double.
    # `record()` omits an unknown optional identity field rather than writing null, so "no
    # provider" is the ABSENCE of the key — the same convention as `model`/`step`/`model_class`.
    assert "provider" not in rows[0]
    assert "timestamp" in rows[0]
