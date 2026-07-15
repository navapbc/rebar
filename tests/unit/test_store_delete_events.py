"""Regression: sidecar retention prunes delete through the canonical locked write path.

Bug malevolent-emigratory-umbrette. The four LLM sidecar ``prune()``s used to run a raw
``git rm`` + a WHOLE-INDEX ``git commit`` via ``subprocess.run`` — no write lock, no
rebase guard, no index-lock retry — which races normal store writes in the shared tracker.
``event_append.delete_events`` is the lock-respecting delete primitive they now share: it
holds the write lock, checks the rebase guard, and commits PATHSPEC-scoped so it can never
sweep an unrelated staged event under its own ``prune:`` message.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import event_append

pytestmark = pytest.mark.unit


def _git(d, *a, check=True):
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


def _event(uuid: str, ts: int) -> dict:
    return {
        "timestamp": ts,
        "uuid": uuid,
        "event_type": "REVIEW_RESULT",
        "env_id": "e",
        "author": "a",
        "data": {"schema": "plan_review_result_v1", "n": uuid},
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


def _commit_events(tracker: str, ticket: str, n: int) -> list[str]:
    """Commit *n* REVIEW_RESULT events and return their relative paths, oldest first."""
    rels = []
    for i in range(n):
        ev = _event(f"u-{i}", 1700000000000000000 + i)
        event_append.stage_and_commit(tracker, ticket, ev)
        fn = event_append.event_filename(ev["timestamp"], ev["uuid"], "REVIEW_RESULT")
        rels.append(f"{ticket}/{fn}")
    return rels


def test_deletes_only_named_events_and_commits(tracker):
    rels = _commit_events(tracker, "tk", 4)
    to_delete = rels[:2]  # prune the two oldest, keep two

    n = event_append.delete_events(tracker, to_delete, "prune: REVIEW_RESULT sidecar for tk")
    assert n == 2

    committed = _git(tracker, "ls-tree", "-r", "--name-only", "HEAD", "tk").stdout
    for rel in to_delete:
        assert Path(rel).name not in committed  # deleted
    for rel in rels[2:]:
        assert Path(rel).name in committed  # retained
    # The deletion is committed (a real prune commit exists at HEAD).
    assert "prune: REVIEW_RESULT" in _git(tracker, "log", "-1", "--format=%s").stdout


def test_does_not_sweep_unrelated_staged_file(tracker):
    """The core anti-race property: a concurrent writer's staged blob (in the index but not
    yet committed) must NOT be swept into the prune commit by delete_events."""
    rels = _commit_events(tracker, "tk", 3)

    # Simulate a concurrent writer that has staged (git add) an event but not committed it.
    foreign = Path(tracker) / "other" / "foreign.json"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("{}")
    _git(tracker, "add", "--", "other/foreign.json")

    event_append.delete_events(tracker, rels[:1], "prune: REVIEW_RESULT sidecar for tk")

    # The prune commit at HEAD must contain ONLY the deletion, never the foreign blob.
    head_paths = _git(tracker, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout
    assert "other/foreign.json" not in head_paths
    assert Path(rels[0]).name in head_paths  # the deletion IS in the commit
    # The foreign blob is still staged (uncommitted), exactly as the concurrent writer left it.
    assert "other/foreign.json" in _git(tracker, "diff", "--cached", "--name-only").stdout


def test_empty_list_is_noop(tracker):
    _commit_events(tracker, "tk", 1)
    head_before = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    assert event_append.delete_events(tracker, [], "prune: nothing") == 0
    assert event_append.delete_events(tracker, ["", None], "prune: nothing") == 0  # type: ignore[list-item]
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == head_before  # no commit


def test_commit_failure_restores_deletions(tracker, monkeypatch):
    rels = _commit_events(tracker, "tk", 3)
    head_before = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and "commit" in cmd[:6]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="injected commit failure")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)
    with pytest.raises(event_append.StoreError):
        event_append.delete_events(tracker, rels[:1], "prune: REVIEW_RESULT sidecar for tk")

    monkeypatch.undo()
    # No commit landed, and the staged deletion was restored → store is exactly as it was.
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(tracker, "diff", "--cached", "--name-only").stdout.strip() == ""
    assert (Path(tracker) / rels[0]).exists()  # worktree file restored
