"""fsck names an untracked event file in the tracker's working tree as its own kind of row.

Bug topnotch-brazen-slug: the production MCP clone carried 23 pre-compaction event files that
git did not track (their committed copies had been renamed to ``.retired`` by a fold), and
fsck reported each one as ``snapshot_inconsistent`` or ``orphan_event`` -- store-integrity
kinds that point an operator at the SNAPSHOT, not at a stray working-tree file. These tests
seed that exact shape in a temp store and read the real ``fsck --output json`` payload.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from _sandbox_capabilities import configure_repo_git_identity

pytestmark = pytest.mark.unit

_TID = "aaaa-bbbb-cccc-dddd"
_SOURCE_UUID = "11111111-2222-3333-4444-555555555555"
_SOURCE = f"{_TID}/1700000000000000001-{_SOURCE_UUID}-CREATE.json"
_SNAPSHOT = f"{_TID}/1700000000000000009-99999999-8888-7777-6666-555555555555-SNAPSHOT.json"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _folded_store(tmp_path: Path) -> Path:
    """A store whose CREATE was folded into a SNAPSHOT: the source is committed only as
    ``.retired``, and the SNAPSHOT names its uuid in ``source_event_uuids``."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "tickets", str(tracker)], check=True)
    configure_repo_git_identity(tracker, email="t@t", name="t")
    _write(tracker, ".gitignore", ".ticket-write.lock\n.ticket-write.lock.d/\n.cache.json\n")
    create = {"event_type": "CREATE", "uuid": _SOURCE_UUID, "data": {"title": "t"}}
    _write(tracker, _SOURCE + ".retired", json.dumps(create))
    snapshot = {
        "event_type": "SNAPSHOT",
        "data": {"source_event_uuids": [_SOURCE_UUID], "compiled_state": {}},
    }
    _write(tracker, _SNAPSHOT, json.dumps(snapshot))
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "fold")
    return tracker


def _fsck_json(tracker: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> dict:
    from rebar._commands import fsck

    monkeypatch.setattr("rebar.config.tracker_dir", lambda _repo_root=None: tracker)
    fsck.fsck_cli(["--output", "json"], no_mutate=True)
    return json.loads(capsys.readouterr().out)


def test_an_untracked_pre_compaction_event_is_its_own_report_only_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tracker = _folded_store(tmp_path)
    # The production leftover: the pre-fold file is back in the working tree, untracked,
    # while its committed copy is the `.retired` sibling.
    _write(tracker, _SOURCE, (tracker / (_SOURCE + ".retired")).read_text(encoding="utf-8"))

    payload = _fsck_json(tracker, monkeypatch, capsys)

    rows = [i for i in payload["issues"] if i["kind"] == "tracker_dirty_untracked_event"]
    assert len(rows) == 1, payload["issues"]
    assert rows[0]["counted"] is False
    assert rows[0]["paths"] == [_SOURCE]
    assert "retired" in rows[0]["detail"]
    misfiled = [
        i
        for i in payload["issues"]
        if i["kind"] in ("snapshot_inconsistent", "orphan_event") and _SOURCE_UUID in json.dumps(i)
    ]
    assert misfiled == [], f"the untracked file is still reported as a snapshot fault: {misfiled}"


def test_a_tracked_active_source_still_reports_snapshot_inconsistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tracker = _folded_store(tmp_path)
    _write(tracker, _SOURCE, (tracker / (_SOURCE + ".retired")).read_text(encoding="utf-8"))
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "the source is committed and active")

    payload = _fsck_json(tracker, monkeypatch, capsys)

    kinds = [i["kind"] for i in payload["issues"]]
    assert "snapshot_inconsistent" in kinds
    assert "tracker_dirty_untracked_event" not in kinds


def test_the_classifier_names_untracked_events_and_leaves_the_other_classes_alone(
    tmp_path: Path,
) -> None:
    from rebar._commands.fsck_tracker_health import dirty_tracker_classes

    tracker = _folded_store(tmp_path)
    _write(tracker, _SOURCE, "{}")

    classes = dirty_tracker_classes(str(tracker))

    assert classes["untracked_events"] == [_SOURCE]
    assert classes["deletions"] == classes["leftovers"] == classes["tmp_events"] == []
