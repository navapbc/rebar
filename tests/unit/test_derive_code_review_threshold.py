"""Unit coverage for the 9f25 code-review threshold derivation (precision-over-priority).
Offline, on a synthetic fixture — the derivation LOGIC is a pure function; the live run over
the real code_review_result_v1 sidecar corpus produces the committed recommendation artifact."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "derive_code_review_threshold",
    Path(__file__).resolve().parents[2] / "docs/experiments/derive_code_review_threshold.py",
)
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)  # type: ignore[union-attr]


def test_precision_at_threshold() -> None:
    # rows: (priority, adjudication). precision(block-worthy | priority >= t)
    rows = [
        {"priority": 0.60, "adjudication": "block-worthy"},
        {"priority": 0.54, "adjudication": "block-worthy"},
        {"priority": 0.54, "adjudication": "not-block-worthy"},
        {"priority": 0.32, "adjudication": "not-block-worthy"},
        {"priority": 0.20, "adjudication": "block-worthy"},
        {"priority": 0.10, "adjudication": "ambiguous"},  # excluded from precision
    ]
    # at t=0.54: qualifying non-ambiguous = 3 (0.60 bw, 0.54 bw, 0.54 nbw) → precision 2/3 ≈ 0.667
    assert abs(_mod.precision_at(rows, 0.54) - (2 / 3)) < 1e-9
    # at t=0.60: qualifying = 1 (0.60 bw) → precision 1.0
    assert _mod.precision_at(rows, 0.60) == 1.0


def test_select_smallest_threshold_above_half() -> None:
    rows = [
        {"priority": 0.60, "adjudication": "block-worthy"},
        {"priority": 0.54, "adjudication": "block-worthy"},
        {"priority": 0.54, "adjudication": "not-block-worthy"},
        {"priority": 0.40, "adjudication": "not-block-worthy"},
        {"priority": 0.40, "adjudication": "not-block-worthy"},
    ]
    # smallest t in the candidate grid with precision > 0.5:
    #   t=0.4 → 5 qualify, 2 bw → 0.40 (NOT > 0.5)
    #   t=0.5 → the two 0.40 nbw rows drop out; 3 qualify, 2 bw → 0.667 (> 0.5) ← smallest
    chosen = _mod.select_threshold(rows, candidates=[0.3, 0.4, 0.5, 0.54, 0.6])
    assert chosen == 0.5


def test_ambiguous_excluded_from_denominator() -> None:
    rows = [
        {"priority": 0.60, "adjudication": "ambiguous"},
        {"priority": 0.60, "adjudication": "block-worthy"},
    ]
    # only the block-worthy counts in the denominator at t=0.60 → precision 1.0
    assert _mod.precision_at(rows, 0.60) == 1.0
