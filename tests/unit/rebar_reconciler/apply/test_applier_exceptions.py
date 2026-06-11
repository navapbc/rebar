"""Tests for applier-level exception re-exports."""
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    m = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so dataclass annotation resolution
    # (PEP 563 `from __future__ import annotations`) can find the module via
    # cls.__module__ lookup during _process_class.
    sys.modules["applier"] = m
    spec.loader.exec_module(m)
    return m


def test_status_mapping_error_importable_from_applier():
    applier = _load_applier()
    assert hasattr(applier, "StatusMappingError")
    assert issubclass(applier.StatusMappingError, Exception)


def test_direction_mismatch_error_importable_from_applier():
    applier = _load_applier()
    assert hasattr(applier, "DirectionMismatchError")
    assert issubclass(applier.DirectionMismatchError, Exception)


def test_unknown_action_error_importable_from_applier():
    applier = _load_applier()
    assert hasattr(applier, "UnknownActionError")
    assert issubclass(applier.UnknownActionError, Exception)
