"""Missing / archived Jira version alerts for releases (ADR 0124, gymnasial-snappy-lorikeet)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from rebar.reconciler import release_alerts as ra

pytestmark = pytest.mark.unit


def _alert(reason: str, uuid: str = "u-1", resolved: bool = False) -> dict[str, Any]:
    return {"uuid": uuid, "reason": reason, "timestamp": 1, "resolved": resolved}


def _missing(name: str) -> dict[str, str]:
    return {"release": name, "reason": "missing"}


# ── plan_release_alerts ──────────────────────────────────────────────────────


def test_new_withheld_name_opens_one_alert() -> None:
    assert ra.plan_release_alerts({"bridge_alerts": []}, [_missing("2.4")]) == [
        {"reason": "release-version-missing:2.4", "tag": "release-version-missing"}
    ]


def test_open_alert_for_the_same_reason_is_not_duplicated() -> None:
    ticket = {"bridge_alerts": [_alert("release-version-missing:2.4")]}
    assert ra.plan_release_alerts(ticket, [_missing("2.4")]) == []


def test_alert_whose_name_is_no_longer_withheld_is_resolved() -> None:
    ticket = {"bridge_alerts": [_alert("release-version-missing:2.4", uuid="a-9")]}
    assert ra.plan_release_alerts(ticket, []) == [
        {"resolved": True, "resolves_uuid": "a-9", "reason": "release-version-missing:2.4"}
    ]


def test_archived_reason() -> None:
    assert ra.plan_release_alerts({}, [{"release": "2.4", "reason": "archived"}]) == [
        {"reason": "release-version-archived:2.4", "tag": "release-version-archived"}
    ]


def test_reason_change_swaps_the_alert_instead_of_keeping_both() -> None:
    ticket = {"bridge_alerts": [_alert("release-version-archived:2.4", uuid="a-1")]}
    assert ra.plan_release_alerts(ticket, [_missing("2.4")]) == [
        {"reason": "release-version-missing:2.4", "tag": "release-version-missing"},
        {"resolved": True, "resolves_uuid": "a-1", "reason": "release-version-archived:2.4"},
    ]


def test_resolved_and_unrelated_alerts_are_ignored() -> None:
    ticket = {
        "bridge_alerts": [
            _alert("release-version-missing:2.4", resolved=True),
            _alert("outbound-field-conflict:REB-1:title"),
        ]
    }
    assert ra.plan_release_alerts(ticket, []) == []


# ── read_release_availability ────────────────────────────────────────────────


class _Client:
    def __init__(self, versions: dict[str, Any]) -> None:
        self._versions = versions
        self.reads: list[str] = []

    def get_project_versions(self, project: str) -> list[dict]:
        self.reads.append(project)
        found = self._versions[project]
        if isinstance(found, Exception):
            raise found
        return found


def test_availability_maps_names_and_skips_unreadable_projects() -> None:
    client = _Client(
        {
            "REB": [{"name": "2.4", "archived": False}, {"name": "1.0", "archived": True}],
            "OPS": RuntimeError("503"),
        }
    )
    got = ra.read_release_availability(client, ["REB", "OPS", "REB"])  # type: ignore[arg-type]
    assert got == {"REB": {"2.4": "available", "1.0": "archived"}}
    assert client.reads == ["OPS", "REB"]  # once per project


def test_projects_needing_availability() -> None:
    tickets = [
        {"ticket_id": "a", "releases": ["2.4"]},
        {"ticket_id": "b", "releases": []},
        {"ticket_id": "c", "bridge_alerts": [_alert("release-version-missing:9")]},
        {"ticket_id": "d", "releases": ["2.4"]},  # unbound
    ]
    keys = {"a": "REB-1", "b": "OPS-1", "c": "ENG-7"}
    assert ra.projects_needing_availability(tickets, keys.get) == {"REB", "ENG"}


# ── write_release_alerts ─────────────────────────────────────────────────────


def test_write_is_best_effort_per_ticket(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from rebar.reconciler import inbound_translate

    written: list[tuple] = []

    def _fake_write(tracker_dir, ticket_id, event_type, data):
        if ticket_id == "bad":
            raise TimeoutError("lock")
        written.append((ticket_id, event_type, data))
        return Path("x")

    monkeypatch.setattr(inbound_translate, "_write_event_file", _fake_write)
    sink = {"bad": ("REB-1", [_missing("1")]), "ok": ("REB-2", [_missing("2")])}
    local = {"bad": {"bridge_alerts": []}, "ok": {"bridge_alerts": []}}
    assert ra.write_release_alerts(sink, local, Path("t")) == 1
    assert written == [
        (
            "ok",
            "BRIDGE_ALERT",
            {"reason": "release-version-missing:2", "tag": "release-version-missing"},
        )
    ]
    assert "BRIDGE_ALERT write for bad failed" in capsys.readouterr().err


# ── The bridge never creates a Jira version (static scan of every reconciler module) ──


def test_no_reconciler_code_creates_a_jira_version() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "rebar" / "reconciler"
    create = re.compile(r"create_version|/rest/api/\d+/version['\"/]")
    hits = [
        f"{p.relative_to(root)}:{n}"
        for p in sorted(root.rglob("*.py"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if create.search(line)
    ]
    assert hits == []
