"""Reconverge robustness: a union merge blocked ONLY by origin-introduced untracked paths
must self-heal instead of wedging the store (bug small-delicious-loris / 6ccc-0577-198c-44fa).

Compaction can leave regenerable artifacts (`*-SNAPSHOT.json`, `*.retired`) as UNTRACKED files
in the tracker working tree. When a peer clone has already committed+pushed those same paths,
the next reconverge's ``git merge origin/tickets`` must CREATE them and aborts with
"untracked working tree files would be overwritten by merge". The pre-fix handler was
abort-only, so every retry re-aborted and local commits never reached origin. The fix
quarantines exactly the untracked paths git names (they exist on origin, so they are
regenerable) and retries the merge once, keeping both parents.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import sync


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def _new_tickets_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "tickets", str(path)], check=True)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


def _commit_event(repo: Path, ticket_uuid: str, filename: str, body: str) -> str:
    tdir = repo / ticket_uuid
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / filename).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", f"ticket: {ticket_uuid}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# The path a peer's compaction commits to origin AND that also lingers untracked locally.
_COLLIDE_DIR = "bbbb-orig-2222-2222"
_COLLIDE_FILE = "1700000000000000000-bbbb-orig-2222-2222-SNAPSHOT.json"
_COLLIDE_PATH = f"{_COLLIDE_DIR}/{_COLLIDE_FILE}"


@pytest.fixture
def diverged_with_untracked_collision(tmp_path: Path) -> tuple[Path, str, str]:
    """Tracker shares a base with origin, then BOTH advance (diverged). Origin's new commit
    adds ``_COLLIDE_PATH``; the tracker working tree holds that SAME path as an UNTRACKED
    leftover. Returns (tracker, local_sha, origin_sha)."""
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    _commit_event(
        origin,
        "0000-base-0000-0000",
        "1700000000000000000-0000-base-0000-0000-CREATE.json",
        '{"e":"base"}',
    )

    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")

    # Origin advances: a peer's compaction snapshot lands on origin/tickets.
    origin_sha = _commit_event(origin, _COLLIDE_DIR, _COLLIDE_FILE, '{"side":"origin-committed"}')

    # Tracker diverges with its own local-only commit (a different ticket).
    local_sha = _commit_event(
        tracker,
        "1111-local-1111-1111",
        "1700000000000000000-1111-local-1111-1111-CREATE.json",
        '{"e":"local"}',
    )

    # The compaction leftover: the SAME path origin now adds, sitting UNTRACKED locally.
    leftover = tracker / _COLLIDE_PATH
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text('{"side":"local-untracked-leftover"}')
    assert _git(tracker, "status", "--porcelain", "--", _COLLIDE_PATH).stdout.startswith("??"), (
        "precondition: the colliding path must be untracked in the working tree"
    )
    return tracker, local_sha, origin_sha


def test_reconverge_self_heals_untracked_collision(diverged_with_untracked_collision) -> None:
    """The wedge: a diverged union merge blocked only by an origin-introduced untracked path
    must complete (both parents kept), not abort-and-strand the local commit off origin."""
    tracker, local_sha, origin_sha = diverged_with_untracked_collision

    sync.reconverge(tracker)

    # The merge completed: origin's commit is now reachable from HEAD (pre-fix this ABORTED).
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0, (
        "origin commit not merged — reconverge wedged on the untracked collision"
    )
    # The local commit is never orphaned.
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0, (
        "local commit orphaned by the recovery"
    )
    # HEAD is a real union (two parents).
    parents = _git(tracker, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3, f"expected a 2-parent merge commit, got parents={parents}"
    # Origin's authoritative version of the colliding path is now tracked in HEAD.
    assert _git(tracker, "cat-file", "-e", f"HEAD:{_COLLIDE_PATH}").returncode == 0, (
        "origin's committed colliding file is not present in the merged tree"
    )
    # Data-safety: the local commit survives an aggressive prune (reachable, not reflog-only).
    _git(tracker, "gc", "--prune=now", "--quiet")
    assert _git(tracker, "cat-file", "-e", local_sha).returncode == 0, (
        "local commit was collectible — it was orphaned, not merged"
    )


def test_reconverge_untracked_leftover_is_preserved_not_destroyed(
    diverged_with_untracked_collision,
) -> None:
    """The relocated untracked leftover must be MOVED aside (recoverable), never deleted:
    its original bytes survive somewhere under the git dir after recovery."""
    tracker, _local_sha, _origin_sha = diverged_with_untracked_collision

    sync.reconverge(tracker)

    common_dir = Path(_git(tracker, "rev-parse", "--git-common-dir").stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (tracker / common_dir).resolve()
    preserved = [
        p
        for p in common_dir.rglob("*")
        if p.is_file() and p.read_text(errors="ignore") == '{"side":"local-untracked-leftover"}'
    ]
    assert preserved, (
        "the untracked leftover's bytes were not preserved anywhere under the git dir — "
        "recovery must relocate (not delete) it"
    )


def test_reconverge_genuine_content_conflict_still_aborts_and_keeps_local(tmp_path: Path) -> None:
    """Guard against over-reach: a real CONTENT conflict on a tracked shared file must still
    abort, keep local, and NOT absorb origin (unchanged pre-fix safety behavior). This is the
    mutation-direction fence — the fix must recover ONLY the untracked-overwrite class."""
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    # A shared, tracked, MUTABLE root file both sides will edit incompatibly.
    (origin / "shared.txt").write_text("base\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "--no-verify", "-m", "base shared file")

    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")

    (origin / "shared.txt").write_text("origin-change\n")
    _git(origin, "add", "-A")
    origin_sha = (
        _git(origin, "commit", "-q", "--no-verify", "-m", "origin edit")
        or _git(origin, "rev-parse", "HEAD").stdout.strip()
    )
    origin_sha = _git(origin, "rev-parse", "HEAD").stdout.strip()

    (tracker / "shared.txt").write_text("local-change\n")
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "local edit")
    local_sha = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    sync.reconverge(tracker)

    # Local is kept and never orphaned; origin's conflicting commit is NOT force-absorbed.
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode != 0, (
        "a genuine content conflict must NOT be silently merged by the untracked-collision path"
    )
    # No merge left in progress.
    assert not (tracker / ".git" / "MERGE_HEAD").exists()
