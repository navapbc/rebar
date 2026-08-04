"""Happy-path contract for the tickets-store epoch union guard (A6-0a)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rebar._store import compat

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _record(epoch: str | None) -> str:
    body: dict[str, object] = {
        "format_version": compat.CURRENT_FORMAT_VERSION,
        "required_capabilities": [],
    }
    if epoch is not None:
        body["epoch"] = epoch
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def _tracker_with_remote_record(
    tmp_path: Path, *, local_epoch: str | None, remote_epoch: str | None
) -> Path:
    tracker = tmp_path / "tracker"
    subprocess.run(
        ["git", "init", "-q", "-b", "tickets", str(tracker)],
        check=True,
        capture_output=True,
    )
    _git(tracker, "config", "user.email", "t@example.com")
    _git(tracker, "config", "user.name", "T")
    record = tracker / compat.COMPAT_FILENAME
    record.write_text(_record(remote_epoch), encoding="utf-8")
    _git(tracker, "add", compat.COMPAT_FILENAME)
    _git(tracker, "commit", "-q", "--no-verify", "-m", "remote epoch")
    _git(tracker, "update-ref", "refs/remotes/origin/tickets", "HEAD")
    record.write_text(_record(local_epoch), encoding="utf-8")
    return tracker


def test_matching_epoch_allows_union(tmp_path: Path) -> None:
    tracker = _tracker_with_remote_record(
        tmp_path,
        local_epoch="2026-08-14T09-31-07Z-4f2a",
        remote_epoch="2026-08-14T09-31-07Z-4f2a",
    )

    assert compat.store_epoch_problem(tracker, "origin/tickets") is None


def test_epoch_absent_on_both_sides_allows_pre_reclaim_union(tmp_path: Path) -> None:
    tracker = _tracker_with_remote_record(tmp_path, local_epoch=None, remote_epoch=None)

    assert compat.store_epoch_problem(tracker, "origin/tickets") is None


def test_write_compat_record_preserves_existing_string_epoch(tmp_path: Path) -> None:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    record = tracker / compat.COMPAT_FILENAME
    epoch = "2026-08-14T09-31-07Z-4f2a"
    record.write_text(_record(epoch), encoding="utf-8")

    compat.write_compat_record(tracker)

    assert json.loads(record.read_text(encoding="utf-8")) == {
        "epoch": epoch,
        "format_version": compat.CURRENT_FORMAT_VERSION,
        "required_capabilities": [],
    }
