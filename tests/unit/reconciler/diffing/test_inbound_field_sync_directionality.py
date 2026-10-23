"""When local equals the previous snapshot, suppress outbound updates for
inbound-mirrored fields so Jira edits flow inward. A local edit still wins; with no
previous value, retain local-wins behavior."""

from __future__ import annotations

from typing import Any

from rebar.reconciler.adapters.jira.backend import JiraBackend
from rebar.reconciler.outbound_field_diff import compute_update_fields


def _diff(ticket: dict, jira: dict, *, prev_jira_fields: dict | None = None) -> dict[str, Any]:
    """Run the live outbound field diff with the Jira backend's mappers. A
    ``prev_jira_fields`` entry is the last-synced Jira value (the arbitration baseline)."""
    backend = JiraBackend(transport=object())
    return compute_update_fields(
        ticket,
        jira,
        inbound_mapper=backend.inbound,
        outbound_mapper=backend.outbound,
        jira_key="DIG-1",
        prev_snapshot={"DIG-1": prev_jira_fields} if prev_jira_fields else None,
    )


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


def test_jira_side_assignee_change_suppressed() -> None:
    # last sync: assignee=Alice; local still Alice; Jira now Bob (teammate edit).
    changed = _diff(
        _ticket(assignee="alice@x.com"), _jira(assignee=BOB), prev_jira_fields={"assignee": ALICE}
    )
    assert "assignee" not in changed, "Jira-side assignee change must mirror inbound, not revert"


def test_jira_side_status_change_suppressed() -> None:
    # local 'open' maps to 'To Do' == prev; Jira now 'In Progress'.
    changed = _diff(
        _ticket(status="open"),
        _jira(status={"name": "In Progress"}),
        prev_jira_fields={"status": {"name": "To Do"}},
    )
    assert "status" not in changed, "Jira-side status change must mirror inbound, not revert"


def test_jira_side_description_change_suppressed() -> None:
    changed = _diff(
        _ticket(description="old body"),
        _jira(description="new body from Jira"),
        prev_jira_fields={"description": "old body"},
    )
    assert "description" not in changed, "Jira-side description change must mirror inbound"


# --- local change -> local-wins still emits outbound ----------------------------


def test_local_assignee_change_still_emits() -> None:
    # last sync: Alice; local changed to Bob; Jira unchanged (Alice).
    changed = _diff(
        _ticket(assignee="bob@x.com"), _jira(assignee=ALICE), prev_jira_fields={"assignee": ALICE}
    )
    assert changed.get("assignee") == "bob@x.com", "a genuine local change still pushes outbound"


def test_local_description_change_still_emits() -> None:
    changed = _diff(
        _ticket(description="locally edited"),
        _jira(description="old body"),
        prev_jira_fields={"description": "old body"},
    )
    assert changed.get("description") == "locally edited"


# --- degrade: no prev -> local-wins (no regression) -----------------------------


def test_no_prev_degrades_to_local_wins() -> None:
    changed = _diff(_ticket(assignee="alice@x.com"), _jira(assignee=BOB))
    assert changed.get("assignee") == "alice@x.com", (
        "without prev, behaviour is unchanged local-wins"
    )


# Pin title/summary translation on the live canonical diff.


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
    from rebar.reconciler.outbound_field_diff import diff_canonical_fields

    return diff_canonical_fields(
        ticket,
        remote,
        baseline,
        outbound_mapper=_PassthroughOutboundMapper(),
        jira_key="DIG-1",
        local_id="loc-1",
    )


def test_jira_side_title_change_suppressed_on_the_canonical_path() -> None:
    """A Jira summary edit must flow inbound when local title is unchanged from
    baseline; an outbound title push would suppress that inbound edit."""
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
