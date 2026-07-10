"""Write-time op batching (epic cold-stall-chalk / B1): ``batch_stage_and_commit``.

The batch primitive commits MANY events under ONE lock acquire + ONE ``git commit``
(all-or-nothing) instead of one commit per event. These tests pin the properties that
make that collapse safe:

- N events land in ONE commit, byte-identical to what the per-event path would write;
- an empty batch is a no-op (no lock, no commit);
- any failure (validation, rename, ``git add``, non-recoverable ``git commit``) rolls
  the WHOLE batch back — no phantom event left staged in the index or on disk;
- a subsequent successful write is uncontaminated by a failed batch;
- the pre-existing unmerged (UU) self-heal (bug 6818) still applies to a batch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import event_append
from rebar._store.canonical import canonical_bytes

pytestmark = pytest.mark.unit


def _git(d, *a, _in=None, check=True):
    r = subprocess.run(["git", "-C", str(d), *a], input=_in, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


def _event(uuid: str, ts: int, body: str | None = None) -> dict:
    return {
        "timestamp": ts,
        "uuid": uuid,
        "event_type": "COMMENT",
        "env_id": "e",
        "author": "a",
        "data": {"body": body if body is not None else uuid},
    }


@pytest.fixture
def tracker(tmp_path: Path) -> str:
    td = tmp_path / "trk"
    td.mkdir()
    _git(td, "init", "-q", "-b", "tickets")
    _git(td, "config", "user.email", "t@e.com")
    _git(td, "config", "user.name", "T")
    (td / "seed").write_text("seed\n")
    _git(td, "add", "-A")
    _git(td, "commit", "-q", "-m", "seed")
    return str(td)


def _commit_count(tracker: str) -> int:
    return int(_git(tracker, "rev-list", "--count", "HEAD").stdout.strip())


def test_batch_commits_all_events_in_a_single_commit(tracker):
    base = _commit_count(tracker)
    items = [
        ("tk1", _event("u-A", 1700000000000000000)),
        ("tk1", _event("u-B", 1700000000000000001)),
        ("tk2", _event("u-C", 1700000000000000002)),
    ]
    n = event_append.batch_stage_and_commit(tracker, items)
    assert n == 3

    # Exactly ONE new commit for the whole batch (not one per event).
    assert _commit_count(tracker) == base + 1

    # All three event files are present in the new commit.
    committed = _git(tracker, "ls-tree", "-r", "--name-only", "HEAD").stdout
    for ticket_id, ev in items:
        fn = event_append.event_filename(ev["timestamp"], ev["uuid"], "COMMENT")
        assert f"{ticket_id}/{fn}" in committed

    # No temp/staging residue and a clean index.
    assert _git(tracker, "diff", "--cached", "--name-only").stdout.strip() == ""
    assert not list(Path(tracker).glob(".tmp-event-*"))


def test_batch_bytes_are_canonical_and_identical_to_single_path(tracker):
    ev = _event("u-canon", 1700000000000000010)
    event_append.batch_stage_and_commit(tracker, [("tk", ev)])
    fn = event_append.event_filename(ev["timestamp"], ev["uuid"], "COMMENT")
    on_disk = (Path(tracker) / "tk" / fn).read_bytes()
    assert on_disk == canonical_bytes(ev)


def test_empty_batch_is_a_noop(tracker):
    base = _commit_count(tracker)
    assert event_append.batch_stage_and_commit(tracker, []) == 0
    assert _commit_count(tracker) == base


def test_validation_failure_rolls_back_before_any_commit(tracker, monkeypatch):
    base = _commit_count(tracker)
    calls: list[str] = []
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and ("add" in cmd[:6] or "commit" in cmd[:6]):
            calls.append(cmd[3] if len(cmd) > 3 else "?")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)

    bad = {"uuid": "u-bad", "timestamp": 1, "event_type": "NOPE", "data": {}}
    with pytest.raises(event_append.StoreError):
        event_append.batch_stage_and_commit(
            tracker, [("tk", _event("u-ok", 1700000000000000020)), ("tk", bad)]
        )

    # Nothing committed, no add/commit reached, no staging residue left behind.
    assert _commit_count(tracker) == base
    assert calls == []
    assert not list(Path(tracker).glob(".tmp-event-*"))


def test_commit_failure_rolls_back_whole_batch_and_next_write_is_clean(tracker, monkeypatch):
    base = _commit_count(tracker)
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and "commit" in cmd[:6]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="injected commit failure")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)
    with pytest.raises(event_append.StoreError):
        event_append.batch_stage_and_commit(
            tracker,
            [
                ("tk", _event("u-A", 1700000000000000030)),
                ("tk", _event("u-B", 1700000000000000031)),
            ],
        )

    monkeypatch.undo()
    # No commit, a clean index (no phantom staged blobs), and the worktree files gone.
    assert _commit_count(tracker) == base
    assert _git(tracker, "diff", "--cached", "--name-only").stdout.strip() == ""
    for uuid in ("u-A", "u-B"):
        fn = event_append.event_filename(
            1700000000000000030 if uuid == "u-A" else 1700000000000000031, uuid, "COMMENT"
        )
        assert not (Path(tracker) / "tk" / fn).exists()

    # A subsequent successful write commits ONLY its own event — not the failed batch.
    n = event_append.batch_stage_and_commit(tracker, [("tk", _event("u-C", 1700000000000000032))])
    assert n == 1
    committed = _git(tracker, "ls-tree", "-r", "--name-only", "HEAD", "tk").stdout
    assert "u-C" in committed
    assert "u-A" not in committed and "u-B" not in committed


def test_batch_self_heals_preexisting_unmerged_bridge_state(tmp_path):
    td = tmp_path / "trk"
    td.mkdir()
    _git(td, "init", "-q", "-b", "tickets")
    _git(td, "config", "user.email", "t@e.com")
    _git(td, "config", "user.name", "T")
    bs = td / ".bridge_state"
    bs.mkdir()
    (bs / "prev_snapshot.json").write_text('{"clean": true}\n')
    _git(td, "add", "-A")
    _git(td, "commit", "-q", "-m", "seed")
    rel = ".bridge_state/prev_snapshot.json"
    b1, b2, b3 = (
        _git(td, "hash-object", "-w", "--stdin", _in=c).stdout.strip()
        for c in ('{"base":1}\n', '{"ours":2}\n', '{"theirs":3}\n')
    )
    info = f"100644 {b1} 1\t{rel}\n100644 {b2} 2\t{rel}\n100644 {b3} 3\t{rel}\n"
    _git(td, "update-index", "--index-info", _in=info)
    (bs / "prev_snapshot.json").write_text("<<<<<<< ours\n=======\n>>>>>>> theirs\n")
    tracker = str(td)

    n = event_append.batch_stage_and_commit(
        tracker,
        [
            ("tk", _event("u-A", 1700000000000000040)),
            ("tk", _event("u-B", 1700000000000000041)),
        ],
    )
    assert n == 2
    # The unmerged entry is healed and both batch events landed in one commit.
    assert _git(tracker, "ls-files", "-u").stdout.strip() == ""
    for uuid, ts in (("u-A", 1700000000000000040), ("u-B", 1700000000000000041)):
        assert (Path(tracker) / "tk" / event_append.event_filename(ts, uuid, "COMMENT")).exists()
