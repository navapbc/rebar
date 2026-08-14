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


# ---------------------------------------------------------------------------
# Variant (b): "Your local changes to the following files would be overwritten
# by merge" — tracked files with LOCAL WORKING-TREE changes (in practice the
# tracked-deletion half of an interrupted compaction fold's rename) abort the
# union merge before it even starts, stranding local ticket commits off origin
# (live wedge on e72e-259d-5ee7-4e73: 119 tracked `.archived` deletions).
# Empirically (git 2.55 / ort): a STAGED deletion (`D ` — what the fold leaves
# after its `git add -A`) and a worktree MODIFICATION (` M`) both abort; a pure
# worktree deletion (` D`) does not (git recreates the file), but the restore
# helper still handles it for older gits. Recovery: DELETION of a tracked file
# → restore from HEAD (bytes already committed — nothing can be lost);
# worktree MODIFICATION → copy the local bytes into the same quarantine dir
# variant (a) uses, THEN restore. Retry the merge ONCE. Anything else keeps
# the abort-only net.
# ---------------------------------------------------------------------------

_TRACKED_DIR = "cccc-trkd-3333-3333"
_TRACKED_FILE = "1700000000000000000-cccc-trkd-3333-3333-CREATE.json"
_TRACKED_PATH = f"{_TRACKED_DIR}/{_TRACKED_FILE}"
_ORIGIN_BODY = '{"e":"origin-updated"}'


def _diverged_with_tracked_base(tmp_path: Path) -> tuple[Path, str, str]:
    """Shared scaffolding: HEAD tracks ``_TRACKED_PATH``; origin then MODIFIES it
    (origin_sha) while the tracker diverges with its own local-only commit
    (local_sha). Callers then dirty the tracker worktree per scenario."""
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    _commit_event(origin, _TRACKED_DIR, _TRACKED_FILE, '{"e":"base"}')

    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")

    origin_sha = _commit_event(origin, _TRACKED_DIR, _TRACKED_FILE, _ORIGIN_BODY)
    local_sha = _commit_event(
        tracker,
        "1111-local-1111-1111",
        "1700000000000000000-1111-local-1111-1111-CREATE.json",
        '{"e":"local"}',
    )
    return tracker, local_sha, origin_sha


@pytest.fixture
def diverged_with_tracked_deletion(tmp_path: Path) -> tuple[Path, str, str]:
    """Variant (b) deletion fixture: the tracker DELETES the tracked file origin
    modifies, STAGED (`D ` in porcelain) — exactly what an interrupted compaction
    fold leaves after renaming sources and running `git add -A`."""
    tracker, local_sha, origin_sha = _diverged_with_tracked_base(tmp_path)
    (tracker / _TRACKED_PATH).unlink()
    _git(tracker, "add", "-A", "--", _TRACKED_PATH)
    assert _git(tracker, "status", "--porcelain", "--", _TRACKED_PATH).stdout.startswith("D "), (
        "precondition: the tracked path must be a staged deletion ('D ')"
    )
    return tracker, local_sha, origin_sha


def test_reconverge_self_heals_tracked_local_deletion(diverged_with_tracked_deletion) -> None:
    """The variant-(b) wedge: a union merge aborted ONLY by a (staged) deletion of a
    tracked file origin modifies must complete (both parents kept) after restoring
    the file from HEAD — not abort-and-strand the local commit off origin."""
    tracker, local_sha, origin_sha = diverged_with_tracked_deletion

    # Paths git does NOT name must be untouched by the recovery.
    tmp_event = tracker / ".tmp-event-12345"
    tmp_event.write_text("in-flight")

    sync.reconverge(tracker)

    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0, (
        "origin commit not merged — reconverge wedged on the tracked-deletion abort"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0, (
        "local commit orphaned by the recovery"
    )
    parents = _git(tracker, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3, f"expected a 2-parent merge commit, got parents={parents}"
    # The deleted file is back at its merged content (origin's version — the local
    # side never committed a change to it).
    assert (tracker / _TRACKED_PATH).read_text() == _ORIGIN_BODY, (
        "the worktree-deleted tracked file must be restored at the merged content"
    )
    # Unnamed paths untouched: the in-flight event marker survives verbatim.
    assert tmp_event.read_text() == "in-flight", ".tmp-event-* must not be touched"
    # Data-safety: the local commit survives an aggressive prune.
    _git(tracker, "gc", "--prune=now", "--quiet")
    assert _git(tracker, "cat-file", "-e", local_sha).returncode == 0


def test_reconverge_quarantines_local_modification_then_merges(tmp_path: Path) -> None:
    """A worktree MODIFICATION of a tracked file origin also modifies: the local bytes
    must be preserved byte-for-byte in the quarantine dir BEFORE restore, and the
    merge must then complete with the merged (origin) content in the worktree."""
    tracker, local_sha, origin_sha = _diverged_with_tracked_base(tmp_path)
    local_bytes = '{"e":"local-uncommitted-edit"}'
    (tracker / _TRACKED_PATH).write_text(local_bytes)
    assert _git(tracker, "status", "--porcelain", "--", _TRACKED_PATH).stdout.startswith(" M")

    sync.reconverge(tracker)

    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0, (
        "origin commit not merged — reconverge wedged on the tracked-modification abort"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    assert (tracker / _TRACKED_PATH).read_text() == _ORIGIN_BODY
    # The local bytes are preserved in the quarantine dir (never deleted).
    common_dir = Path(_git(tracker, "rev-parse", "--git-common-dir").stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (tracker / common_dir).resolve()
    quarantine = common_dir / "reconverge-quarantine"
    preserved = [
        p
        for p in quarantine.rglob("*")
        if p.is_file() and p.read_text(errors="ignore") == local_bytes
    ]
    assert preserved, (
        "the local modification's bytes must be copied into the quarantine dir "
        "before the restore overwrites them"
    )


def test_reconverge_local_changes_recovery_survives_conflict_on_retry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Recovery restores the named deletion, but the retried merge hits a GENUINE
    content conflict on another file: the retry must run exactly once (two merge
    attempts total — never zero, never a third), then abort, keep local, and hint
    fsck-recover."""
    tracker, local_sha, origin_sha = _diverged_with_tracked_base(tmp_path)
    # Both sides commit conflicting edits to a shared tracked file.
    origin = tmp_path / "origin"
    (origin / _TRACKED_DIR / "shared.json").write_text('{"v":"base"}')
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "--no-verify", "-m", "shared base")
    # Tracker needs the shared file too — recreate the same base commit content there
    # via a direct write + commit so both sides then diverge on it.
    (tracker / _TRACKED_DIR / "shared.json").write_text('{"v":"base"}')
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "shared base (local)")
    (origin / _TRACKED_DIR / "shared.json").write_text('{"v":"origin"}')
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "--no-verify", "-m", "origin edit")
    origin_sha = _git(origin, "rev-parse", "HEAD").stdout.strip()
    (tracker / _TRACKED_DIR / "shared.json").write_text('{"v":"local"}')
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "local edit")
    local_sha = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    # And the variant-(b) trigger: staged deletion of the tracked file.
    (tracker / _TRACKED_PATH).unlink()
    _git(tracker, "add", "-A", "--", _TRACKED_PATH)

    merge_calls: list[tuple[str, ...]] = []
    real_git = sync._git

    def counting_git(t: str, *args: str):
        if args and args[0] == "merge" and "--abort" not in args:
            merge_calls.append(args)
        return real_git(t, *args)

    import logging as _logging
    from unittest import mock

    with (
        mock.patch.object(sync, "_git", counting_git),
        caplog.at_level(_logging.WARNING, logger="rebar._store.sync"),
    ):
        sync.reconverge(tracker)

    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode != 0, (
        "a genuine content conflict must still abort, not be absorbed"
    )
    assert not (tracker / ".git" / "MERGE_HEAD").exists()
    assert len(merge_calls) == 2, (
        f"expected exactly one retry (2 merge attempts), got {len(merge_calls)}"
    )
    assert any("fsck-recover" in r.message for r in caplog.records), (
        "the abort net must hint rebar fsck-recover"
    )


def test_reconverge_unrecognized_local_state_keeps_abort_only(tmp_path: Path) -> None:
    """The status-code fence: a STAGED (index) change is not one of the two proven
    recovery states (' D' worktree deletion / ' M' worktree modification), so the
    recovery must decline and keep today's abort-only behavior — merge attempted
    exactly once, local kept, origin not absorbed, staged bytes untouched."""
    tracker, local_sha, origin_sha = _diverged_with_tracked_base(tmp_path)
    staged_bytes = '{"e":"staged-local-edit"}'
    (tracker / _TRACKED_PATH).write_text(staged_bytes)
    _git(tracker, "add", "--", _TRACKED_PATH)
    assert _git(tracker, "status", "--porcelain", "--", _TRACKED_PATH).stdout.startswith("M "), (
        "precondition: the tracked path must be a STAGED modification ('M ')"
    )

    merge_calls: list[tuple[str, ...]] = []
    real_git = sync._git

    def counting_git(t: str, *args: str):
        if args and args[0] == "merge" and "--abort" not in args:
            merge_calls.append(args)
        return real_git(t, *args)

    from unittest import mock

    with mock.patch.object(sync, "_git", counting_git):
        sync.reconverge(tracker)

    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode != 0
    assert (tracker / _TRACKED_PATH).read_text() == staged_bytes, (
        "an unrecognized local state must be left untouched"
    )
    assert len(merge_calls) == 1, "no retry without a completed recovery"
    assert not (tracker / ".git" / "MERGE_HEAD").exists()


def test_restore_local_changes_handles_pure_worktree_deletion(tmp_path: Path) -> None:
    """The ` D` branch directly: modern git (2.55/ort) does not even abort on a pure
    worktree deletion, but older gits name it, so the restore helper must handle it —
    checkout from HEAD, True returned, nothing quarantined (nothing to preserve)."""
    repo = tmp_path / "repo"
    _new_tickets_repo(repo)
    _commit_event(repo, _TRACKED_DIR, _TRACKED_FILE, '{"e":"base"}')
    (repo / _TRACKED_PATH).unlink()
    assert _git(repo, "status", "--porcelain", "--", _TRACKED_PATH).stdout.startswith(" D")

    assert sync._restore_local_changes(str(repo), [_TRACKED_PATH]) is True
    assert (repo / _TRACKED_PATH).read_text() == '{"e":"base"}'
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_recover_merge_abort_clears_merge_state_before_touching_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering contract: a failed merge can leave merge state behind (older gits
    detect the collision mid-merge), so recovery must run `git merge --abort` BEFORE
    relocating or restoring any file — the retry has to start from a clean tree."""
    calls: list[tuple[str, ...]] = []

    def fake_git(tracker: str, *args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    def fake_quarantine(tracker: str, paths: list[str]) -> bool:
        calls.append(("quarantine",))
        return True

    monkeypatch.setattr(sync, "_git", fake_git)
    monkeypatch.setattr(sync, "_quarantine_untracked", fake_quarantine)
    merge = subprocess.CompletedProcess(
        ["git", "merge"],
        2,
        "",
        "error: The following untracked working tree files would be overwritten by merge:\n"
        "\tsome/file.json\n"
        "Please move or remove them before you merge.\n"
        "Aborting\n",
    )

    assert sync._recover_merge_abort(str(tmp_path), merge) is True
    assert ("merge", "--abort") in calls, "recovery must abort the failed merge"
    assert calls.index(("merge", "--abort")) < calls.index(("quarantine",)), (
        "merge --abort must run before any file is relocated"
    )
