"""Exhaustive unit shard for the pure compaction fold predicate.

This is the mutation-testing kill-suite for ``src/rebar/_commands/_compact_policy.py``
(story 25aa): it pins every operator, branch, and constant in ``is_foldable`` so a
mutmut mutation of any of them is caught. See docs/mutation-testing.md.
"""

from __future__ import annotations

import pytest

from rebar._commands._compact_policy import is_foldable


@pytest.mark.unit
class TestIsFoldable:
    # ── horizon <= 0 folds EVERYTHING (pre-RC2b / offline default) ──────────────
    @pytest.mark.parametrize("horizon", [0, -1, -1_000_000])
    @pytest.mark.parametrize("ts", [None, 0, 100, 10**18])
    def test_nonpositive_horizon_always_foldable(self, ts, horizon):
        # Even a ts of None (unknown age) and even a "future" event fold when the
        # horizon disables the age check. Kills `horizon <= 0` -> `< 0` / `== 0`
        # and the short-circuit `or`.
        assert is_foldable(ts, now=50, horizon=horizon) is True

    # ── positive horizon: ts is None is NEVER foldable ─────────────────────────
    @pytest.mark.parametrize("now", [0, 100, 10**18])
    def test_none_ts_not_foldable_under_positive_horizon(self, now):
        # Kills mutations that drop the `ts is not None` guard (which would raise or
        # wrongly fold) and the `and`.
        assert is_foldable(None, now=now, horizon=10) is False

    # ── the age boundary: now - ts >= horizon ──────────────────────────────────
    def test_boundary_equal_is_foldable(self):
        # now - ts == horizon exactly -> foldable. Kills `>=` -> `>`.
        assert is_foldable(ts=100, now=200, horizon=100) is True

    def test_boundary_one_below_not_foldable(self):
        # now - ts == horizon - 1 -> NOT foldable. Kills `>=` -> `>` in the other
        # direction and any off-by-one constant drift.
        assert is_foldable(ts=101, now=200, horizon=100) is False

    def test_boundary_one_above_is_foldable(self):
        # now - ts == horizon + 1 -> foldable.
        assert is_foldable(ts=99, now=200, horizon=100) is True

    def test_large_gap_is_foldable(self):
        assert is_foldable(ts=1, now=10**18, horizon=100) is True

    def test_tiny_gap_not_foldable(self):
        # now - ts == 1, horizon == 100 -> not old enough.
        assert is_foldable(ts=199, now=200, horizon=100) is False

    def test_zero_gap_not_foldable_under_positive_horizon(self):
        # now == ts, positive horizon -> age 0 < horizon -> not foldable. Guards
        # against `-` -> `+` (which would make 200+200=400 >= 100 wrongly fold).
        assert is_foldable(ts=200, now=200, horizon=100) is False

    def test_negative_gap_not_foldable(self):
        # A "future" event (now < ts) under a positive horizon is not foldable;
        # now - ts is negative, which is < horizon.
        assert is_foldable(ts=500, now=200, horizon=100) is False

    def test_subtraction_order_matters(self):
        # Distinguishes `now - ts` from `ts - now`: here now - ts = 100 (foldable)
        # but ts - now = -100 (would be not foldable). Kills operand-swap mutants.
        assert is_foldable(ts=100, now=200, horizon=50) is True

    def test_horizon_of_one(self):
        assert is_foldable(ts=100, now=101, horizon=1) is True
        assert is_foldable(ts=100, now=100, horizon=1) is False
