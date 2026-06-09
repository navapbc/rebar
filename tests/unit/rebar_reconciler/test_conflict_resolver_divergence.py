"""Divergence behavioral tests for rebar_reconciler/conflict_resolver.py.

Tests cover the three resolution classes using conftest.py fixtures:
  - test_state_divergence_local_wins: state-class → local value always returned.
  - test_additive_divergence_both_present: additive-class → combined content
      contains both local and remote.
  - test_set_divergence_union_all_three: set-class → union contains all 3 items.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers (mirrors test_conflict_resolver.py pattern)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFLICT_RESOLVER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "conflict_resolver.py"
)


def _load_conflict_resolver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("conflict_resolver", CONFLICT_RESOLVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def cr() -> ModuleType:
    return _load_conflict_resolver()


# ---------------------------------------------------------------------------
# State-class divergence
# ---------------------------------------------------------------------------


def test_state_divergence_local_wins(cr: ModuleType, state_divergence: dict) -> None:
    """State-class: resolve_field('status', 'In Progress', 'Done') returns local value."""
    field = state_divergence["field"]
    local = state_divergence["local"]
    remote = state_divergence["remote"]

    result = cr.resolve_field(field, local, remote)

    assert result == local, (
        f"State-class field '{field}' must return local value '{local}'; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Additive-class divergence
# ---------------------------------------------------------------------------


def test_additive_divergence_both_present(cr: ModuleType, additive_divergence: dict) -> None:
    """Additive-class: result contains both local and remote content."""
    field = additive_divergence["field"]
    local = additive_divergence["local"]
    remote = additive_divergence["remote"]

    result = cr.resolve_field(field, local, remote)

    assert local in result, (
        f"Additive-class field '{field}': expected local content {local!r} in result {result!r}"
    )
    assert remote in result, (
        f"Additive-class field '{field}': expected remote content {remote!r} in result {result!r}"
    )


# ---------------------------------------------------------------------------
# Set-class divergence
# ---------------------------------------------------------------------------


def test_set_divergence_union_all_three(cr: ModuleType, set_divergence: dict) -> None:
    """Set-class: result is the union of local and remote, containing all 3 items."""
    field = set_divergence["field"]
    local = set_divergence["local"]
    remote = set_divergence["remote"]
    expected_union = set_divergence["expected_union"]

    result = cr.resolve_field(field, local, remote, [])

    result_set = set(result)
    assert result_set == expected_union, (
        f"Set-class field '{field}': expected union {expected_union}; got {result_set}"
    )
