"""Inbound field-sync directionality: a Jira-side edit must flow inbound, not revert.

Bug: the outbound differ used unconditional local-wins — it emitted an outbound
update whenever local != current Jira, *without* consulting prev_snapshot. So a
field a teammate changed in Jira (while local was unchanged since the last sync)
was reverted by local-wins instead of mirrored inbound to local.

Fix: when local is UNCHANGED since the last sync (local matches prev_snapshot) for
an inbound-mirrored field, suppress the outbound so the inbound differ mirrors the
Jira-side change. When local actually changed (local != prev), local-wins still
applies. With no prev entry it degrades to local-wins (no regression).

This applies to ALL inbound-mirrored fields (title/description/priority/status/
assignee), not just assignee.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
# Ticket 4af8: the pure field-diff helpers (_diff_fields/_extract_jira_field/
# _assignee_matches) live in the leaf outbound_fields adapter; the differ reaches them
# via the Backend port, so this field-diff suite loads the leaf directly.
DIFFER_PATH = (
    REPO_ROOT
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "adapters"
    / "jira"
    / "outbound_fields.py"
)


def _load_differ() -> ModuleType:
    spec = importlib.util.spec_from_file_location("outbound_differ_inbound_dir", DIFFER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["outbound_differ_inbound_dir"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def differ() -> ModuleType:
    return _load_differ()


ALICE = {"accountId": "alice", "displayName": "Alice", "emailAddress": "alice@x.com"}
BOB = {"accountId": "bob", "displayName": "Bob", "emailAddress": "bob@x.com"}


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


# --- Jira-side change, local unchanged -> SUPPRESS outbound (mirror inbound) -----


def test_jira_side_assignee_change_suppressed(differ):
    # last sync: assignee=Alice; local still Alice; Jira now Bob (teammate edit).
    changed = differ._diff_fields(
        _ticket(assignee="alice@x.com"), _jira(assignee=BOB), prev_jira_fields={"assignee": ALICE}
    )
    assert "assignee" not in changed, "Jira-side assignee change must mirror inbound, not revert"


def test_jira_side_status_change_suppressed(differ):
    # local 'open' maps to 'To Do' == prev; Jira now 'In Progress'.
    changed = differ._diff_fields(
        _ticket(status="open"),
        _jira(status={"name": "In Progress"}),
        prev_jira_fields={"status": {"name": "To Do"}},
    )
    assert "status" not in changed, "Jira-side status change must mirror inbound, not revert"


def test_jira_side_description_change_suppressed(differ):
    changed = differ._diff_fields(
        _ticket(description="old body"),
        _jira(description="new body from Jira"),
        prev_jira_fields={"description": "old body"},
    )
    assert "description" not in changed, "Jira-side description change must mirror inbound"


# --- local change -> local-wins still emits outbound ----------------------------


def test_local_assignee_change_still_emits(differ):
    # last sync: Alice; local changed to Bob; Jira unchanged (Alice).
    changed = differ._diff_fields(
        _ticket(assignee="bob@x.com"), _jira(assignee=ALICE), prev_jira_fields={"assignee": ALICE}
    )
    assert changed.get("assignee") == "bob@x.com", "a genuine local change still pushes outbound"


def test_local_description_change_still_emits(differ):
    changed = differ._diff_fields(
        _ticket(description="locally edited"),
        _jira(description="old body"),
        prev_jira_fields={"description": "old body"},
    )
    assert changed.get("description") == "locally edited"


# --- degrade: no prev -> local-wins (no regression) -----------------------------


def test_no_prev_degrades_to_local_wins(differ):
    changed = differ._diff_fields(_ticket(assignee="alice@x.com"), _jira(assignee=BOB))
    assert changed.get("assignee") == "alice@x.com", (
        "without prev, behaviour is unchanged local-wins"
    )
