"""Regression: a store write must not be permanently wedged by a PRE-EXISTING
unmerged (UU) index entry on a reconciler-regenerable .bridge_state/* file
(bug 6818 / filmy-basin-chasm, the "store writes wedged" half).

Before the fix, any `git commit` refused while an unmerged path existed, so an
event append raised the cryptic "git commit failed while holding lock" and the
tracker stayed wedged until manual recovery. Because .bridge_state/* files are
reconciler-REGENERABLE, the write path should self-heal such an entry (restore it
to HEAD) and complete the commit — no manual intervention.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import event_append

pytestmark = pytest.mark.unit


def _git(d, *a, _in=None, check=True):
    r = subprocess.run(
        ["git", "-C", str(d), *a], input=_in, capture_output=True, text=True, check=False
    )
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


def _hash_object(tracker, content: str) -> str:
    return _git(tracker, "hash-object", "-w", "--stdin", _in=content).stdout.strip()


@pytest.fixture
def tracker_with_unmerged_bridge_state(tmp_path: Path) -> str:
    """A tracker whose index has a pre-existing UU (stages 1/2/3) on
    .bridge_state/prev_snapshot.json — NO in-progress merge — exactly the end state
    a stranded stash-pop leaves. The working-tree copy carries conflict markers."""
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
    # Synthesize an unmerged index entry (stages 1/2/3) with no MERGE_HEAD.
    rel = ".bridge_state/prev_snapshot.json"
    b1, b2, b3 = (_hash_object(td, c) for c in ('{"base":1}\n', '{"ours":2}\n', '{"theirs":3}\n'))
    info = f"100644 {b1} 1\t{rel}\n100644 {b2} 2\t{rel}\n100644 {b3} 3\t{rel}\n"
    _git(td, "update-index", "--index-info", _in=info)
    (bs / "prev_snapshot.json").write_text(
        '<<<<<<< ours\n{"ours":2}\n=======\n{"theirs":3}\n>>>>>>> theirs\n'
    )
    assert _git(td, "ls-files", "-u", rel).stdout.strip() != "", "fixture must create a UU"
    return str(td)


def test_write_self_heals_preexisting_unmerged_bridge_state(
    tracker_with_unmerged_bridge_state: str,
) -> None:
    tracker = tracker_with_unmerged_bridge_state
    event = {
        "timestamp": 1700000000000000000,
        "uuid": "u-1",
        "event_type": "COMMENT",
        "env_id": "e",
        "author": "a",
        "data": {"body": "x"},
    }
    # Must NOT raise the cryptic "git commit failed while holding lock"; the write
    # self-heals the regenerable unmerged path and the event commit lands.
    rc = event_append.stage_and_commit(tracker, "tk", dict(event))
    assert rc == 0

    # The unmerged entry is gone (index consistent → tracker writable again).
    assert _git(tracker, "ls-files", "-u").stdout.strip() == ""
    # The event actually committed.
    fn = event_append.event_filename(event["timestamp"], event["uuid"], "COMMENT")
    assert (Path(tracker) / "tk" / fn).exists()


# ── bug 2fa6: a path the tickets branch does not track is FOREIGN, not ticket data ────────
def _tracker_with_unmerged(tmp_path: Path, rel: str, *, seed_path: bool) -> str:
    """A tracker with a stranded UU on ``rel`` and NO MERGE_HEAD. When ``seed_path`` the
    path's top-level component is committed first (so the branch tracks it)."""
    td = tmp_path / "trk"
    td.mkdir()
    _git(td, "init", "-q", "-b", "tickets")
    _git(td, "config", "user.email", "t@e.com")
    _git(td, "config", "user.name", "T")
    (td / ".bridge_state").mkdir()
    (td / ".bridge_state" / "prev_snapshot.json").write_text("{}\n")
    if seed_path:
        p = td / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("seed\n")
    _git(td, "add", "-A")
    _git(td, "commit", "-q", "-m", "seed")
    b1, b2, b3 = (_hash_object(td, c) for c in ("base\n", "ours\n", "theirs\n"))
    info = f"100644 {b1} 1\t{rel}\n100644 {b2} 2\t{rel}\n100644 {b3} 3\t{rel}\n"
    _git(td, "update-index", "--index-info", _in=info)
    f = td / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\n")
    assert _git(td, "ls-files", "-u", rel).stdout.strip() != "", "fixture must create a UU"
    return str(td)


def _event(uuid: str = "u-2") -> dict:
    return {
        "timestamp": 1700000000000000000,
        "uuid": uuid,
        "event_type": "COMMENT",
        "env_id": "e",
        "author": "a",
        "data": {"body": "x"},
    }


def test_write_self_heals_unmerged_path_foreign_to_the_branch(tmp_path: Path) -> None:
    """A `git stash pop` in the tickets worktree can apply a stash created in a SOURCE
    worktree (the stash stack is shared across worktrees), dropping `src/...` into the store.
    Such a path is not reconciler-regenerable, but it is also NOT ticket data — the tickets
    branch tracks no source tree — so it must be discarded, not left to wedge every write.
    Uses the exact path observed on the live store.
    """
    rel = "src/rebar/llm/plan_review/det_floor.py"
    tracker = _tracker_with_unmerged(tmp_path, rel, seed_path=False)

    rc = event_append.stage_and_commit(tracker, "tk", _event())

    assert rc == 0, "a foreign unmerged path must not wedge the write"
    assert _git(tracker, "ls-files", "-u").stdout.strip() == "", "index must be consistent"
    assert not (Path(tracker) / rel).exists(), "the foreign file itself must be gone"
    fn = event_append.event_filename(1700000000000000000, "u-2", "COMMENT")
    assert (Path(tracker) / "tk" / fn).exists(), "the event must have committed"


def test_write_still_refuses_unmerged_path_the_branch_tracks(tmp_path: Path) -> None:
    """The guard must not weaken: a path the branch DOES track is real ticket data and is
    never auto-discarded — the write refuses and surfaces an actionable message."""
    rel = "tk/1700000000000000000-u-9-COMMENT.json"
    tracker = _tracker_with_unmerged(tmp_path, rel, seed_path=True)

    with pytest.raises(Exception) as exc:
        event_append.stage_and_commit(tracker, "tk", _event("u-3"))

    assert "stranded merge/stash" in str(exc.value), str(exc.value)
    assert rel in str(exc.value), "the offending ticket path must be named"
