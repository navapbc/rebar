"""fsck restore reads deleted pre-images by object name only (bug cold-censorable-armedcrab).

``git show <sha>^:<path>`` with no ``--`` makes git stat the argument as a possible filename.
On Windows, when cwd plus that argument passes MAX_PATH, the stat fails with "Filename too
long" and git exits 128, so a legacy-deleted source was never restored. ``git cat-file blob``
takes only an object name, so the argument is never stat'd.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rebar._commands import fsck_restore

_TICKET = "a1b2-c3d4-e5f6-0718"
_UUID = "36d385c1-0b03-4cdd-b64b-de7e466e93d8"
_NAME = f"1795261069748668001-{_UUID}-COMMENT.json"
_SNAP = "9000000000000000000-77777777-7777-4777-8777-777777777777-SNAPSHOT.json"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=T", *args],
        check=True,
        capture_output=True,
    )


def _store_with_deleted_source(repo: Path) -> Path:
    """A tracker whose ticket lost a source to a committed legacy-style deletion."""
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "tickets")
    ticket = repo / _TICKET
    ticket.mkdir()
    (ticket / _NAME).write_bytes(b'{"event_type":"COMMENT","uuid":"' + _UUID.encode() + b'"}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    (ticket / _NAME).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "legacy delete")
    (ticket / _SNAP).write_text(
        json.dumps({"event_type": "SNAPSHOT", "data": {"source_event_uuids": [_UUID]}}),
        encoding="utf-8",
    )
    return ticket


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    real = fsck_restore.run_git

    def spy(tracker, *args, **kwargs):
        calls.append(tuple(args))
        return real(tracker, *args, **kwargs)

    monkeypatch.setattr(fsck_restore, "run_git", spy)
    return calls


def _reads(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [c for c in calls if c and c[0] in ("show", "cat-file")]


def test_the_history_walk_reads_the_pre_image_by_object_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _store_with_deleted_source(tmp_path / "trk")
    calls = _spy(monkeypatch)

    restored = fsck_restore.restore_deleted_sources(str(tmp_path / "trk"), _TICKET, str(ticket))

    assert restored == [_NAME]
    assert (ticket / (_NAME + ".retired")).exists()
    reads = _reads(calls)
    assert reads and all(c[:2] == ("cat-file", "blob") for c in reads), reads


def test_the_per_uuid_fallback_reads_the_pre_image_by_object_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback runs only for what the walk missed, so drive it with an empty walk."""
    ticket = _store_with_deleted_source(tmp_path / "trk")
    monkeypatch.setattr(fsck_restore, "_deleted_history", lambda _t, _id: {})
    calls = _spy(monkeypatch)

    restored = fsck_restore.restore_deleted_sources(str(tmp_path / "trk"), _TICKET, str(ticket))

    assert restored == [_NAME]
    reads = _reads(calls)
    assert reads and all(c[:2] == ("cat-file", "blob") for c in reads), reads


@pytest.mark.skipif(sys.platform != "win32", reason="MAX_PATH applies only on Windows")
def test_restore_succeeds_when_cwd_plus_the_revision_argument_passes_max_path(
    tmp_path: Path,
) -> None:
    """The reproduced condition: every real path stays under MAX_PATH, but cwd + the
    ``<sha>^:<ticket>/<name>`` argument (42 characters longer than the file path) does not."""
    max_path, target = 260, 230
    base = tmp_path / "p"
    rel = f"{_TICKET}\\{_NAME}"
    pad = target - len(str(base)) - 1 - len("trk\\") - len(rel)
    assert pad > 0, f"tmp_path too long to build the case: {tmp_path}"
    repo = base / ("x" * pad) / "trk"
    ticket = _store_with_deleted_source(repo)
    assert len(str(ticket / _NAME)) < max_path
    assert len(str(repo)) + 1 + 42 + len(f"{_TICKET}/{_NAME}") > max_path

    restored = fsck_restore.restore_deleted_sources(str(repo), _TICKET, str(ticket))

    assert restored == [_NAME]
