"""Unit tests for rebar_reconciler/reconcile_check.py — read-only discrepancy detection.

Tests follow the importlib-based loading convention used by the reconciler
test tree (see conftest.py docstring).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
RC_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile_check.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def rc_mod() -> ModuleType:
    return _load_module("reconcile_check", RC_PATH)


# ---------------------------------------------------------------------------
# Minimal BindingStore stub
# ---------------------------------------------------------------------------


class StubBindingStore:
    """In-memory binding store for tests.

    Mirrors the real ``BindingStore.all_bindings()`` return type:
    ``dict[str, dict]`` where each value has at least a ``"jira_key"`` field.
    Accepts ``(local_id, jira_key)`` tuples for convenience and converts them.
    """

    def __init__(self, bindings: list[tuple[str, str]]) -> None:
        self._bindings: dict[str, dict] = {
            local_id: {"jira_key": jira_key, "state": "confirmed"}
            for local_id, jira_key in bindings
        }

    def all_bindings(self) -> dict[str, dict]:
        return dict(self._bindings)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_in_sync(rc_mod: ModuleType) -> None:
    """When all bound pairs match, report 0 discrepancies."""
    local_tickets = [
        {"id": "abc-1234", "title": "Fix bug", "status": "open", "description": "desc"},
    ]
    jira_snapshot = {
        "DIG-100": {
            "summary": "Fix bug",
            "status": "To Do",
            "description": "desc",
        },
    }
    store = StubBindingStore([("abc-1234", "DIG-100")])

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store)

    assert report["total_bindings"] == 1
    assert report["checked"] == 1
    assert report["in_sync"] == 1
    assert report["discrepancies"] == []
    assert report["orphaned_bindings"] == []
    assert report["orphaned_jira"] == []


def test_title_mismatch(rc_mod: ModuleType) -> None:
    """When local title differs from Jira summary, a discrepancy is reported."""
    local_tickets = [
        {"id": "abc-1234", "title": "Fix bug", "status": "open"},
    ]
    jira_snapshot = {
        "DIG-100": {"summary": "Fix bug in login", "status": "To Do"},
    }
    store = StubBindingStore([("abc-1234", "DIG-100")])

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store)

    assert report["checked"] == 1
    assert report["in_sync"] == 0
    assert len(report["discrepancies"]) == 1
    d = report["discrepancies"][0]
    assert d["field"] == "title"
    assert d["local_value"] == "Fix bug"
    assert d["jira_value"] == "Fix bug in login"


def test_orphaned_binding(rc_mod: ModuleType) -> None:
    """When binding exists but local ticket is deleted, it is an orphaned binding."""
    local_tickets = []  # ticket deleted
    jira_snapshot = {
        "DIG-100": {"summary": "Something"},
    }
    store = StubBindingStore([("abc-deleted", "DIG-100")])

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store)

    assert "abc-deleted" in report["orphaned_bindings"]
    assert report["checked"] == 0


def test_orphaned_jira(rc_mod: ModuleType) -> None:
    """Jira issue with dso-id-* label but not in binding store is orphaned."""
    local_tickets = [{"id": "abc-1234", "title": "Local only"}]
    jira_snapshot = {
        "DIG-999": {
            "summary": "Orphaned",
            "labels": ["dso-id-old-ticket", "team:backend"],
        },
    }
    store = StubBindingStore([])  # no bindings

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store)

    assert "DIG-999" in report["orphaned_jira"]


def test_unbound_counts(rc_mod: ModuleType) -> None:
    """Unbound local tickets and Jira issues without dso-id labels are counted."""
    local_tickets = [
        {"id": "local-1", "title": "A"},
        {"id": "local-2", "title": "B"},
        {"id": "bound-1", "title": "C", "status": "open"},
    ]
    jira_snapshot = {
        "DIG-100": {"summary": "C", "status": "To Do"},
        "DIG-200": {"summary": "Unbound jira", "labels": []},
        "DIG-300": {"summary": "Also unbound"},
    }
    store = StubBindingStore([("bound-1", "DIG-100")])

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store)

    assert report["unbound_local"] == 2
    assert report["unbound_jira"] == 2


def test_priority_mismatch_with_mapping(rc_mod: ModuleType) -> None:
    """Priority 2 (local int) should match 'Medium' (Jira), not 'Highest'."""
    local_tickets = [
        {"id": "abc-1", "title": "T", "priority": 2, "status": "open"},
    ]
    jira_snapshot = {
        "DIG-1": {"summary": "T", "priority": "Highest", "status": "To Do"},
    }
    store = StubBindingStore([("abc-1", "DIG-1")])

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store)

    priority_discs = [d for d in report["discrepancies"] if d["field"] == "priority"]
    assert len(priority_discs) == 1
    assert priority_discs[0]["local_value"] == 2
    assert priority_discs[0]["jira_value"] == "Highest"


def test_labels_exclude_dso_id(rc_mod: ModuleType) -> None:
    """dso-id-* and imported:* labels are excluded from comparison."""
    local_tickets = [
        {
            "id": "abc-1",
            "title": "T",
            "status": "open",
            "tags": ["team:backend", "dso-id-abc-1"],
        },
    ]
    jira_snapshot = {
        "DIG-1": {
            "summary": "T",
            "status": "To Do",
            "labels": ["team:backend", "dso-id-abc-1", "imported:yes"],
        },
    }
    store = StubBindingStore([("abc-1", "DIG-1")])

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store)

    label_discs = [d for d in report["discrepancies"] if d["field"] == "labels"]
    assert label_discs == []


def test_format_report_produces_readable_output(rc_mod: ModuleType) -> None:
    """format_report returns a human-readable string."""
    report = {
        "total_bindings": 10,
        "checked": 10,
        "in_sync": 9,
        "discrepancies": [
            {
                "local_id": "abc-1",
                "jira_key": "DIG-1",
                "field": "title",
                "local_value": "A",
                "jira_value": "B",
            }
        ],
        "orphaned_bindings": [],
        "orphaned_jira": ["DIG-999"],
        "unbound_local": 5,
        "unbound_jira": 0,
    }
    text = rc_mod.format_report(report)
    assert "10 bindings" in text
    assert "9 in sync" in text
    assert "DIG-1" in text
    assert "DIG-999" in text
