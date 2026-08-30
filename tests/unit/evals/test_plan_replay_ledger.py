"""Tests for the eval budget ledger (``rebar.llm.evals.plan_replay.ledger``,
ticket fizzy-hypnotic-boto).

All pricing here goes through a STUBBED ``genai_prices`` module (the same pattern
``test_usage_log_pricing_qualified_2ca9.py`` uses) rather than the real, network-free but
version-drifting pricing table — a stub keeps the priced totals asserted here exact and
independent of the installed price table's contents.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from rebar.llm.evals.plan_replay import ledger

pytestmark = pytest.mark.unit


def _row(model: str, input_tokens: int = 1000, output_tokens: int = 500) -> dict:
    return {
        "model": model,
        "provider": "bedrock",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "timestamp": "2026-07-30T00:00:00+00:00",
    }


def _stub_pricing(monkeypatch, calc_price):
    """Install a fake ``genai_prices`` module with a caller-controlled ``calc_price``."""
    stub = types.ModuleType("genai_prices")

    class Usage:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _Price:
        def __init__(self, total_price):
            self.total_price = total_price

    stub.Usage = Usage
    stub.calc_price = calc_price
    monkeypatch.setitem(sys.modules, "genai_prices", stub)
    return _Price


def _stub_pricing_fixed(monkeypatch, usd_per_row: float):
    class _Price:
        def __init__(self, total_price):
            self.total_price = total_price

    def calc_price(usage, model_ref, provider_id=None, genai_request_timestamp=None):
        return _Price(usd_per_row)

    _stub_pricing(monkeypatch, calc_price)


def _stub_pricing_unresolvable(monkeypatch, bad_models: set[str]):
    class _Price:
        def __init__(self, total_price):
            self.total_price = total_price

    def calc_price(usage, model_ref, provider_id=None, genai_request_timestamp=None):
        if model_ref in bad_models:
            raise LookupError(f"Unable to find model with model_ref={model_ref!r}")
        return _Price(1.0)

    _stub_pricing(monkeypatch, calc_price)


def _ledger_path(tmp_path):
    return str(tmp_path / "ledger.jsonl")


def _ledger_rows(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []


# ── happy path ──────────────────────────────────────────────────────────────────
def test_estimate_computes_flat_per_sample_cost():
    """The flat historical per-sample estimate, not a token-derived prediction."""
    assert ledger.estimate("tier1", 10) == pytest.approx(5.0)
    assert ledger.estimate("tier2", 5) == pytest.approx(17.0)


def test_reserve_succeeds_under_cap(tmp_path):
    path = _ledger_path(tmp_path)
    ledger.reserve(50.0, ledger_path=path)  # must not raise


def test_finalize_prices_resolvable_model_and_appends_ledger_row(tmp_path, monkeypatch):
    _stub_pricing_fixed(monkeypatch, 2.5)
    path = _ledger_path(tmp_path)
    rows = [
        _row("bedrock:us.anthropic.claude-opus-4-8"),
        _row("bedrock:us.anthropic.claude-opus-4-8"),
    ]

    entry = ledger.finalize(
        "run-001",
        "tier1",
        "candidate-a",
        3,
        {"pass1": "bedrock:us.anthropic.claude-opus-4-8"},
        rows,
        ledger_path=path,
    )

    assert entry["usd"] == pytest.approx(5.0)  # 2 rows * $2.50
    stored = _ledger_rows(path)
    assert len(stored) == 1
    assert stored[0]["run_id"] == "run-001"
    assert stored[0]["usd"] == pytest.approx(5.0)
    assert stored[0]["sample_n"] == 3


def test_print_summary_reports_spent_and_remaining(tmp_path, monkeypatch):
    _stub_pricing_fixed(monkeypatch, 10.0)
    path = _ledger_path(tmp_path)
    ledger.finalize("run-001", "tier1", "c", 1, {}, [_row("bedrock:m")], ledger_path=path)

    summary = ledger.print_summary(ledger_path=path)

    assert "10.00" in summary
    assert "200" in summary  # cap appears somewhere in the report


# ── edge: reserve() over cap ──────────────────────────────────────────────────
def test_reserve_refuses_over_cap_naming_remaining_amount(tmp_path):
    path = _ledger_path(tmp_path)
    remaining = ledger.LEDGER_CAP_USD - ledger.LEDGER_RESERVE_USD

    with pytest.raises(ledger.BudgetExceeded) as exc_info:
        ledger.reserve(remaining + 1.0, ledger_path=path)

    assert f"{remaining:.2f}" in str(exc_info.value)


def test_reserve_reads_spent_from_existing_ledger_rows(tmp_path, monkeypatch):
    _stub_pricing_fixed(monkeypatch, 100.0)
    path = _ledger_path(tmp_path)
    ledger.finalize("run-a", "tier1", "c", 1, {}, [_row("bedrock:m")], ledger_path=path)
    ledger.finalize("run-b", "tier1", "c", 1, {}, [_row("bedrock:m")], ledger_path=path)
    # spent = 200; remaining = 200 - 30 - 200 = -30

    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(1.0, ledger_path=path)


# ── edge: estimate() unknown tier ──────────────────────────────────────────────
def test_estimate_rejects_unknown_tier():
    with pytest.raises(ValueError):
        ledger.estimate("tier3", 1)


# ── edge: finalize() loud pricing failure ──────────────────────────────────────
def test_finalize_raises_on_unresolvable_model_never_records_zero(tmp_path, monkeypatch):
    _stub_pricing_unresolvable(monkeypatch, {"mystery-model"})
    path = _ledger_path(tmp_path)
    rows = [_row("mystery-model")]

    with pytest.raises(ledger.UnpriceableRun):
        ledger.finalize("run-bad", "tier1", "c", 1, {}, rows, ledger_path=path)

    assert _ledger_rows(path) == []  # no row written -- never a silent usd=0


# ── edge: reconcile() ───────────────────────────────────────────────────────────
def test_reconcile_finalizes_a_crashed_run_from_partial_rows(tmp_path, monkeypatch):
    _stub_pricing_fixed(monkeypatch, 1.0)
    path = _ledger_path(tmp_path)
    partial_rows = [_row("bedrock:m"), _row("bedrock:m"), _row("bedrock:m")]

    entry = ledger.reconcile("run-crashed", "tier2", "c", 2, {}, partial_rows, ledger_path=path)

    assert entry["usd"] == pytest.approx(3.0)
    assert len(_ledger_rows(path)) == 1


def test_reconcile_is_idempotent_when_run_already_finalized(tmp_path, monkeypatch):
    _stub_pricing_fixed(monkeypatch, 1.0)
    path = _ledger_path(tmp_path)
    ledger.finalize("run-done", "tier1", "c", 1, {}, [_row("bedrock:m")], ledger_path=path)
    assert len(_ledger_rows(path)) == 1

    # A reconcile call with DIFFERENT rows must not double-count or overwrite.
    entry = ledger.reconcile(
        "run-done", "tier1", "c", 1, {}, [_row("bedrock:m"), _row("bedrock:m")], ledger_path=path
    )

    rows_after = _ledger_rows(path)
    assert len(rows_after) == 1
    assert entry["usd"] == pytest.approx(1.0)  # unchanged from the original finalize


# ── edge: constants are single-sourced ──────────────────────────────────────────
def test_reserve_derives_cap_math_from_module_constants(tmp_path, monkeypatch):
    """Patching the module constants changes reserve()'s behavior -- proving reserve()
    reads them live rather than embedding its own copy of 200/30."""
    path = _ledger_path(tmp_path)
    monkeypatch.setattr(ledger, "LEDGER_CAP_USD", 10.0)
    monkeypatch.setattr(ledger, "LEDGER_RESERVE_USD", 0.0)

    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(11.0, ledger_path=path)

    ledger.reserve(9.0, ledger_path=path)  # under the patched cap -- must not raise


# ── E2E: the full reserve -> finalize -> summary -> reserve-again loop ──────────
def test_full_run_lifecycle_reserve_finalize_summary(tmp_path, monkeypatch):
    _stub_pricing_fixed(monkeypatch, 20.0)
    path = _ledger_path(tmp_path)

    est = ledger.estimate("tier2", 5)
    ledger.reserve(est, ledger_path=path)

    ledger.finalize(
        "run-e2e",
        "tier2",
        "candidate-x",
        5,
        {"pass1": "bedrock:m1", "pass2": "bedrock:m2"},
        [_row("bedrock:m1"), _row("bedrock:m2")],
        ledger_path=path,
    )

    summary = ledger.print_summary(ledger_path=path)
    assert "tier2" in summary
    assert "40.00" in summary  # 2 rows * $20 each

    # A second reserve() must see the $40 already spent.
    remaining_after = ledger.LEDGER_CAP_USD - ledger.LEDGER_RESERVE_USD - 40.0
    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(remaining_after + 1.0, ledger_path=path)
    ledger.reserve(remaining_after - 1.0, ledger_path=path)  # must not raise


def test_print_summary_breaks_down_by_tier(tmp_path, monkeypatch):
    _stub_pricing_fixed(monkeypatch, 5.0)
    path = _ledger_path(tmp_path)
    ledger.finalize("run-t1", "tier1", "c", 1, {}, [_row("bedrock:m")], ledger_path=path)
    ledger.finalize("run-t2a", "tier2", "c", 1, {}, [_row("bedrock:m")], ledger_path=path)
    ledger.finalize("run-t2b", "tier2", "c", 1, {}, [_row("bedrock:m")], ledger_path=path)

    summary = ledger.print_summary(ledger_path=path)

    assert "tier1" in summary
    assert "tier2" in summary
    assert "5.00" in summary  # tier1 total
    assert "10.00" in summary  # tier2 total (2 runs * $5)
