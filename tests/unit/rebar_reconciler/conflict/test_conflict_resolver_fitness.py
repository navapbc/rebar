"""Behavioral fitness tests for rebar_reconciler/conflict_resolver.py.

Fitness invariants:
  - test_resolve_state_local_always_wins: resolve_state() never loses the local value.
  - test_resolve_additive_contains_both_values: resolve_additive() covers both inputs.
  - test_resolve_set_valued_idempotent: resolve_set_valued() union is idempotent.
  - test_resolve_field_unknown_defaults_to_local_wins: unknown field → local wins.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFLICT_RESOLVER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "conflict_resolver.py"
)


def _load_conflict_resolver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("conflict_resolver", CONFLICT_RESOLVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


mod = _load_conflict_resolver()

# ---------------------------------------------------------------------------
# Fitness test 1: resolve_state() never loses local value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "local_val,remote_val",
    [
        ("In Progress", "Done"),
        ("Open", None),
        (None, "Closed"),
        ("A", "B"),
    ],
)
def test_resolve_state_local_always_wins(local_val: object, remote_val: object) -> None:
    """resolve_state() must always return the local value, regardless of remote."""
    result = mod.resolve_state(local_val, remote_val)
    assert result == local_val


# ---------------------------------------------------------------------------
# Fitness test 2: resolve_additive() covers both input values
# ---------------------------------------------------------------------------


def test_resolve_additive_contains_both_values() -> None:
    """resolve_additive() output must contain content from both local and remote."""
    local = "Content A"
    remote = "Content B"
    result = mod.resolve_additive(local, remote)
    assert "Content A" in str(result)
    assert "Content B" in str(result)


# ---------------------------------------------------------------------------
# Fitness test 3: resolve_set_valued() union is idempotent
# ---------------------------------------------------------------------------


def test_resolve_set_valued_idempotent() -> None:
    """Applying resolve_set_valued() twice must not add new items to the result."""
    local_set = ["X", "Y"]
    remote_set = ["Y", "Z"]
    first = mod.resolve_set_valued(local_set, remote_set, [])
    second = mod.resolve_set_valued(first, remote_set, [])
    assert set(second) == set(first)


# ---------------------------------------------------------------------------
# Fitness test 4: resolve_field() with unknown field defaults to local-wins
# ---------------------------------------------------------------------------


def test_resolve_field_unknown_defaults_to_local_wins() -> None:
    """resolve_field() with an unregistered field name must return the local value."""
    result = mod.resolve_field("totally_unknown_field_xyz", "local_val", "remote_val")
    assert result == "local_val"
