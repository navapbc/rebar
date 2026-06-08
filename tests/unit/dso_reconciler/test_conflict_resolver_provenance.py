"""Unit tests for resolve_field ledger threading + FIELD_CLASSES title/type entries.

RED task 902e-1eea-a505-451e — extend FIELD_CLASSES + thread ledger through
resolve_field. Tests use a minimal fake ledger duck-typed to the
ProvenanceLedger API (record(key, side, value)) to avoid coupling this test
file to the (separately-merged) provenance_ledger module.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFLICT_RESOLVER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "conflict_resolver.py"
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
# Fake ledger — duck-typed to ProvenanceLedger.record(key, side, value)
# ---------------------------------------------------------------------------


@dataclass
class FakeLedger:
    """Records each .record() call verbatim for inspection."""

    calls: list[tuple[str, str, Any]] = field(default_factory=list)

    def record(self, key: str, side: str, value: Any) -> None:
        self.calls.append((key, side, value))


# ---------------------------------------------------------------------------
# FIELD_CLASSES extension
# ---------------------------------------------------------------------------


def test_field_classes_includes_title_and_type(cr: ModuleType) -> None:
    """FIELD_CLASSES MUST classify both 'title' and 'type' as 'state'."""
    assert cr.FIELD_CLASSES.get("title") == "state"
    assert cr.FIELD_CLASSES.get("type") == "state"


# ---------------------------------------------------------------------------
# Backward-compat: ledger=None preserves prior behavior
# ---------------------------------------------------------------------------


def test_resolve_field_backward_compatible_no_ledger(cr: ModuleType) -> None:
    """resolve_field without a ledger arg matches the prior contract exactly."""
    # State: local wins
    assert cr.resolve_field("status", "open", "closed") == "open"
    # New state-class fields: local wins
    assert cr.resolve_field("title", "My Story", "Their Title") == "My Story"
    assert cr.resolve_field("type", "story", "task") == "story"
    # Additive list: order-preserved union
    assert cr.resolve_field("comments", ["a"], ["b"]) == ["a", "b"]
    # Set: union via resolve_set_valued (provenance_record None branch)
    result = cr.resolve_field("labels", ["x"], ["y"])
    assert set(result) == {"x", "y"}
    # Unknown field: defaults to resolve_state (local wins)
    assert cr.resolve_field("unknown_field", "L", "R") == "L"


# ---------------------------------------------------------------------------
# Per-element provenance across all element classes
# ---------------------------------------------------------------------------


def test_resolve_field_records_provenance_all_element_classes(cr: ModuleType) -> None:
    """ledger.record fires for every scalar resolve + every collection element."""
    ledger = FakeLedger()

    # 6 scalars (all state-class except description which is additive scalar).
    cr.resolve_field("title", "Story A", "Remote Title", ledger=ledger)
    cr.resolve_field("description", "local desc", "remote desc", ledger=ledger)
    cr.resolve_field("status", "open", "closed", ledger=ledger)
    cr.resolve_field("priority", "P1", "P3", ledger=ledger)
    cr.resolve_field("type", "story", "task", ledger=ledger)
    cr.resolve_field("assignee", "alice", "bob", ledger=ledger)

    # 4 collections — each contributes one record per element.
    cr.resolve_field("comments", ["c1"], ["c2"], ledger=ledger)
    cr.resolve_field("labels", ["L1"], ["L2"], ledger=ledger)
    cr.resolve_field("watchers", ["w1"], ["w2"], ledger=ledger)
    cr.resolve_field("links", ["lnk1"], ["lnk2"], ledger=ledger)

    # 6 scalar records + 4 collections * 2 elements = 14 total.
    assert len(ledger.calls) == 14

    # Side attribution: local items recorded as 'local', remote-only as 'jira'.
    sides_by_key = {key: side for key, side, _val in ledger.calls}
    assert sides_by_key["title"] == "local"
    assert sides_by_key["status"] == "local"
    assert sides_by_key["type"] == "local"
    # At least one local + one jira entry across the collection elements.
    collection_sides = [side for key, side, _ in ledger.calls if ":" in key]
    assert "local" in collection_sides
    assert "jira" in collection_sides


# ---------------------------------------------------------------------------
# Coexistence: provenance_record FIFO list + ledger both populated
# independently when both are provided to a set-class resolve.
# ---------------------------------------------------------------------------


def test_set_strategy_populates_both_provenance_record_and_ledger(
    cr: ModuleType,
) -> None:
    """The legacy provenance_record FIFO and the new ledger both write; neither
    derives from the other (independent records).
    """
    provenance_record: list[Any] = []
    ledger = FakeLedger()

    result = cr.resolve_field(
        "labels",
        ["alpha"],
        ["beta"],
        provenance_record=provenance_record,
        ledger=ledger,
    )

    # The union is correct.
    assert set(result) == {"alpha", "beta"}
    # provenance_record FIFO populated (set-level audit).
    assert "alpha" in provenance_record
    assert "beta" in provenance_record
    # Ledger populated (per-element).
    keys = [k for k, _s, _v in ledger.calls]
    assert any(k.startswith("labels:") and k.endswith("alpha") for k in keys)
    assert any(k.startswith("labels:") and k.endswith("beta") for k in keys)
    # Independence: ledger does not reuse provenance_record entries verbatim
    # (ledger keys are composite "labels:<id>", FIFO entries are bare values).
    assert "alpha" not in keys
    assert "beta" not in keys
