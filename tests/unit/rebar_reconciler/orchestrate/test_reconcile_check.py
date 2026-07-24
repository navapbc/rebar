"""Unit tests for rebar_reconciler/reconcile_check.py — read-only discrepancy detection.

Tests follow the importlib-based loading convention used by the reconciler
test tree (see conftest.py docstring).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from rebar_reconciler.adapters.jira.backend import JiraBackend

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
RC_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile_check.py"


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


@pytest.fixture(scope="module")
def backend() -> JiraBackend:
    """A pure JiraBackend (mappers only, no live transport/env) — the
    injection seam ``reconcile_check(..., backend=...)`` exercises (ticket
    ad44), mirroring the outbound differ's test convention."""
    return JiraBackend(transport=object())


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


def test_all_in_sync(rc_mod: ModuleType, backend: JiraBackend) -> None:
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

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store, backend=backend)

    assert report["total_bindings"] == 1
    assert report["checked"] == 1
    assert report["in_sync"] == 1
    assert report["discrepancies"] == []
    assert report["orphaned_bindings"] == []
    assert report["orphaned_jira"] == []


def test_title_mismatch(rc_mod: ModuleType, backend: JiraBackend) -> None:
    """When local title differs from Jira summary, a discrepancy is reported."""
    local_tickets = [
        {"id": "abc-1234", "title": "Fix bug", "status": "open"},
    ]
    jira_snapshot = {
        "DIG-100": {"summary": "Fix bug in login", "status": "To Do"},
    }
    store = StubBindingStore([("abc-1234", "DIG-100")])

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store, backend=backend)

    assert report["checked"] == 1
    assert report["in_sync"] == 0
    assert len(report["discrepancies"]) == 1
    d = report["discrepancies"][0]
    assert d["field"] == "title"
    assert d["local_value"] == "Fix bug"
    assert d["jira_value"] == "Fix bug in login"


def test_orphaned_binding(rc_mod: ModuleType, backend: JiraBackend) -> None:
    """When binding exists but local ticket is deleted, it is an orphaned binding."""
    local_tickets = []  # ticket deleted
    jira_snapshot = {
        "DIG-100": {"summary": "Something"},
    }
    store = StubBindingStore([("abc-deleted", "DIG-100")])

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store, backend=backend)

    assert "abc-deleted" in report["orphaned_bindings"]
    assert report["checked"] == 0


def test_orphaned_jira(rc_mod: ModuleType, backend: JiraBackend) -> None:
    """Jira issue with rebar-id-* label but not in binding store is orphaned."""
    local_tickets = [{"id": "abc-1234", "title": "Local only"}]
    jira_snapshot = {
        "DIG-999": {
            "summary": "Orphaned",
            "labels": ["rebar-id-old-ticket", "team:backend"],
        },
    }
    store = StubBindingStore([])  # no bindings

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store, backend=backend)

    assert "DIG-999" in report["orphaned_jira"]


def test_unbound_counts(rc_mod: ModuleType, backend: JiraBackend) -> None:
    """Unbound local tickets and Jira issues without rebar-id labels are counted."""
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

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store, backend=backend)

    assert report["unbound_local"] == 2
    assert report["unbound_jira"] == 2


def test_priority_mismatch_with_mapping(rc_mod: ModuleType, backend: JiraBackend) -> None:
    """Priority 2 (local int) should match 'Medium' (Jira), not 'Highest'."""
    local_tickets = [
        {"id": "abc-1", "title": "T", "priority": 2, "status": "open"},
    ]
    jira_snapshot = {
        "DIG-1": {"summary": "T", "priority": "Highest", "status": "To Do"},
    }
    store = StubBindingStore([("abc-1", "DIG-1")])

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store, backend=backend)

    priority_discs = [d for d in report["discrepancies"] if d["field"] == "priority"]
    assert len(priority_discs) == 1
    assert priority_discs[0]["local_value"] == 2
    assert priority_discs[0]["jira_value"] == "Highest"


def test_labels_exclude_rebar_id(rc_mod: ModuleType, backend: JiraBackend) -> None:
    """rebar-id-* and imported:* labels are excluded from comparison."""
    local_tickets = [
        {
            "id": "abc-1",
            "title": "T",
            "status": "open",
            "tags": ["team:backend", "rebar-id-abc-1"],
        },
    ]
    jira_snapshot = {
        "DIG-1": {
            "summary": "T",
            "status": "To Do",
            "labels": ["team:backend", "rebar-id-abc-1", "imported:yes"],
        },
    }
    store = StubBindingStore([("abc-1", "DIG-1")])

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store, backend=backend)

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


# ---------------------------------------------------------------------------
# Bug ad39: reconcile-check must load compiled tickets from .cache.json["state"],
# not the nonexistent per-ticket ticket.json (which made EVERY binding orphan).
# ---------------------------------------------------------------------------


def _write_cache_ticket(tracker_dir: Path, tid: str, **state) -> None:
    """Write a compiled ticket exactly as the event-sourced store does:
    <id>/.cache.json with a top-level ``state`` object (plus sibling event
    files that must be ignored). NO ticket.json is ever written."""
    d = tracker_dir / tid
    d.mkdir(parents=True)
    st = {"ticket_id": tid, "status": "open", **state}
    (d / ".cache.json").write_text(json.dumps({"dir_hash": "x", "state": st}))
    # sibling event-log file — must not be mistaken for a ticket
    (d / "1-CREATE.json").write_text(json.dumps({"event": "CREATE"}))


def test_load_local_tickets_reads_compiled_cache_state(rc_mod: ModuleType, tmp_path: Path) -> None:
    tracker = tmp_path / ".tickets-tracker"
    _write_cache_ticket(tracker, "abc-1234", title="Fix bug", status="open")
    tickets = rc_mod.load_local_tickets(tracker)
    assert len(tickets) == 1
    assert tickets[0]["ticket_id"] == "abc-1234"
    assert tickets[0]["status"] == "open"
    assert tickets[0]["title"] == "Fix bug"


def test_load_local_tickets_skips_dirs_without_cache(rc_mod: ModuleType, tmp_path: Path) -> None:
    """A ticket dir that has ONLY a legacy ticket.json (no .cache.json) yields
    no ticket — proving the old ticket.json path is dead and the loader relies
    on the compiled cache the store actually writes."""
    tracker = tmp_path / ".tickets-tracker"
    d = tracker / "no-cache"
    d.mkdir(parents=True)
    (d / "ticket.json").write_text(json.dumps({"id": "no-cache", "status": "open"}))
    assert rc_mod.load_local_tickets(tracker) == []


def test_bound_ticket_via_cache_is_checked_not_orphaned(
    rc_mod: ModuleType, backend: JiraBackend, tmp_path: Path
) -> None:
    """Regression for bug ad39: a binding whose local ticket (in .cache.json)
    AND Jira issue both exist must count as checked/in_sync — NOT orphaned.

    Before the fix, reconcile-check read <id>/ticket.json (never written), so
    local_tickets was empty and EVERY binding fell into orphaned_bindings.
    """
    tracker = tmp_path / ".tickets-tracker"
    _write_cache_ticket(tracker, "abc-1234", title="Fix bug", status="open")
    jira_snapshot = {"DIG-100": {"summary": "Fix bug", "status": "To Do"}}
    store = StubBindingStore([("abc-1234", "DIG-100")])

    local_tickets = rc_mod.load_local_tickets(tracker)
    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store, backend=backend)

    assert report["orphaned_bindings"] == []  # was ["abc-1234"] before the fix
    assert report["checked"] == 1
    assert report["in_sync"] == 1

    # Demonstrate the OLD behavior: with the broken loader (no ticket.json →
    # empty local_tickets) the same binding is reported orphaned.
    broken_report = rc_mod.reconcile_check([], jira_snapshot, store, backend=backend)
    assert broken_report["orphaned_bindings"] == ["abc-1234"]
    assert broken_report["checked"] == 0


# ---------------------------------------------------------------------------
# Bug runny-lens-strafe: reconcile-check compared RAW live Jira snapshot values
# (nested {"name": ...} objects, ADF description dicts, assignee dicts,
# reconciler-written labels) against local scalars WITHOUT the extraction /
# normalization the real inbound differ applies — so on a healthy live store it
# flagged EVERY binding as discrepant (~3660 false positives) while the differ
# dispatched ~0 changes. reconcile-check must normalize the Jira snapshot the
# same way the differ does before comparing.
# ---------------------------------------------------------------------------


def _adf_doc(text: str) -> dict:
    """Build a minimal ADF (Atlassian Document Format) description dict, exactly
    the shape a live Jira cloud fetch returns for the ``description`` field."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _live_binding(status_name: str) -> tuple[list[dict], dict, StubBindingStore]:
    """One present-present binding in LIVE Jira snapshot shapes whose NORMALIZED
    values agree with the local ticket, parameterized by the Jira status name."""
    local_tickets = [
        {
            "id": "abc-1",
            "title": "T",
            "status": "open",
            "priority": 2,
            "description": "Hello",
            "assignee": "user@x.com",
            "tags": ["team:backend"],
        },
    ]
    jira_snapshot = {
        "DIG-1": {
            "summary": "T",
            "status": {"name": status_name},
            "priority": {"name": "Medium"},
            # issuetype is a deliberately sync-excepted field: the differ never
            # dispatches it, so it must be dropped from comparison entirely.
            "issuetype": {"name": "Story"},
            "description": _adf_doc("Hello"),
            "assignee": {
                "emailAddress": "user@x.com",
                "displayName": "User X",
                "accountId": "acc-1",
            },
            # jira-only reconciler-written labels that the differ excludes. NOTE
            # (ticket ad44): no ``rebar-status:`` annotation label here — under
            # canonical mapping that label takes PRECEDENCE over the raw Jira
            # workflow status for the local status field (inbound_fields), which
            # would make every ``status_name`` canonicalize to the SAME local
            # status and defeat this fixture's ``status_name`` parameterization
            # (used by both the in-sync and the genuine-drift test below).
            "labels": ["team:backend", "rebar-id:abc-1"],
        },
    }
    store = StubBindingStore([("abc-1", "DIG-1")])
    return local_tickets, jira_snapshot, store


def test_normalized_agree_raw_differ_counts_in_sync(
    rc_mod: ModuleType, backend: JiraBackend
) -> None:
    """A healthy live-shape binding whose normalized values agree with local
    must count as in_sync with ZERO discrepancies (the false-positive bug)."""
    local_tickets, jira_snapshot, store = _live_binding("To Do")

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store, backend=backend)

    assert report["checked"] == 1
    assert report["in_sync"] == 1
    assert report["discrepancies"] == []


def test_real_status_drift_still_reported(rc_mod: ModuleType, backend: JiraBackend) -> None:
    """Normalization must NOT suppress actionable drift: an identical binding
    except a genuinely different Jira status ("Done" vs local "open") must
    still report exactly one status discrepancy (critical guard)."""
    local_tickets, jira_snapshot, store = _live_binding("Done")

    report = rc_mod.reconcile_check(local_tickets, jira_snapshot, store, backend=backend)

    status_discs = [d for d in report["discrepancies"] if d["field"] == "status"]
    assert len(status_discs) == 1
    # status is the ONLY drift — normalization suppressed the raw-shape noise.
    assert report["discrepancies"] == status_discs
