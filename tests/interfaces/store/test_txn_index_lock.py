"""The transition/claim write path also self-heals a git ``index.lock`` (ticket
snide-cut-mussel: *ticket writes* — not just event appends — must be resilient).

Transitions and claims commit through ``txn.py``'s own ``_git`` helper, a separate write
path from ``event_append``. A stale/contended ``index.lock`` on the shared tickets worktree
must be reclaimed-if-stale / ridden-out here too, or a concurrent claim/transition still
fails hard.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import rebar
from rebar import config
from rebar._store import event_append

_STALE_S = getattr(event_append, "_INDEX_LOCK_STALE_S", 300)


def _fresh_repo(tmp_path: Path) -> tuple[str, str]:
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return str(repo), str(config.tracker_dir(str(repo)))


def _index_lock_path(tracker: str) -> Path:
    p = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-path", "index.lock"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(p) if os.path.isabs(p) else Path(tracker) / p


def test_transition_reclaims_stale_index_lock(tmp_path: Path) -> None:
    repo, tracker = _fresh_repo(tmp_path)
    tid = rebar.create_ticket("task", "t", repo_root=repo)

    lock = _index_lock_path(tracker)
    lock.write_text("")
    old = time.time() - (_STALE_S + 60)
    os.utime(lock, (old, old))

    # A stale lock must not make a transition fail hard — it self-heals.
    rebar.transition(tid, "open", "in_progress", repo_root=repo)

    from rebar import show_ticket

    assert show_ticket(tid, repo_root=repo)["status"] == "in_progress"
    assert not lock.exists(), "stale lock should have been reclaimed by the transition write"
