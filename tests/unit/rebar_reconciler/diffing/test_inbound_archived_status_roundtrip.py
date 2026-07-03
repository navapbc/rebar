"""ADR 0029 #2 — archived/deleted → Done round-trip must not oscillate (bug 444d).

Child 444d adds an outbound ``archived``/``deleted`` → Jira ``Done`` transition.
Inbound maps Jira ``Done`` → local ``closed``, so without suppression the next
inbound pass would flip a locally-archived ticket to ``closed`` — an oscillation.
This asserts the inbound status diff KEEPS a terminal local status when Jira is
``Done`` (the echo of our own transition), while a genuine non-Done Jira move still
flows. RED before the inbound_differ guard.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[4]
INBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "inbound_differ.py"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


_id = _load("inbound_differ_archived", INBOUND_DIFFER_PATH)


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
