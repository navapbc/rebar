"""bridge-fsck binding-level drift audit (epic 3006-e198, child 8de5).

The pre-fix ``audit_bridge_mappings`` walked local event dirs ONLY (orphan /
duplicate / stale SYNC) and was structurally blind to binding-level drift: a
confirmed binding whose local ticket is archived/deleted, a binding whose local
ticket vanished, or a live/retired overlap. This asserts the new offline arm —
the SECOND consumer of the one convergence classifier — reads bindings.json and
flags them (the old checks return clean over the same store: RED before the arm).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def fsck() -> ModuleType:
    from rebar._engine_support import bridge_fsck

    return bridge_fsck


def _write_bindings(tracker: Path, bindings: dict, reverse: dict) -> None:
    state = tracker / ".bridge_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "bindings.json").write_text(
        json.dumps({"version": 2, "bindings": bindings, "reverse": reverse})
    )


def _confirmed(jira_key: str) -> dict:
    return {"jira_key": jira_key, "state": "confirmed"}


@pytest.mark.unit
@pytest.mark.scripts
def test_binding_drift_flags_archived_and_deleted_and_gone(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(
        tracker,
        bindings={
            "loc-active": _confirmed("REB-1"),
            "loc-arch": _confirmed("REB-2"),
            "loc-del": _confirmed("REB-3"),
            "loc-gone": _confirmed("REB-4"),
        },
        reverse={
            "REB-1": "loc-active",
            "REB-2": "loc-arch",
            "REB-3": "loc-del",
            "REB-4": "loc-gone",
        },
    )
    # Injected local states (identical shape to reduce_all_tickets output).
    local_states = [
        {"ticket_id": "loc-active", "status": "in_progress", "archived": False},
        {"ticket_id": "loc-arch", "status": "open", "archived": True},
        {"ticket_id": "loc-del", "status": "deleted", "archived": True},
        # loc-gone: deliberately ABSENT from the reduced store.
    ]
    drift = fsck.audit_binding_drift(tracker, local_states=local_states)

    wt = {e["local_id"] for e in drift["would_terminal"]}
    assert wt == {"loc-arch", "loc-del"}, drift
    lg = {e["local_id"] for e in drift["local_gone"]}
    assert lg == {"loc-gone"}, drift
    # The active binding is NOT drift (needs Jira to decide field-level sync).
    assert "loc-active" not in wt and "loc-active" not in lg


@pytest.mark.unit
@pytest.mark.scripts
def test_binding_drift_flags_live_retired_overlap(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(tracker, bindings={"loc-1": _confirmed("REB-9")}, reverse={"REB-9": "loc-1"})
    state = tracker / ".bridge_state"
    (state / "bindings-retired.json").write_text(
        json.dumps({"version": 1, "retired": {"REB-9": {"local_id": "loc-1"}}})
    )
    drift = fsck.audit_binding_drift(
        tracker, local_states=[{"ticket_id": "loc-1", "status": "open", "archived": False}]
    )
    assert [e["jira_key"] for e in drift["retired_overlap"]] == ["REB-9"]


@pytest.mark.unit
@pytest.mark.scripts
def test_no_snapshot_skips_jira_requiring_cells(fsck, tmp_path):
    # Without a Jira snapshot artifact, dangling/unbound_jira cannot be decided.
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(tracker, bindings={"loc-1": _confirmed("REB-1")}, reverse={"REB-1": "loc-1"})
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[{"ticket_id": "loc-1", "status": "open", "archived": False}],
        use_prev_snapshot=False,
    )
    assert drift["dangling"] == []
    assert drift["unbound_jira"] == []


@pytest.mark.unit
@pytest.mark.scripts
def test_dangling_binding_flagged_with_snapshot(fsck, tmp_path):
    # AC1(a)/AC4 — a confirmed binding whose Jira side is gone (absent from the
    # snapshot) is flagged as dangling, even with an ACTIVE local ticket. RED
    # before the snapshot arm: the audit returned clean for this exact case.
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(tracker, bindings={"f8b5": _confirmed("REB-530")}, reverse={"REB-530": "f8b5"})
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[{"ticket_id": "f8b5", "status": "in_progress", "archived": False}],
        jira_snapshot={},  # REB-530 absent from the snapshot → dangling candidate
    )
    assert drift["dangling"] == [{"local_id": "f8b5", "jira_key": "REB-530"}]


@pytest.mark.unit
@pytest.mark.scripts
def test_unbound_jira_native_flagged_with_snapshot(fsck, tmp_path):
    # AC1(c) — a Jira-native issue in the snapshot with no binding is unbound_jira.
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(tracker, bindings={"loc-1": _confirmed("REB-1")}, reverse={"REB-1": "loc-1"})
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[{"ticket_id": "loc-1", "status": "in_progress", "archived": False}],
        jira_snapshot={
            "REB-1": {"status": "In Progress"},
            "REB-532": {"status": "To Do"},  # native, unbound → adopt candidate
        },
    )
    assert drift["unbound_jira"] == [{"jira_key": "REB-532"}]
    assert drift["dangling"] == []  # REB-1 is present + bound + active → no drift


@pytest.mark.unit
@pytest.mark.scripts
def test_would_terminal_via_snapshot_when_jira_live(fsck, tmp_path):
    # An archived-local binding whose Jira is present + not Done → would_terminal.
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(
        tracker, bindings={"loc-a": _confirmed("REB-464")}, reverse={"REB-464": "loc-a"}
    )
    drift = fsck.audit_binding_drift(
        tracker,
        local_states=[{"ticket_id": "loc-a", "status": "archived", "archived": True}],
        jira_snapshot={"REB-464": {"status": "To Do"}},
    )
    assert drift["would_terminal"] == [{"local_id": "loc-a", "jira_key": "REB-464"}]
    assert drift["dangling"] == []


@pytest.mark.unit
@pytest.mark.scripts
def test_audit_bridge_mappings_includes_binding_drift_and_sets_exit(fsck, tmp_path):
    # The offline event-scan checks return clean, but binding_drift is non-empty →
    # the finding surfaces AND main() exits non-zero (class-D blindness healed).
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(
        tracker,
        bindings={"loc-arch": _confirmed("REB-2")},
        reverse={"REB-2": "loc-arch"},
    )
    # No local ticket dirs at all → reduce yields nothing → loc-arch is local_gone.
    findings = fsck.audit_bridge_mappings(tracker, now_ts=1_800_000_000_000_000_000)
    assert "binding_drift" in findings
    drift = findings["binding_drift"]
    assert drift["local_gone"] == [{"local_id": "loc-arch", "jira_key": "REB-2"}]
    # Old checks clean:
    assert findings["orphaned"] == [] and findings["duplicates"] == [] and findings["stale"] == []

    # The report renders the section; main() exits 1 on drift.
    report = fsck._format_report(findings)
    assert "Binding-Level Drift" in report
    rc = fsck.main(["--tickets-tracker", str(tracker), "--output", "json"])
    assert rc == 1


@pytest.mark.unit
@pytest.mark.scripts
def test_no_bindings_store_is_clean(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    drift = fsck.audit_binding_drift(tracker, local_states=[])
    assert drift == fsck._empty_binding_drift()
