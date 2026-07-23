"""Bug 5886 (audit reliability #7): an inbound Jira status with no mapping must NOT
silently reopen a closed local ticket.

Before the fix, `_map_jira_to_local_fields` defaulted an unmapped Jira status to
"open" (`_JIRA_TO_LOCAL_STATUS.get(status_raw, "open")`), so moving a Jira issue bound
to a CLOSED local ticket into a custom/unmapped workflow state (e.g. "In UAT") produced
an inbound status change closed→open — corrupting board state with no operator signal.
The fetcher already emits a deduped unmapped-status alert (fetcher-unmapped-jira-status)
from the same mapping; the differ must simply leave the local status untouched.
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


_id = _load("inbound_differ_unmapped", INBOUND_DIFFER_PATH)

# Ticket 4af8: the Jira->local field mapper is no longer re-exported on the differ
# module (the core differ now receives the mapper by injection). Load the mapper from
# its owning leaf module directly for the isolated mapper assertion below.
_INBOUND_FIELDS_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "inbound_fields.py"
)
_ifields = _load("inbound_fields_unmapped", _INBOUND_FIELDS_PATH)


def _jira(status: str) -> dict:
    return {
        "summary": "T",
        "description": "D",
        "issuetype": "Task",
        "priority": "Medium",
        "status": status,
        "assignee": "",
    }


def test_unmapped_status_does_not_reopen_closed_ticket():
    local = {"ticket_id": "loc-1", "title": "T", "description": "D", "status": "closed"}
    changed = _id._diff_jira_vs_local(_jira("In UAT"), local)
    # No status change emitted: the closed local ticket stays closed.
    assert "status" not in changed, f"unmapped status must not diff; got {changed!r}"


def test_unmapped_status_omitted_from_mapped_fields():
    mapped = _ifields._map_jira_to_local_fields(_jira("Totally Custom State"))
    # The mapper omits status entirely for an unmapped Jira status (no "open" default).
    assert "status" not in mapped
    # Other fields still map.
    assert mapped["title"] == "T"


def test_mapped_status_still_flows():
    # A genuinely mapped status still produces the expected local status change.
    local = {"ticket_id": "loc-2", "title": "T", "description": "D", "status": "open"}
    changed = _id._diff_jira_vs_local(_jira("Done"), local)
    assert changed.get("status") == "closed"


def test_idea_status_roundtrip_unaffected():
    # IDEA <-> idea must keep mapping (the fix only touches the UNMAPPED default path).
    local = {"ticket_id": "loc-3", "title": "T", "description": "D", "status": "open"}
    changed = _id._diff_jira_vs_local(_jira("IDEA"), local)
    assert changed.get("status") == "idea"
