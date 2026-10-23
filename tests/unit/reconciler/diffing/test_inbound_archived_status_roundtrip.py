"""A Jira Done echo of an outbound archived/deleted transition must not reopen or close
the terminal local ticket; a genuine non-Done move still applies."""

from __future__ import annotations

import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
INBOUND_DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "inbound_differ.py"


_id = importlib.import_module("rebar.reconciler.inbound_differ")


def _jira(status: str) -> dict:
    return {
        "summary": "T",
        "description": "D",
        "issuetype": "Task",
        "priority": "Medium",
        "status": status,
        "assignee": "",
    }


def test_archived_local_with_jira_done_does_not_flip_to_closed():
    local = {"ticket_id": "loc-1", "title": "T", "description": "D", "status": "archived"}
    changed = _id._diff_jira_vs_local(_jira("Done"), local)
    # ADR 0029 #2: the archived local terminal status is canonical; Jira Done is
    # the echo of our own terminal transition — no status flip.
    assert "status" not in changed, changed


def test_deleted_local_with_jira_done_does_not_flip_to_closed():
    local = {"ticket_id": "loc-1", "title": "T", "description": "D", "status": "deleted"}
    changed = _id._diff_jira_vs_local(_jira("Done"), local)
    assert "status" not in changed, changed


def test_archived_local_stable_over_three_passes():
    # Assert stability: re-diffing an archived/Done pair never emits a status change.
    local = {"ticket_id": "loc-1", "title": "T", "description": "D", "status": "archived"}
    for _ in range(3):
        changed = _id._diff_jira_vs_local(_jira("Done"), local)
        assert "status" not in changed


def test_genuine_non_done_jira_move_still_flows_for_archived_local():
    # A real Jira-side move to In Progress on a locally-archived ticket is NOT the
    # Done echo — it must still be mirrored inbound (suppression is Done-scoped).
    local = {"ticket_id": "loc-1", "title": "T", "description": "D", "status": "archived"}
    changed = _id._diff_jira_vs_local(_jira("In Progress"), local)
    assert changed.get("status") == "in_progress"


def test_active_local_with_jira_done_still_flips_to_closed():
    # The guard is terminal-local-only: an ACTIVE local ticket whose Jira went Done
    # still mirrors closed inbound (unchanged behavior).
    local = {"ticket_id": "loc-1", "title": "T", "description": "D", "status": "in_progress"}
    changed = _id._diff_jira_vs_local(_jira("Done"), local)
    assert changed.get("status") == "closed"
