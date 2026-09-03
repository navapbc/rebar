"""Tests for the parameterized eval-budget-ledger cap (ticket fb55-04d6-4beb-4025).

These exercise the real :mod:`rebar.llm.evals.plan_replay.ledger` functions against a
``tmp_path`` ledger file only -- no model call, no network -- so every assertion is
deterministic. They pin: the parameterized refusal boundary, the unchanged $200/$30
defaults, the two new estimate tiers (and the unknown-tier error), and that
``print_summary`` reports headroom from the supplied cap rather than the module global.
"""

from __future__ import annotations

import pytest

from rebar.llm.evals.plan_replay import ledger

pytestmark = pytest.mark.unit


def _empty_ledger(tmp_path) -> str:
    path = tmp_path / "ledger.jsonl"
    path.write_text("", encoding="utf-8")
    return str(path)


def test_reserve_honors_supplied_cap_and_reserve_boundary(tmp_path):
    """cap_usd=50, reserve_usd=10 => remaining 40.0: 40.01 refused, 40.0 admitted."""
    path = _empty_ledger(tmp_path)
    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(40.01, ledger_path=path, cap_usd=50.0, reserve_usd=10.0)
    assert ledger.reserve(40.0, ledger_path=path, cap_usd=50.0, reserve_usd=10.0) is None


def test_reserve_defaults_unchanged_at_200_over_30(tmp_path):
    """With the new keywords omitted the $200 cap / $30 reserve still apply."""
    path = _empty_ledger(tmp_path)
    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(170.01, ledger_path=path)
    assert ledger.reserve(170.0, ledger_path=path) is None


def test_new_estimate_tiers_and_unknown_tier():
    assert ledger.estimate("criteria-eval-cheap", 24) == 0.72
    assert ledger.estimate("criteria-eval-agent", 24) == 6.0
    with pytest.raises(ValueError):
        ledger.estimate("no-such-tier", 24)


def test_print_summary_headroom_from_supplied_cap(tmp_path):
    path = _empty_ledger(tmp_path)
    out = ledger.print_summary(ledger_path=path, cap_usd=50.0)
    assert "remaining: $50.00" in out
    assert "cap:       $50.00" in out
    assert "$200.00" not in out


def test_print_summary_default_cap_still_200(tmp_path):
    path = _empty_ledger(tmp_path)
    out = ledger.print_summary(ledger_path=path)
    assert "cap:       $200.00" in out
