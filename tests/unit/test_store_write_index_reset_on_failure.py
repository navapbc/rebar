"""Regression: a failed store write must leave NO phantom event staged in the index.

Audit reliability #4 / audit 2.2. Before the fix, `stage_and_commit`'s failure paths
only unlinked the worktree file — the `git add`-staged blob stayed in the index, so the
NEXT successful write (which commits the whole index) durably committed the failed
write's phantom event. The claim/transition path (txn.py) already reset the index on
failure; the general append path did not. Also, `git commit` used to run before `git
add`'s return code was checked, so a failed add could still let a commit sweep in
unrelated staged residue under this write's message.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import event_append

pytestmark = pytest.mark.unit


def _git(d, *a, _in=None, check=True):
    r = subprocess.run(["git", "-C", str(d), *a], input=_in, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


def _event(uuid: str, ts: int) -> dict:
    return {
        "timestamp": ts,
        "uuid": uuid,
        "event_type": "COMMENT",
        "env_id": "e",
        "author": "a",
        "data": {"body": uuid},
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


def test_commit_failure_resets_index_and_next_write_has_no_phantom(tracker, monkeypatch):
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        # Simulate the commit failing WITHOUT actually committing (so the only residue
        # is the git add-staged blob, which the failure path must reset out of the index).
        if isinstance(cmd, list) and "commit" in cmd[:6]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="injected commit failure")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)
    with pytest.raises(event_append.StoreError):
        event_append.stage_and_commit(tracker, "tk", _event("u-A", 1700000000000000000))

    monkeypatch.undo()  # restore real git for the assertions + the successful write
    # The failed write's blob must NOT be left staged (index clean → no phantom event).
    assert _git(tracker, "diff", "--cached", "--name-only").stdout.strip() == ""

    # A subsequent successful write commits ONLY its own event — not the failed A.
    rc = event_append.stage_and_commit(tracker, "tk", _event("u-B", 1700000000000000001))
    assert rc == 0
    committed = _git(tracker, "ls-tree", "-r", "--name-only", "HEAD", "tk").stdout
    fn_a = event_append.event_filename(1700000000000000000, "u-A", "COMMENT")
    fn_b = event_append.event_filename(1700000000000000001, "u-B", "COMMENT")
    assert fn_b in committed
    assert fn_a not in committed


def test_failed_add_never_runs_commit(tracker, monkeypatch):
    real_run = subprocess.run
    calls: list[str] = []

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list):
            if "add" in cmd[:6]:
                calls.append("add")
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="injected add failure")
            if "commit" in cmd[:6]:
                calls.append("commit")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)
    with pytest.raises(event_append.StoreError):
        event_append.stage_and_commit(tracker, "tk", _event("u-C", 1700000000000000002))

    # The add return code is checked BEFORE commit runs, so commit is never reached.
    assert "add" in calls
    assert "commit" not in calls
