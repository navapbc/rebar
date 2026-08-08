"""Unit tests for the Mode enum in src/rebar/_engine/rebar_reconciler/mode.py."""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
MODE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mode.py"

_spec = importlib.util.spec_from_file_location("rebar_reconciler_mode_under_test", MODE_PATH)
_mode_mod = importlib.util.module_from_spec(_spec)
sys.modules["rebar_reconciler_mode_under_test"] = _mode_mod
_spec.loader.exec_module(_mode_mod)
Mode = _mode_mod.Mode
MODE_CAPS = _mode_mod.MODE_CAPS


def test_from_str_accepts_known_mode():
    """Mode.from_str must round-trip all known mode strings."""
    assert Mode.from_str("bootstrap-strict") == Mode.BOOTSTRAP_STRICT
    assert Mode.from_str("dry-run") == Mode.DRY_RUN
    assert Mode.from_str("bootstrap-throttle") == Mode.BOOTSTRAP_THROTTLE
    assert Mode.from_str("live") == Mode.LIVE


def test_from_str_rejects_unknown_and_names_allowed_set():
    """ValueError for unknown mode must list ALL four allowed values verbatim."""
    with pytest.raises(ValueError) as exc_info:
        Mode.from_str("not-a-mode")
    message = str(exc_info.value)
    for allowed in ("dry-run", "bootstrap-strict", "bootstrap-throttle", "live"):
        assert allowed in message, (
            f"Expected allowed value {allowed!r} in error message, got: {message!r}"
        )


def test_mode_has_exactly_five_members():
    """Mode enum must contain exactly the five rollout-safety modes."""
    assert {m.value for m in Mode} == {
        "reconcile-check",
        "dry-run",
        "bootstrap-strict",
        "bootstrap-throttle",
        "live",
    }


def test_mode_caps_preserve_all_five_legacy_limits():
    """MODE_CAPS remains the exact compatibility contract for every mode."""
    assert MODE_CAPS == {
        Mode.RECONCILE_CHECK: 0,
        Mode.DRY_RUN: 0,
        Mode.BOOTSTRAP_STRICT: 10,
        Mode.BOOTSTRAP_THROTTLE: 100,
        Mode.LIVE: None,
    }


def test_mode_ordering_dry_run_special():
    """dry-run has a lower rank than any operational mode."""
    assert Mode.DRY_RUN.rank() < Mode.BOOTSTRAP_STRICT.rank()
    assert Mode.DRY_RUN.rank() < Mode.BOOTSTRAP_THROTTLE.rank()
    assert Mode.DRY_RUN.rank() < Mode.LIVE.rank()


def test_mode_ordering_bootstrap_strict_less_than_bootstrap_throttle():
    """bootstrap-strict is ordered before bootstrap-throttle."""
    assert Mode.BOOTSTRAP_STRICT.rank() < Mode.BOOTSTRAP_THROTTLE.rank()


def test_mode_ordering_bootstrap_throttle_less_than_live():
    """bootstrap-throttle is ordered before live."""
    assert Mode.BOOTSTRAP_THROTTLE.rank() < Mode.LIVE.rank()


def test_mode_ordering_supports_comparison():
    """Modes support > comparison semantics for check_phase_gate."""
    # live > bootstrap-throttle > bootstrap-strict > dry-run
    assert Mode.LIVE.rank() > Mode.BOOTSTRAP_THROTTLE.rank()
    assert Mode.BOOTSTRAP_THROTTLE.rank() > Mode.BOOTSTRAP_STRICT.rank()
    assert Mode.BOOTSTRAP_STRICT.rank() > Mode.DRY_RUN.rank()


def test_mode_rich_comparisons_follow_rank_for_every_pair():
    """Inherited string comparisons never replace the five-mode rank contract."""
    ordered = [
        Mode.RECONCILE_CHECK,
        Mode.DRY_RUN,
        Mode.BOOTSTRAP_STRICT,
        Mode.BOOTSTRAP_THROTTLE,
        Mode.LIVE,
    ]
    for index, lower in enumerate(ordered):
        assert lower <= lower
        assert lower >= lower
        assert not lower < lower
        assert not lower > lower
        for higher in ordered[index + 1 :]:
            assert lower < higher
            assert lower <= higher
            assert higher > lower
            assert higher >= lower
