"""Regression: the push-retry stash→merge→pop dance must not strand a conflict
(bug 6818 / filmy-basin-chasm).

On the clean-merge2 path, `git stash pop` (push.py) was run unconditionally and its
return code ignored. When the stashed uncommitted edit to a tracked `.bridge_state/*`
file collides with the freshly-merged upstream copy, the pop applies-with-conflict:
it leaves `<<<<<<< Updated upstream … >>>>>>> Stashed changes` markers in the working
tree and an unmerged (UU, stages 1/2/3) index entry, and keeps the stash. Because
merge2 succeeded (rc 0), the `merge --abort` cleanup is skipped, so nothing repairs
it. That wedged reconcile (the prev_snapshot read-guard fail-closes) AND every store
write (`git commit` refuses an unmerged path → "git commit failed while holding lock").

The dance must leave the worktree CONSISTENT — no markers, no UU, writable, valid JSON.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rebar._store import push

pytestmark = pytest.mark.unit


def _git(d, *a, check=True):
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


def _ident(d) -> None:
    _git(d, "config", "user.email", "t@e.com")
    _git(d, "config", "user.name", "T")
    _git(d, "config", "gc.auto", "0")


def _snap(region_a: str, region_b: str) -> str:
    """A JSON blob with two well-separated regions, so a change to region B merges
    cleanly against an upstream change to region A (no textual collision at merge),
    while a stashed region-A edit DOES collide with the merged-upstream region A."""
    lines = ["{", f'  "regionA": "{region_a}",']
    lines += [f'  "pad{i}": {i},' for i in range(1, 40)]
    lines += [f'  "regionB": "{region_b}"', "}"]
    return "\n".join(lines) + "\n"


@pytest.fixture
def diverged_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A tracker clone in the exact bug shape:
    - origin (upstream) is AHEAD with a change to region A of a tracked .bridge_state file;
    - local committed a clean-merging change to region B (non-FF vs origin);
    - an UNCOMMITTED working-tree edit to region A (collides with upstream's region A).
    Driving ``push_tickets_branch`` here exercises stash → clean-merge2 → pop-conflict.
    """
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    origin, tracker, up = tmp_path / "origin.git", tmp_path / "tracker", tmp_path / "upstream"
    subprocess.run(
        ["git", "init", "--bare", "-b", "tickets", str(origin)], check=True, capture_output=True
    )
    subprocess.run(["git", "clone", str(origin), str(tracker)], check=True, capture_output=True)
    _ident(tracker)
    _git(tracker, "checkout", "-q", "-b", "tickets")
    bs = tracker / ".bridge_state"
    bs.mkdir()
    snap = bs / "prev_snapshot.json"
    snap.write_text(_snap("BASE", "BASE"))
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "-m", "seed")
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    # upstream advances region A and pushes (origin diverges → non-FF)
    subprocess.run(["git", "clone", str(origin), str(up)], check=True, capture_output=True)
    _ident(up)
    (up / ".bridge_state" / "prev_snapshot.json").write_text(_snap("UPSTREAM", "BASE"))
    _git(up, "add", "-A")
    _git(up, "commit", "-q", "-m", "upstream region A")
    _git(up, "push", "-q", "origin", "HEAD:tickets")
    # local commits region B (clean-merges vs upstream's region A)
    snap.write_text(_snap("BASE", "LOCAL"))
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "-m", "local region B")
    # uncommitted WT edit to region A — the stash that will conflict on pop
    snap.write_text(_snap("LOCAL_WT_A", "LOCAL"))
    return str(tracker)


def test_push_retry_pop_conflict_leaves_consistent_worktree(diverged_tracker: str) -> None:
    snap = Path(diverged_tracker) / ".bridge_state" / "prev_snapshot.json"
    assert "prev_snapshot.json" in _git(diverged_tracker, "status", "--porcelain").stdout

    push.push_tickets_branch(diverged_tracker)  # best-effort; never raises

    content = snap.read_text()
    assert "<<<<<<<" not in content and ">>>>>>>" not in content, (
        "push-retry stash-pop stranded conflict markers in prev_snapshot.json"
    )
    unmerged = _git(diverged_tracker, "ls-files", "-u", ".bridge_state/prev_snapshot.json").stdout
    assert unmerged.strip() == "", "push-retry left an unmerged (UU) index entry"
    # The reconcile read-guard json.loads() must not fail-closed on this file.
    json.loads(content)
    # The store must remain WRITABLE — a commit must not be blocked by an unmerged path
    # (this is the "git commit failed while holding lock" symptom).
    (Path(diverged_tracker) / "after.txt").write_text("x")
    _git(diverged_tracker, "add", "-A")
    commit = _git(diverged_tracker, "commit", "-m", "post-push write", check=False)
    assert commit.returncode == 0, f"store write blocked after push-retry: {commit.stderr}"


def test_async_push_spawn_failure_is_logged(tmp_path, monkeypatch, caplog):
    """audit 3.2: a failed detached async-push spawn must be logged, not swallowed."""
    monkeypatch.setenv("REBAR_SYNC_PUSH", "async")

    def _boom(*a, **k):
        raise OSError("no resources to fork")

    monkeypatch.setattr(push.subprocess, "Popen", _boom)
    with caplog.at_level("WARNING"):
        push.push_tickets_branch(str(tmp_path))  # best-effort; must not raise
    assert "async tickets-branch push spawn failed" in caplog.text


def test_push_retry_merge_skipped_when_write_lock_busy(
    diverged_tracker: str, monkeypatch, caplog
) -> None:
    """audit reliability #2: when the write lock is held, the push-retry merge is skipped
    (push stays pending) rather than racing a concurrent write — and never raises."""
    from rebar._store import lock as _lock

    def _busy(*a, **k):
        raise _lock.LockTimeout("held by a concurrent writer")

    monkeypatch.setattr("rebar._store.lock.write_lock", _busy)
    head_before = _git(diverged_tracker, "rev-parse", "HEAD").stdout.strip()

    with caplog.at_level("WARNING"):
        push.push_tickets_branch(diverged_tracker)  # best-effort; must not raise

    # The merge was skipped: HEAD is unchanged and there is no in-progress merge.
    assert _git(diverged_tracker, "rev-parse", "HEAD").stdout.strip() == head_before
    assert not (Path(diverged_tracker) / ".git" / "MERGE_HEAD").exists()
    assert "push stays pending" in caplog.text
    # The store remains writable — the skipped merge did not wedge the tracker.
    (Path(diverged_tracker) / "after.txt").write_text("x")
    _git(diverged_tracker, "add", "-A")
    assert _git(diverged_tracker, "commit", "-m", "post", check=False).returncode == 0
