"""Focused contract for the narrow stale-channel fsck repair path."""

from __future__ import annotations

import json
from pathlib import Path

from fsck_stale_channel_harness import write_snapshot_ticket

from rebar._commands import fsck


def test_repair_plan_detects_isolated_stale_channel(tmp_path: Path) -> None:
    ticket_dir, snapshot = write_snapshot_ticket(tmp_path, "stale-only", stale_channel=True)

    plan = fsck._repair_plan(str(ticket_dir), "stale-only")

    assert plan["stale_channel"] == [snapshot.name]
    assert plan["retire"] == []
    assert plan["auto_orphans"] == []
    assert plan["triage_orphans"] == []


def test_stale_channel_only_dry_run_excludes_other_repair_classes(tmp_path: Path) -> None:
    write_snapshot_ticket(tmp_path, "a-stale", stale_channel=True)
    write_snapshot_ticket(tmp_path, "b-orphan", stale_channel=False, orphan_type="LINK")

    lines, unresolved = fsck._repair_run(str(tmp_path), dry_run=True, only="stale-channel")

    report = "\n".join(lines)
    assert unresolved == 0
    assert "DRY-RUN a-stale" in report
    assert "b-orphan" not in report
    assert "stale_channel=1" in report


def test_stale_channel_live_rebuild_clears_finding(tmp_path: Path, monkeypatch) -> None:
    ticket_dir, snapshot = write_snapshot_ticket(tmp_path, "stale-live", stale_channel=True)

    def _rebuild(_tracker, _ticket_id, _ticket_dir, *, no_commit=False):
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        payload["data"]["compiled_state"]["creation_channel"] = "python"
        snapshot.write_text(json.dumps(payload), encoding="utf-8")
        return True

    monkeypatch.setattr("rebar._commands.compact.rebuild_snapshot_from_full_log", _rebuild)

    disposition = fsck._repair_ticket(
        str(tmp_path),
        "stale-live",
        str(ticket_dir),
        dry_run=False,
        repair_stale_channel=True,
        no_commit=True,
    )

    assert disposition["rebuilt"] is True
    assert not any(
        "SNAPSHOT_STALE_CHANNEL" in finding
        for finding in fsck._check_snapshot(str(ticket_dir), "stale-live", snapshot.name)
    )
