"""Push-recovery parity with reconverge's untracked-overwrite self-heal (variant (a),
loris/1757; generalized by wolverine/1767): ``_recover_dirty_merge`` sets tracked
changes aside with a stash COMMIT OBJECT and ``reset --hard``, but untracked files are
deliberately left alone — so an untracked compaction leftover (`*-SNAPSHOT.json` /
`*.retired`) colliding with a file the remote merge wants to create aborts the merge
("untracked working tree files would be overwritten by merge"), and the pre-fix code
gave up (abort, restore stash, warn). Every push retry then re-failed identically.

The fix mirrors reconverge: parse exactly the paths git names (the shared pure parser
in ``rebar._store.sync``), move — never delete — the named UNTRACKED paths into the
shared ``<git-common-dir>/reconverge-quarantine/<utc-ts>/`` dir, retry the merge ONCE.
The MOVE is implemented locally through push_recovery's late-bound ``core._git`` seam
(sync's mover shells through sync's own module-level ``_git``, which would bypass the
~25 ``push._git`` monkeypatch sites). Any other failure keeps today's behavior exactly,
and the stash commit is restored exactly once on every exit path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import lock as lock_module
from rebar._store import push, push_recovery


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


_COLLIDE_DIR = "bbbb-orig-2222-2222"
_COLLIDE_FILE = "1700000000000000000-bbbb-orig-2222-2222-SNAPSHOT.json"
_COLLIDE_PATH = f"{_COLLIDE_DIR}/{_COLLIDE_FILE}"
_LOCAL_LEFTOVER_BODY = '{"side":"local-untracked-leftover"}'
_ORIGIN_BODY = '{"side":"origin-committed"}'
_TRACKED_LOCAL = "1111-locl-1111-1111/1700000000000000000-1111-locl-1111-1111-CREATE.json"
_DIRTY_BODY = '{"e":"local","dirty":"uncommitted-edit"}'


def _diverged_with_untracked_collision(tmp_path: Path) -> tuple[Path, str, str]:
    """Origin and tracker share a base, then BOTH advance. Origin's new commit adds
    ``_COLLIDE_PATH``; the tracker holds that SAME path as an UNTRACKED leftover and a
    DIRTY uncommitted edit to its own tracked event file (the stash payload). The
    remote-tracking ref is fetched, as ``_recover_non_fast_forward`` would have done.
    Returns (tracker, local_sha, origin_sha)."""
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

    origin_sha = _commit_event(origin, _COLLIDE_DIR, _COLLIDE_FILE, _ORIGIN_BODY)
    local_sha = _commit_event(
        tracker, _TRACKED_LOCAL.split("/")[0], _TRACKED_LOCAL.split("/")[1], '{"e":"local"}'
    )
    # The dirty tracked edit the stash must carry across the recovery.
    (tracker / _TRACKED_LOCAL).write_text(_DIRTY_BODY)
    # The untracked compaction leftover colliding with origin's new file.
    leftover = tracker / _COLLIDE_PATH
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text(_LOCAL_LEFTOVER_BODY)
    assert _git(tracker, "status", "--porcelain", "--", _COLLIDE_PATH).stdout.startswith("??")

    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")
    return tracker, local_sha, origin_sha


def _quarantine_root(tracker: Path) -> Path:
    common_dir = Path(_git(tracker, "rev-parse", "--git-common-dir").stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (tracker / common_dir).resolve()
    return common_dir / "reconverge-quarantine"


def _counting_git(calls: list[tuple[str, ...]]):
    real_git = push._git

    def wrapper(base: str, *args: str, **kwargs):
        calls.append(args)
        return real_git(base, *args, **kwargs)

    return wrapper


def _merges(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [c for c in calls if c and c[0] == "merge" and "--abort" not in c]


def _stash_applies(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [c for c in calls if len(c) >= 2 and c[0] == "stash" and c[1] == "apply"]


def test_recover_dirty_merge_self_heals_untracked_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wedge: a post-stash merge blocked ONLY by an origin-introduced untracked
    collision must quarantine the named path, retry ONCE (exactly two merge calls),
    complete with both parents, and restore the stashed dirty edit."""
    tracker, local_sha, origin_sha = _diverged_with_untracked_collision(tmp_path)
    # A path git does NOT name must be untouched by the recovery.
    tmp_event = tracker / ".tmp-event-12345"
    tmp_event.write_text("in-flight")

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(push, "_git", _counting_git(calls))

    result = push_recovery._recover_dirty_merge(push, str(tracker), "origin/tickets", 1, False)

    assert result is True, "recovery must succeed once the collision is quarantined"
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0, (
        "origin commit not merged — push recovery wedged on the untracked collision"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    parents = _git(tracker, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3, f"expected a 2-parent merge commit, got parents={parents}"
    # Origin's committed bytes landed at the colliding path.
    assert (tracker / _COLLIDE_PATH).read_text() == _ORIGIN_BODY
    # The leftover's bytes were MOVED (never deleted) into the shared quarantine dir.
    preserved = [
        p
        for p in _quarantine_root(tracker).rglob("*")
        if p.is_file() and p.read_text(errors="ignore") == _LOCAL_LEFTOVER_BODY
    ]
    assert preserved, "the untracked leftover's bytes must survive in reconverge-quarantine"
    # The stashed dirty edit is restored, exactly once.
    assert (tracker / _TRACKED_LOCAL).read_text() == _DIRTY_BODY, "stashed edit lost"
    assert len(_stash_applies(calls)) == 1, "stash must be restored exactly once"
    # Exactly ONE retry — never zero, never a second.
    assert len(_merges(calls)) == 2, f"expected 2 merge attempts, got {_merges(calls)}"
    # Unnamed paths untouched.
    assert tmp_event.read_text() == "in-flight", ".tmp-event-* must not be touched"


def test_recover_dirty_merge_retry_failure_keeps_abort_net_and_restores_stash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery quarantines the named collision, but the retried merge hits a GENUINE
    content conflict on another file: exactly one retry, then today's net — abort,
    stash restored exactly once, False returned, origin not absorbed."""
    tracker, local_sha, origin_sha = _diverged_with_untracked_collision(tmp_path)
    origin = tmp_path / "origin"
    # Both sides ADD conflicting versions of the same tracked file (targeted adds so
    # the untracked collision and the dirty edit stay out of these commits).
    shared = "0000-base-0000-0000/shared.json"
    (origin / shared).write_text('{"v":"origin"}')
    _git(origin, "add", "--", shared)
    _git(origin, "commit", "-q", "--no-verify", "-m", "origin edit")
    origin_sha = _git(origin, "rev-parse", "HEAD").stdout.strip()
    (tracker / shared).write_text('{"v":"local"}')
    _git(tracker, "add", "--", shared)
    _git(tracker, "commit", "-q", "--no-verify", "-m", "local edit")
    local_sha = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")
    assert _git(tracker, "status", "--porcelain", "--", _COLLIDE_PATH).stdout.startswith("??")
    assert (tracker / _TRACKED_LOCAL).read_text() == _DIRTY_BODY

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(push, "_git", _counting_git(calls))

    result = push_recovery._recover_dirty_merge(push, str(tracker), "origin/tickets", 1, False)

    assert result is False
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode != 0
    assert not (tracker / ".git" / "MERGE_HEAD").exists()
    assert (tracker / _TRACKED_LOCAL).read_text() == _DIRTY_BODY, (
        "the stash must be restored on the retry-failure path"
    )
    assert len(_stash_applies(calls)) == 1, "stash must be restored exactly once"
    assert len(_merges(calls)) == 2, f"expected 2 merge attempts, got {_merges(calls)}"


def test_recover_dirty_merge_genuine_conflict_keeps_todays_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: a merge failure that names NO untracked collision (a genuine content
    conflict) must keep today's behavior exactly — merge attempted ONCE (no retry),
    abort, stash restored once, False — and no quarantine dir is created."""
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    (origin / "shared.json").write_text('{"v":"base"}')
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "--no-verify", "-m", "base")
    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")
    (origin / "shared.json").write_text('{"v":"origin"}')
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "--no-verify", "-m", "origin edit")
    origin_sha = _git(origin, "rev-parse", "HEAD").stdout.strip()
    (tracker / "shared.json").write_text('{"v":"local"}')
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "local edit")
    local_sha = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    dirty = tracker / "1111-locl-1111-1111" / "note.json"
    dirty.parent.mkdir(parents=True, exist_ok=True)
    dirty.write_text('{"e":"dirty-tracked"}')
    _git(tracker, "add", "--", "1111-locl-1111-1111/note.json")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "note")
    local_sha = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    dirty.write_text('{"e":"dirty-tracked","edit":1}')
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(push, "_git", _counting_git(calls))

    result = push_recovery._recover_dirty_merge(push, str(tracker), "origin/tickets", 1, False)

    assert result is False
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode != 0
    assert not (tracker / ".git" / "MERGE_HEAD").exists()
    assert dirty.read_text() == '{"e":"dirty-tracked","edit":1}', "stash not restored"
    assert len(_stash_applies(calls)) == 1
    assert len(_merges(calls)) == 1, "a genuine conflict must never trigger a retry"
    assert not _quarantine_root(tracker).exists(), (
        "the quarantine dir must only be created when something is moved"
    )


def test_merge_remote_under_lock_routes_untracked_collision_to_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sibling seam: when the abort surfaces in ``_merge_remote_under_lock``'s own
    merge (tracked tree clean, untracked collision present), ``_DIRTY_WD`` routes it
    into ``_recover_dirty_merge``, whose quarantine+retry must then complete the
    merge end-to-end."""
    tracker, local_sha, origin_sha = _diverged_with_untracked_collision(tmp_path)
    # Clean tracked tree: only the untracked collision remains.
    _git(tracker, "checkout", "-q", "--", ".")

    result = push_recovery._merge_remote_under_lock(
        push, str(tracker), "origin/tickets", 1, False, lock_module
    )

    assert result is True
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0, (
        "the locked-merge seam must self-heal the untracked collision too"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    assert (tracker / _COLLIDE_PATH).read_text() == _ORIGIN_BODY
    preserved = [
        p
        for p in _quarantine_root(tracker).rglob("*")
        if p.is_file() and p.read_text(errors="ignore") == _LOCAL_LEFTOVER_BODY
    ]
    assert preserved, "the leftover's bytes must be preserved in quarantine"


def test_quarantine_declines_when_a_named_path_is_not_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The untracked fence: the mover must refuse (and leave every byte in place) if a
    path git named is not actually untracked in the working tree — a mis-parse must
    never relocate tracked data."""
    tracker, _local_sha, _origin_sha = _diverged_with_untracked_collision(tmp_path)

    moved = push_recovery._quarantine_untracked_paths(
        push, str(tracker), [_COLLIDE_PATH, _TRACKED_LOCAL]
    )

    assert moved is False
    assert (tracker / _TRACKED_LOCAL).exists(), "a tracked path must never be moved"


def test_quarantine_aborts_merge_before_moving_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering contract: an older git can leave merge state behind on this abort, so
    the recovery must run `git merge --abort` BEFORE any file is relocated — the
    retry has to start from a clean tree."""
    calls: list[tuple[str, ...]] = []

    def fake_git(base: str, *args: str, **kwargs) -> subprocess.CompletedProcess:
        calls.append(args)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    def fake_move(core, base: str, paths: list[str]) -> bool:
        calls.append(("quarantine-move",))
        return True

    monkeypatch.setattr(push, "_git", fake_git)
    monkeypatch.setattr(push_recovery, "_quarantine_untracked_paths", fake_move)
    merge = subprocess.CompletedProcess(
        ["git", "merge"],
        2,
        "",
        "error: The following untracked working tree files would be overwritten by merge:\n"
        f"\t{_COLLIDE_PATH}\n"
        "Please move or remove them before you merge.\n"
        "Aborting\n",
    )

    retry = push_recovery._retry_untracked_overwrite(
        push, str(tmp_path), "target-sha", "origin/tickets", merge
    )

    assert retry.returncode == 0
    assert ("merge", "--abort") in calls
    assert calls.index(("merge", "--abort")) < calls.index(("quarantine-move",)), (
        "merge --abort must run before any file is relocated"
    )


class _FakeCore:
    """Minimal ``core`` stand-in: a scripted ``_git`` for the mover's degenerate
    branches that real fixtures cannot reach deterministically."""

    def __init__(self, common: str, status: str) -> None:
        self._common = common
        self._status = status
        import logging

        self.logger = logging.getLogger("rebar._store.push")

    def _git(self, base: str, *args: str, **kwargs) -> subprocess.CompletedProcess:
        if args[:2] == ("rev-parse", "--git-common-dir"):
            return subprocess.CompletedProcess(["git", *args], 0, self._common, "")
        if args[0] == "status":
            return subprocess.CompletedProcess(["git", *args], 0, self._status, "")
        return subprocess.CompletedProcess(["git", *args], 0, "", "")


def test_quarantine_refuses_when_common_dir_is_unresolvable(tmp_path: Path) -> None:
    """An empty ``--git-common-dir`` answer must refuse the recovery outright — a
    quarantine path computed from '' would land INSIDE the working tree."""
    (tmp_path / "leftover.json").write_text("x")
    core = _FakeCore(common="", status="?? leftover.json\n")

    assert push_recovery._quarantine_untracked_paths(core, str(tmp_path), ["leftover.json"]) is (
        False
    )
    assert (tmp_path / "leftover.json").read_text() == "x", "nothing may move on refusal"


def test_quarantine_refuses_a_named_path_that_vanished(tmp_path: Path) -> None:
    """The move-time existence fence: a path that vanished between the status check
    and the move (a concurrent writer) must answer False, never raise — the caller
    then keeps the abort net."""
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    core = _FakeCore(common=str(gitdir), status="?? vanished.json\n")

    result = push_recovery._quarantine_untracked_paths(core, str(tmp_path), ["vanished.json"])

    assert result is False
