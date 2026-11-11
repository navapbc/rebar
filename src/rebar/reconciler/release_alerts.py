"""Missing and archived Jira versions for rebar releases (ADR 0124, decisions 8 and 9).

The bridge never creates a Jira version. When a release a ticket intends to push names a
version the project lacks (or has archived), the outbound diff withholds the Jira add,
the release stays on the rebar ticket, and the ticket carries exactly one unresolved
``BRIDGE_ALERT`` naming it. Each pass retries; once the version exists the add goes out
and the alert is resolved.

This module owns the I/O around that rule: reading each project's versions once per pass
(an unreadable project is *unknown*, so nothing is withheld, alerted or resolved for it)
and writing alert events. The planning step, ``plan_release_alerts``, is pure.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rebar.reconciler._backend import TicketTransport

ALERT_PREFIX = "release-version-"
AVAILABLE = "available"
ARCHIVED = "archived"


def project_of(jira_key: str) -> str:
    """The project key of an issue key (``REB-12`` -> ``REB``)."""
    return jira_key.rsplit("-", 1)[0] if "-" in jira_key else ""


def read_release_availability(
    client: TicketTransport, project_keys: Iterable[str]
) -> dict[str, dict[str, str]]:
    """Map each readable project to ``{version name: "available"|"archived"}``.

    A project whose read fails is left out (unknown): the diff then behaves as before
    this rule, and no alert is written or resolved for its tickets this pass.
    """
    availability: dict[str, dict[str, str]] = {}
    for project in sorted(set(project_keys)):
        try:
            versions = client.get_project_versions(project)
        except Exception as exc:  # noqa: BLE001 — unknown availability; retried next pass
            print(  # noqa: T201
                f"WARNING: release_alerts: version read for project {project!r} failed "
                f"({exc!r}); fix-version availability unknown this pass",
                file=sys.stderr,
            )
            continue
        availability[project] = {
            v["name"]: ARCHIVED if v.get("archived") else AVAILABLE
            for v in versions or []
            if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"]
        }
    return availability


def projects_needing_availability(
    local_tickets: Iterable[dict[str, Any]], jira_key_for: Any
) -> set[str]:
    """Projects of bound tickets that hold releases or an unresolved release alert."""
    projects: set[str] = set()
    for ticket in local_tickets:
        local_id = ticket.get("ticket_id", ticket.get("id", ""))
        jira_key = jira_key_for(local_id)
        if not jira_key:
            continue
        if ticket.get("releases") or _unresolved_release_alerts(ticket):
            projects.add(project_of(jira_key))
    return projects


def _unresolved_release_alerts(ticket: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        a
        for a in ticket.get("bridge_alerts") or []
        if isinstance(a, dict)
        and not a.get("resolved")
        and str(a.get("reason", "")).startswith(ALERT_PREFIX)
    ]


def alert_reason(withheld: dict[str, str]) -> str:
    """``release-version-<missing|archived>:<name>`` for one withheld record."""
    return f"{ALERT_PREFIX}{withheld['reason']}:{withheld['release']}"


def plan_release_alerts(
    ticket: dict[str, Any], withheld: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """The BRIDGE_ALERT bodies that bring the ticket's release alerts in line with
    ``withheld``: one new alert per withheld reason not already open, and one
    resolution per open release alert whose exact reason is no longer current (so an
    archived version that is later deleted swaps its alert instead of keeping both)."""
    current = {alert_reason(w): w for w in withheld}
    open_alerts = _unresolved_release_alerts(ticket)
    open_reasons = {a.get("reason") for a in open_alerts}
    records: list[dict[str, Any]] = [
        {"reason": reason, "tag": f"{ALERT_PREFIX}{w['reason']}"}
        for reason, w in sorted(current.items())
        if reason not in open_reasons
    ]
    records.extend(
        {"resolved": True, "resolves_uuid": a.get("uuid"), "reason": a.get("reason")}
        for a in open_alerts
        if a.get("reason") not in current
    )
    return records


def write_release_alerts(
    withheld_sink: dict[str, tuple[str, list[dict[str, str]]]],
    local_by_id: dict[str, dict[str, Any]],
    tracker_dir: Path,
) -> int:
    """Write the planned BRIDGE_ALERT events for every ticket diffed with known
    availability. Best-effort per ticket, like the other outbound alert emitters: a
    failed write is logged and the next pass plans it again. Returns events written."""
    from rebar.reconciler.inbound_translate import _write_event_file

    written = 0
    for local_id, (_jira_key, withheld) in sorted(withheld_sink.items()):
        ticket = local_by_id.get(local_id)
        if ticket is None:
            continue
        for record in plan_release_alerts(ticket, withheld):
            try:
                _write_event_file(tracker_dir, local_id, "BRIDGE_ALERT", record)
                written += 1
            except Exception as exc:  # noqa: BLE001 — observability write; never fail the pass
                print(  # noqa: T201
                    f"WARNING: release_alerts: BRIDGE_ALERT write for {local_id} failed "
                    f"({exc!r}); retried next pass",
                    file=sys.stderr,
                )
    return written
