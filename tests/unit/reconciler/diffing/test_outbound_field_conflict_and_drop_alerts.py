"""Outbound observability (bugs a713 / acd0): a both-sides field conflict and an
allowlist-dropped field must be recorded, without changing behavior (local-wins and
the drop are preserved — only signals are added).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

from rebar.reconciler.adapters.jira import outbound_fields as _of  # 625b: adapter-internal

REPO_ROOT = Path(__file__).resolve().parents[4]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "outbound_differ.py"
RUN_DIFFERS_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "run_differs.py"
ALERT_STORE_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "alert_store.py"


@pytest.fixture(scope="module")
def differ() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_differ")


def _ticket(**ov) -> dict:
    t = {
        "ticket_id": "x",
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "",
    }
    t.update(ov)
    return t


def _jira(**ov) -> dict:
    f = {
        "summary": "T",
        "description": "D",
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": None,
    }
    f.update(ov)
    return f


# ── a713: both-sides conflict ────────────────────────────────────────────────
def test_both_sides_conflict_is_recorded(differ) -> None:
    """local AND Jira both diverged from the baseline → conflict recorded; local still
    wins (the field is still in `changed`)."""
    sink: list[tuple[str, str]] = []
    changed = _of._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        jira_key="KEY-1",
        prev_jira_fields={"description": "D"},
        conflict_sink=sink,
    )
    assert changed.get("description") == "local-edit"  # local-wins preserved
    assert ("KEY-1", "description") in sink


def test_one_sided_local_change_is_not_a_conflict(differ) -> None:
    """local changed, Jira still at baseline → local-wins, but NO conflict."""
    sink: list[tuple[str, str]] = []
    changed = _of._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="D"),
        jira_key="KEY-2",
        prev_jira_fields={"description": "D"},
        conflict_sink=sink,
    )
    assert changed.get("description") == "local-edit"
    assert sink == []


def test_no_baseline_never_fabricates_a_conflict(differ) -> None:
    sink: list[tuple[str, str]] = []
    _of._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        jira_key="KEY-3",
        prev_jira_fields=None,
        conflict_sink=sink,
    )
    assert sink == []


# ── acd0: allowlist-dropped field ────────────────────────────────────────────
def test_dropped_issuetype_is_recorded(differ) -> None:
    """A local issuetype that differs from Jira is dropped by the outbound allowlist —
    it must be recorded, and still NOT emitted."""
    dropped: list[tuple[str, str]] = []
    changed = _of._diff_fields(
        _ticket(ticket_type="bug"),  # -> issuetype "Bug"
        _jira(issuetype={"name": "Task"}),
        jira_key="KEY-4",
        dropped_field_sink=dropped,
    )
    assert "issuetype" not in changed  # still excluded from the outbound update
    assert ("KEY-4", "issuetype") in dropped


def test_matching_issuetype_is_not_flagged(differ) -> None:
    dropped: list[tuple[str, str]] = []
    _of._diff_fields(
        _ticket(ticket_type="task"),  # -> "Task"
        _jira(issuetype={"name": "Task"}),
        jira_key="KEY-5",
        dropped_field_sink=dropped,
    )
    assert dropped == []


# ── dedup: one alert per (kind, ticket, field) ───────────────────────────────
def test_emit_dedupes_per_ticket_field(tmp_path) -> None:
    rd = importlib.import_module("rebar.reconciler.run_differs")
    als = importlib.import_module("rebar.reconciler.alert_store")
    rd._emit_outbound_field_alerts([("KEY-9", "priority")], [], tmp_path, "p1")
    assert als.is_deduped("outbound-field-conflict:KEY-9:priority", repo_root=tmp_path)
    # A second pass for the same (ticket, field) must not re-file.
    rd._emit_outbound_field_alerts([("KEY-9", "priority")], [], tmp_path, "p2")
    store_dir = als._store_dir(tmp_path)
    records = [
        line
        for f in store_dir.glob("*")
        if f.is_file()
        for line in f.read_text().splitlines()
        if "outbound-field-conflict:KEY-9:priority" in line
    ]
    assert len(records) == 1, f"expected exactly one deduped alert, got {records}"
