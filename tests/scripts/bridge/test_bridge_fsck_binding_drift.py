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
def test_dangling_and_unbound_are_online_only(fsck, tmp_path):
    # Offline mode never populates the Jira-requiring cells.
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(tracker, bindings={"loc-1": _confirmed("REB-1")}, reverse={"REB-1": "loc-1"})
    drift = fsck.audit_binding_drift(
        tracker, local_states=[{"ticket_id": "loc-1", "status": "open", "archived": False}]
    )
    assert drift["dangling"] == []
    assert drift["unbound_jira"] == []


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
