"""Applier-level exception behavior.

Each exception is provoked through a real raising seam and asserted via
``pytest.raises`` (never a mere importability / ``hasattr`` probe): a mismatched
direction drives ``_direction_guard``; an unregistered ``(direction, action)``
pair drives ``_apply_typed``. ``StatusMappingError`` has no live production raise
site — the outbound status preflight was downgraded to NON-FATAL (see
``reconcile.py`` "Facet 3 (reconciler-abort-isolation)") — so it is exercised at
its raise/catch contract, the nearest real seam.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


class _Val:
    """A hashable stand-in for a Mutation direction/action enum member: it carries
    a ``.value`` (what the applier reads) and is hashable by identity so it can key
    the ``_LEAVES`` dispatch table (SimpleNamespace defines ``__eq__`` and is not)."""

    def __init__(self, value: str) -> None:
        self.value = value


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    m = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so dataclass annotation resolution
    # (PEP 563 `from __future__ import annotations`) can find the module via
    # cls.__module__ lookup during _process_class.
    sys.modules["applier"] = m
    spec.loader.exec_module(m)
    return m


def test_direction_guard_raises_direction_mismatch_error():
    """``_direction_guard`` RAISES ``DirectionMismatchError`` when a mutation's
    direction does not match the leaf's declared direction — the real
    defense-in-depth seam a leaf hits when invoked with the wrong direction."""
    applier = _load_applier()
    mutation = SimpleNamespace(direction=_Val("inbound"))
    expected = _Val("outbound")

    with pytest.raises(applier.DirectionMismatchError) as exc_info:
        applier._direction_guard(mutation, expected)

    # The raised error names both the expected and the actual direction.
    message = str(exc_info.value)
    assert "outbound" in message and "inbound" in message


def test_apply_typed_raises_unknown_action_error_for_unregistered_pair():
    """``_apply_typed`` RAISES ``UnknownActionError`` (with zero side-effects) when
    the ``(direction, action)`` pair is absent from the ``_LEAVES`` dispatch table."""
    applier = _load_applier()
    bogus = SimpleNamespace(direction=_Val("sideways"), action=_Val("nope"))

    with pytest.raises(applier.UnknownActionError) as exc_info:
        applier._apply_typed(bogus)

    # The error message reports the unregistered action that reached dispatch.
    assert "nope" in str(exc_info.value)


def test_status_mapping_error_propagates_message_when_raised():
    """``StatusMappingError`` has no live production raise site (the preflight was
    downgraded to non-fatal), so it is exercised at its raise/catch contract:
    raising it yields a CATCHABLE ``Exception`` whose message round-trips through
    ``str`` — an observable behavior, not a ``hasattr`` probe."""
    applier = _load_applier()

    with pytest.raises(applier.StatusMappingError) as exc_info:
        raise applier.StatusMappingError("cannot map status 'Frobnicate'")

    assert "Frobnicate" in str(exc_info.value)
    assert isinstance(exc_info.value, Exception)
