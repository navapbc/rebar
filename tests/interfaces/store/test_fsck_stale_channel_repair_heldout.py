"""Adversarial tests withheld from the stale-channel repair implementer."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fsck_stale_channel_harness import write_snapshot_ticket

from rebar._commands import fsck, fsck_repair
from rebar._store import ensures


@pytest.mark.parametrize("fault", ["orphan", "inconsistent"])
def test_stale_only_refuses_mixed_fault_tickets(tmp_path: Path, fault: str) -> None:
    ticket_dir, snapshot = write_snapshot_ticket(
        tmp_path,
        f"mixed-{fault}",
        stale_channel=True,
        orphan_type="LINK" if fault == "orphan" else None,
    )
    if fault == "inconsistent":
        source_uuid = "44444444-aaaa-4bbb-8ccc-000000000004"
        source = ticket_dir / f"1500000000000000000-{source_uuid}-COMMENT.json"
        source.write_text(
            json.dumps({"event_type": "COMMENT", "uuid": source_uuid}), encoding="utf-8"
        )
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        payload["data"]["source_event_uuids"].append(source_uuid)
        snapshot.write_text(json.dumps(payload), encoding="utf-8")

    before = {path.name: path.read_bytes() for path in ticket_dir.iterdir()}
    lines, unresolved = fsck._repair_run(str(tmp_path), dry_run=True, only="stale-channel")

    report = "\n".join(lines)
    assert unresolved == -1
    assert f"REFUSE mixed-{fault}" in report
    assert "ORPHAN_EVENT" in report if fault == "orphan" else "SNAPSHOT_INCONSISTENT" in report
    assert {path.name: path.read_bytes() for path in ticket_dir.iterdir()} == before


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--only=stale-channel"], "requires --repair"),
        (["--repair", "--only=unknown"], "unknown --only value"),
        (["--repair", "--only"], "requires a value"),
        (
            ["--repair", "--only=stale-channel", "--only=stale-channel"],
            "specified only once",
        ),
    ],
)
def test_only_parser_rejects_misuse(argv: list[str], message: str, capsys) -> None:
    assert fsck.fsck_cli(argv) == 2
    assert message in capsys.readouterr().err


def test_default_repair_does_not_select_stale_channel_only_ticket(tmp_path: Path) -> None:
    write_snapshot_ticket(tmp_path, "stale-default", stale_channel=True)

    lines, unresolved = fsck._repair_run(str(tmp_path), dry_run=True)

    assert unresolved == 0
    assert lines == ["a3-remediation: no repairable faults"]


def test_stale_only_cli_skips_unrelated_ensure_sweep(tmp_path: Path, monkeypatch, capsys) -> None:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.name", "Test Operator")
    _git(tracker, "config", "user.email", "test@example.invalid")
    ticket_dir, snapshot = write_snapshot_ticket(tracker, "stale-cli", stale_channel=True)
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "-m", "seed stale snapshot")

    def _rebuild(_tracker, _ticket_id, _ticket_dir, *, no_commit=False):
        assert no_commit is True
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        payload["data"]["compiled_state"]["creation_channel"] = "python"
        snapshot.write_text(json.dumps(payload), encoding="utf-8")
        return True

    monkeypatch.setattr("rebar._commands.compact.rebuild_snapshot_from_full_log", _rebuild)
    monkeypatch.setattr(fsck_repair, "_reconciler_pause", lambda repo_root=None: False)
    monkeypatch.setattr(fsck_repair, "_reconciler_in_flight", lambda repo_root=None: False)

    def _unexpected_ensure(_tracker):
        raise AssertionError("the stale-channel-only repair must not run ensures")

    monkeypatch.setattr(ensures, "run_ensures", _unexpected_ensure)

    rc = fsck.fsck_cli(["--repair", "--only=stale-channel"], repo_root=str(tmp_path))

    assert rc == 0
    assert "0 stale-channel fault(s) remain" in capsys.readouterr().out
    assert not any(
        "SNAPSHOT_STALE_CHANNEL" in finding
        for finding in fsck._check_snapshot(str(ticket_dir), "stale-cli", snapshot.name)
    )


def test_stale_only_limit_is_deterministic_and_resumable(tmp_path: Path) -> None:
    write_snapshot_ticket(tmp_path, "a-stale", stale_channel=True)
    write_snapshot_ticket(tmp_path, "b-stale", stale_channel=True)

    lines, unresolved = fsck._repair_run(str(tmp_path), dry_run=True, limit=1, only="stale-channel")

    report = "\n".join(lines)
    assert unresolved == 0
    assert "DRY-RUN a-stale" in report
    assert "DRY-RUN b-stale" not in report
    assert "1/2 ticket(s) would be repaired" in report


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def test_stale_only_push_failure_keeps_pretag_for_disposable_rollback(
    tmp_path: Path, monkeypatch
) -> None:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.name", "Test Operator")
    _git(tracker, "config", "user.email", "test@example.invalid")
    ticket_dir, snapshot = write_snapshot_ticket(tracker, "stale-push", stale_channel=True)
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "-m", "seed stale snapshot")
    original = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    def _rebuild(_tracker, _ticket_id, _ticket_dir, *, no_commit=False):
        assert no_commit is True, "stale-only repair must leave commits to the batch driver"
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        payload["data"]["compiled_state"]["creation_channel"] = "python"
        snapshot.write_text(json.dumps(payload), encoding="utf-8")
        return True

    monkeypatch.setattr("rebar._commands.compact.rebuild_snapshot_from_full_log", _rebuild)
    monkeypatch.setattr(fsck_repair, "_reconciler_pause", lambda repo_root=None: False)
    monkeypatch.setattr(fsck_repair, "_reconciler_in_flight", lambda repo_root=None: False)
    monkeypatch.setattr(fsck_repair, "_has_remote", lambda _tracker: True)
    monkeypatch.setattr(
        fsck_repair,
        "_git_push",
        lambda *_args: subprocess.CompletedProcess([], 1, "", "rejected"),
    )

    lines, unresolved = fsck._repair_run(str(tracker), dry_run=False, only="stale-channel")

    assert unresolved == -1
    assert "ABORT: push failed" in "\n".join(lines)
    assert _git(tracker, "rev-parse", "pre-a3-remediation").stdout.strip() == original
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() != original
    assert not any(
        "SNAPSHOT_STALE_CHANNEL" in finding
        for finding in fsck._check_snapshot(str(ticket_dir), "stale-push", snapshot.name)
    )

    _git(tracker, "reset", "--hard", "pre-a3-remediation")
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == original
    assert any(
        "SNAPSHOT_STALE_CHANNEL" in finding
        for finding in fsck._check_snapshot(str(ticket_dir), "stale-push", snapshot.name)
    )


def test_failed_stale_rebuild_is_reported_as_remaining(tmp_path: Path, monkeypatch) -> None:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.name", "Test Operator")
    _git(tracker, "config", "user.email", "test@example.invalid")
    write_snapshot_ticket(tracker, "stale-failed", stale_channel=True)
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "-m", "seed stale snapshot")

    monkeypatch.setattr(
        "rebar._commands.compact.rebuild_snapshot_from_full_log",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(fsck_repair, "_reconciler_pause", lambda repo_root=None: False)
    monkeypatch.setattr(fsck_repair, "_reconciler_in_flight", lambda repo_root=None: False)

    lines, unresolved = fsck._repair_run(str(tracker), dry_run=False, only="stale-channel")

    assert unresolved == 1
    assert "1 stale-channel fault(s) remain" in "\n".join(lines)
