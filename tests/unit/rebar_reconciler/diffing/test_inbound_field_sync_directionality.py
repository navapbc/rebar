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

from rebar_reconciler.adapters.jira import outbound_fields as _of  # 625b: adapter-internal

REPO_ROOT = Path(__file__).resolve().parents[4]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"


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
    changed = _of._diff_fields(
        _ticket(assignee="alice@x.com"), _jira(assignee=BOB), prev_jira_fields={"assignee": ALICE}
    )
    assert "assignee" not in changed, "Jira-side assignee change must mirror inbound, not revert"


def test_jira_side_status_change_suppressed(differ):
    # local 'open' maps to 'To Do' == prev; Jira now 'In Progress'.
    changed = _of._diff_fields(
        _ticket(status="open"),
        _jira(status={"name": "In Progress"}),
        prev_jira_fields={"status": {"name": "To Do"}},
    )
    assert "status" not in changed, "Jira-side status change must mirror inbound, not revert"


def test_jira_side_description_change_suppressed(differ):
    changed = _of._diff_fields(
        _ticket(description="old body"),
        _jira(description="new body from Jira"),
        prev_jira_fields={"description": "old body"},
    )
    assert "description" not in changed, "Jira-side description change must mirror inbound"


# --- local change -> local-wins still emits outbound ----------------------------


def test_local_assignee_change_still_emits(differ):
    # last sync: Alice; local changed to Bob; Jira unchanged (Alice).
    changed = _of._diff_fields(
        _ticket(assignee="bob@x.com"), _jira(assignee=ALICE), prev_jira_fields={"assignee": ALICE}
    )
    assert changed.get("assignee") == "bob@x.com", "a genuine local change still pushes outbound"


def test_local_description_change_still_emits(differ):
    changed = _of._diff_fields(
        _ticket(description="locally edited"),
        _jira(description="old body"),
        prev_jira_fields={"description": "old body"},
    )
    assert changed.get("description") == "locally edited"


# --- degrade: no prev -> local-wins (no regression) -----------------------------


def test_no_prev_degrades_to_local_wins(differ):
    changed = _of._diff_fields(_ticket(assignee="alice@x.com"), _jira(assignee=BOB))
    assert changed.get("assignee") == "alice@x.com", (
        "without prev, behaviour is unchanged local-wins"
    )


# --- title, on the LIVE canonical path (outbound_field_diff) --------------------
#
# The cases above drive ``adapters/jira/outbound_fields._diff_fields``, which no
# production caller reaches any more: since ticket 625b the pass runs
# ``outbound_field_diff.diff_canonical_fields`` (outbound_differ.py imports
# ``compute_update_fields`` from it). ``title`` is the one inbound-mirrored field
# whose local and Jira names differ (``title`` vs ``summary``), and it is the field
# the module docstring above names first — so it is pinned here against the module
# the pass actually executes.


def _canonical(**ov) -> dict:
    """A canonical (LOCAL-shaped) field dict, the shape the InboundMapper emits."""
    f = {
        "title": "T",
        "description": "D",
        "priority": 2,
        "status": "open",
        "assignee": "",
    }
    f.update(ov)
    return f


class _PassthroughOutboundMapper:
    """The OutboundMapper port, reduced to the two operations the diff calls."""

    def map_fields_to_remote(self, changed, **_kw):
        return dict(changed)

    def resolve_assignee(self, local_value, _remote_identity, *, assignee_resolver=None):
        return (local_value, False, False)


def _live_diff(ticket: dict, remote: dict, baseline: dict | None) -> dict:
    from rebar_reconciler.outbound_field_diff import diff_canonical_fields

    return diff_canonical_fields(
        ticket,
        remote,
        baseline,
        outbound_mapper=_PassthroughOutboundMapper(),
        jira_key="DIG-1",
        local_id="loc-1",
    )


def test_jira_side_title_change_suppressed_on_the_canonical_path() -> None:
    """A Jira-side SUMMARY edit, with local unchanged since the baseline, must NOT be
    reverted by an outbound local-wins push.

    ADR 0026 names the arbitrated set as the five inbound-mirrored scalar fields
    ``title``/``description``/``priority``/``status``/``assignee`` and mandates
    "local == baseline -> mirror Jira inbound (suppress outbound)". Emitting ``title``
    here makes the inbound differ drop its own title update as a contradiction
    (inbound_differ bidirectional suppression, bug 3bf8), so the Jira-side title never
    reaches the local ticket and the stale local title is pushed back over it.
    """
    baseline = _canonical(title="OLD")
    ticket = {"ticket_id": "loc-1", "ticket_type": "task", **_canonical(title="OLD")}
    remote = _canonical(title="NEW from Jira")

    changed = _live_diff(ticket, remote, baseline)

    assert "title" not in changed, (
        "a Jira-side title edit must mirror inbound, not be reverted by local-wins; "
        f"changed={ {k: v for k, v in changed.items() if not k.startswith('_')} }"
    )


def test_local_title_change_still_emits_on_the_canonical_path() -> None:
    """The other half of the arbitration: local != baseline is a genuine local edit,
    so local-wins still pushes the title outbound."""
    baseline = _canonical(title="OLD")
    ticket = {"ticket_id": "loc-1", "ticket_type": "task", **_canonical(title="locally edited")}
    remote = _canonical(title="OLD")

    changed = _live_diff(ticket, remote, baseline)

    assert changed.get("title") == "locally edited", "a genuine local title edit still pushes"


def test_title_without_a_baseline_degrades_to_local_wins() -> None:
    """No ancestor for ``title`` -> no arbitration possible -> unchanged local-wins."""
    ticket = {"ticket_id": "loc-1", "ticket_type": "task", **_canonical(title="LOCAL")}
    remote = _canonical(title="NEW from Jira")

    assert _live_diff(ticket, remote, None).get("title") == "LOCAL"
    # A baseline that simply omits ``title`` is the same "no ancestor" case.
    assert _live_diff(ticket, remote, {"description": "D"}).get("title") == "LOCAL"
