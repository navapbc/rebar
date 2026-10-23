"""Set-valued conflict provenance keeps the newest 50 entries: below cap it appends, at cap
it evicts FIFO, and over-capacity input is truncated."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFLICT_RESOLVER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "conflict_resolver.py"


def _load_conflict_resolver() -> ModuleType:
    return importlib.import_module("rebar.reconciler.conflict_resolver")


@pytest.fixture(scope="module")
def cr() -> ModuleType:
    return _load_conflict_resolver()


# ---------------------------------------------------------------------------
# FIFO cap tests
# ---------------------------------------------------------------------------


def test_cap_at_50_no_growth(cr: ModuleType) -> None:
    """When provenance_record already has 50 elements and a new item is added via
    merge, the list must not grow beyond 50 elements."""
    provenance: list[str] = [f"item_{i}" for i in range(50)]
    assert len(provenance) == 50

    # Merge in a brand-new item not present in the provenance list
    cr.resolve_set_valued([], ["new_item"], provenance)

    assert len(provenance) == 50


def test_cap_fifo_eviction(cr: ModuleType) -> None:
    """Eviction is FIFO: the element at index 0 (oldest) is removed when the cap is
    reached and a new item is appended."""
    provenance: list[str] = [f"item_{i}" for i in range(50)]
    oldest = provenance[0]  # "item_0"

    cr.resolve_set_valued([], ["brand_new"], provenance)

    # Oldest element must be gone
    assert oldest not in provenance
    # Newest element must be present
    assert "brand_new" in provenance
    # Length must still be exactly 50
    assert len(provenance) == 50


def test_no_eviction_below_cap(cr: ModuleType) -> None:
    """When provenance_record has fewer than 50 elements, no eviction occurs; all
    merged items are simply appended."""
    provenance: list[str] = [f"item_{i}" for i in range(10)]
    cr.resolve_set_valued([], ["extra_item"], provenance)

    # All original items must still be present
    for i in range(10):
        assert f"item_{i}" in provenance
    # New item must have been appended
    assert "extra_item" in provenance
    # Total should be 11
    assert len(provenance) == 11


def test_cap_truncates_over_capacity_input(cr: ModuleType) -> None:
    """An already oversized provenance list is reduced to 50 after a merge, not merely
    prevented from growing."""
    provenance: list[str] = [f"old_{i}" for i in range(100)]
    cr.resolve_set_valued([], ["fresh_item"], provenance)

    assert len(provenance) == 50
    # FIFO eviction means the OLDEST entries are dropped; "fresh_item" survives
    assert "fresh_item" in provenance
    # The first 51 originals (indices 0..50) should be evicted, leaving 49
    # originals + "fresh_item" = 50 total.
    assert "old_0" not in provenance
    assert "old_99" in provenance
