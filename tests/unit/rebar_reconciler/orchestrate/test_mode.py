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
    assert "reconcile-check" not in message


def test_from_str_decodes_legacy_pause_sentinel_without_listing_it():
    """Historical pause blobs decode to the live cap-0 read-only floor."""
    assert Mode.from_str("reconcile-check") is Mode.DRY_RUN


def test_mode_has_exactly_four_members():
    """Mode enum must contain exactly the four rollout-safety modes."""
    assert {m.value for m in Mode} == {
        "dry-run",
        "bootstrap-strict",
        "bootstrap-throttle",
        "live",
    }


def test_mode_caps_preserve_all_four_limits():
    """MODE_CAPS remains the exact contract for every mode."""
    assert MODE_CAPS == {
        Mode.DRY_RUN: 0,
        Mode.BOOTSTRAP_STRICT: 10,
        Mode.BOOTSTRAP_THROTTLE: 100,
        Mode.LIVE: None,
    }


def test_mode_ordering_dry_run_special():
    """dry-run orders before any operational mode."""
    assert Mode.DRY_RUN < Mode.BOOTSTRAP_STRICT
    assert Mode.DRY_RUN < Mode.BOOTSTRAP_THROTTLE
    assert Mode.DRY_RUN < Mode.LIVE


def test_mode_ordering_bootstrap_strict_less_than_bootstrap_throttle():
    """bootstrap-strict is ordered before bootstrap-throttle."""
    assert Mode.BOOTSTRAP_STRICT < Mode.BOOTSTRAP_THROTTLE


def test_mode_ordering_bootstrap_throttle_less_than_live():
    """bootstrap-throttle is ordered before live."""
    assert Mode.BOOTSTRAP_THROTTLE < Mode.LIVE


def test_mode_ordering_supports_comparison():
    """Modes support > comparison semantics for check_phase_gate."""
    assert Mode.LIVE > Mode.BOOTSTRAP_THROTTLE
    assert Mode.BOOTSTRAP_THROTTLE > Mode.BOOTSTRAP_STRICT
    assert Mode.BOOTSTRAP_STRICT > Mode.DRY_RUN


def test_mode_rich_comparisons_follow_order_for_every_pair():
    """Inherited string comparisons never replace the four-mode order contract."""
    ordered = [
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


def test_mode_rich_comparisons_accept_equivalent_members_from_a_second_load():
    """Dynamic loader aliases retain the same four-value comparison contract."""
    spec = importlib.util.spec_from_file_location("rebar_reconciler_mode_second_load", MODE_PATH)
    assert spec is not None and spec.loader is not None
    second = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = second
    spec.loader.exec_module(second)

    assert Mode.BOOTSTRAP_THROTTLE > second.Mode.DRY_RUN
    assert Mode.DRY_RUN <= second.Mode.DRY_RUN
    assert Mode.LIVE.__gt__("bootstrap-throttle") is NotImplemented
