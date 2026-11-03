"""Keep local commits reachable through sync recovery (97e7, 5546).

The old reset orphaned local-only commits. Shared-ancestry divergence now
union-merges. An unrelated event-empty local store can be adopted. Refuse
DIVERGED for unrelated local ticket events instead of publishing an orphan
store. Even on refusal, every local commit stays reachable from HEAD and
survives aggressive git gc. test_sync_narrow_refspec covers union paths."""

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


def _commit_event(repo: Path, ticket_uuid: str, body: str) -> str:
    """Write a UUID-named append-only event file and commit it; return its SHA."""
    tdir = repo / ticket_uuid
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"1700000000000000000-{ticket_uuid}-CREATE.json").write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", f"ticket: CREATE {ticket_uuid}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def unrelated_origin(tmp_path: Path) -> tuple[Path, str, str]:
    """A tracker with a local-only ticket whose `origin/tickets` is an UNRELATED
    history carrying a different ticket. Returns (tracker, local_sha, origin_sha)."""
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    _new_tickets_repo(tracker)

    origin_sha = _commit_event(origin, "0000-aaaa-bbbb-cccc", '{"side":"origin"}')
    local_sha = _commit_event(tracker, "1111-dddd-eeee-ffff", '{"side":"local"}')

    _git(tracker, "remote", "add", "origin", str(origin))
    # No common ancestor: the two repos were init'd independently.
    return tracker, local_sha, origin_sha


def test_unrelated_history_with_local_events_is_refused_not_unioned(unrelated_origin) -> None:
    """The local store has no common ancestor with ``origin/tickets`` AND carries its own
    ticket events — an orphan store. Bug 5546: refuse, never absorb it."""
    tracker, local_sha, origin_sha = unrelated_origin

    sync.reconverge(tracker)

    # WU-2's invariant, unchanged: the local commit is never discarded.
    assert (tracker / "1111-dddd-eeee-ffff").is_dir(), "local ticket discarded by reconverge"
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0, (
        "local commit was orphaned — reset --hard regression"
    )

    # Bug 5546: the unrelated store is NOT absorbed, so HEAD stays a single-parent commit.
    assert not (tracker / "0000-aaaa-bbbb-cccc").exists(), (
        "orphan store absorbed the shared tracker — the merge must be refused"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode != 0
    parents = _git(tracker, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 1, f"expected HEAD to be untouched by the refusal, got {parents}"


def test_unrelated_history_local_commit_survives_gc_prune_after_refusal(unrelated_origin) -> None:
    """The end-to-end WU-2 invariant, still asserted under the 5546 refusal: whatever
    reconverge does, an aggressive ``gc --prune=now`` collects nothing rebar cares about —
    the local commit stays REACHABLE from HEAD, never orphaned into the reflog."""
    tracker, local_sha, _origin_sha = unrelated_origin

    sync.reconverge(tracker)
    gc = _git(tracker, "gc", "--prune=now")
    assert gc.returncode == 0, gc.stderr

    assert _git(tracker, "cat-file", "-e", local_sha).returncode == 0, (
        f"{local_sha} lost after gc --prune=now — it was orphaned, not kept reachable"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    assert (tracker / "1111-dddd-eeee-ffff").is_dir()


# ── Detached-HEAD-local-ahead preservation (story archaic-elegant-bovine, WS3) ──
#
# The tracker worktree can be in a detached-HEAD-local-ahead state: a local commit advances
# HEAD but not refs/heads/tickets. `_do_reconverge` measures local-ahead by HEAD
# (`sync.py`: `rev-list {remote}..HEAD`), NOT the lagging branch ref — so the un-pushed local
# commit is never `reset --hard`'d away. This pins that data-safety guard.


def test_sync_preserves_detached_head_local_ahead_commit(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    _commit_event(origin, "0000-base-0000-0000", '{"e":"base"}')  # shared base

    # Tracker is CLONED from origin (shares history; gets origin remote + origin/tickets).
    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")

    # Origin advances beyond the shared base.
    origin_sha = _commit_event(origin, "2222-orig-2222-2222", '{"e":"origin"}')

    # Tracker goes DETACHED at the base, then commits locally: HEAD advances past base while
    # refs/heads/tickets still points at base (the detached-HEAD-local-ahead state).
    _git(tracker, "checkout", "--detach", "HEAD")
    local_sha = _commit_event(tracker, "1111-local-1111-1111", '{"e":"local"}')
    assert _git(tracker, "rev-parse", "tickets").stdout.strip() != local_sha, (
        "precondition: the branch ref must lag HEAD (detached-local-ahead)"
    )

    sync.reconverge(tracker)

    # The local commit must survive — reachable from HEAD, NOT orphaned by a reset --hard.
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0, (
        "detached-HEAD local commit was orphaned — the WS3 (branch-ref) data-loss regression"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0, (
        "origin commit not reachable after reconverge"
    )
    assert (tracker / "1111-local-1111-1111").is_dir(), "local event dir lost"
    assert (tracker / "2222-orig-2222-2222").is_dir(), "origin event dir not adopted"

    # And it stays reachable through an aggressive prune (not merely un-GC'd yet).
    _git(tracker, "gc", "--prune=now", "--quiet")
    assert _git(tracker, "cat-file", "-e", local_sha).returncode == 0, (
        "local commit was collectible — it was orphaned, not merged"
    )


# ── A timed-out merge is a timeout, not a conflict (bug painted-epidermal-shelduck) ──
#
# ``run_git_bounded`` folds a watchdog kill into a synthetic rc-124 result. Sync used to
# feed that result to the conflict-recovery path, find no conflict marker, and report
# "could not auto-merge … run: rebar store fsck-recover" — a tool that cannot help with a
# merge that is merely slower than its bound. These pin the distinct report, the single
# attempt, the unchanged conflict path, and the bounds sync applies.


def _diverged_clone(tmp_path: Path, *, conflict: bool = False) -> tuple[Path, str, str]:
    """A clone of ``origin`` where both sides committed after a shared base. With
    *conflict*, both sides write different content to the same tracked file."""
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    _commit_event(origin, "0000-base-0000-0000", '{"e":"base"}')
    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")
    if conflict:
        origin_sha = _commit_event(origin, "3333-same-3333-3333", '{"side":"origin"}')
        local_sha = _commit_event(tracker, "3333-same-3333-3333", '{"side":"local"}')
    else:
        origin_sha = _commit_event(origin, "2222-orig-2222-2222", '{"e":"origin"}')
        local_sha = _commit_event(tracker, "1111-local-1111-1111", '{"e":"local"}')
    return tracker, local_sha, origin_sha


def _stall_merges(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Make every non-abort ``git merge`` hit the watchdog, through the real
    ``run_git_bounded`` fold, so the result is exactly what production builds."""
    attempts: list[tuple[str, ...]] = []
    real_run_git = sync.run_git

    def stalling_run_git(cwd, *args, **kwargs):
        if args and args[0] == "merge" and "--abort" not in args:
            attempts.append(args)
            raise subprocess.TimeoutExpired(["git", *args], kwargs.get("timeout"))
        return real_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(sync, "run_git", stalling_run_git)
    return attempts


def test_timed_out_merge_is_reported_as_a_timeout_not_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tracker, local_sha, origin_sha = _diverged_clone(tmp_path)
    _stall_merges(monkeypatch)

    with caplog.at_level("WARNING", logger="rebar._store.sync"):
        sync.reconverge(tracker)

    text = caplog.text
    assert "could not auto-merge" not in text, text
    assert "fsck-recover" not in text, text
    assert "timed out" in text and "git merge" in text, text
    assert f"{sync._LOCAL_GIT_TIMEOUT}s" in text, f"the bound is not named: {text}"
    # Local state is still never discarded, and nothing is left mid-merge.
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode != 0
    assert (tracker / "1111-local-1111-1111").is_dir()
    assert not (tracker / ".git" / "MERGE_HEAD").exists()


def test_timed_out_merge_is_attempted_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running an operation that already outran its bound only burns more bounds."""
    tracker, _local_sha, _origin_sha = _diverged_clone(tmp_path)
    attempts = _stall_merges(monkeypatch)

    sync.reconverge(tracker)

    assert len(attempts) == 1, f"a timed-out merge was retried: {attempts}"


def test_genuine_conflict_keeps_the_could_not_auto_merge_report(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tracker, local_sha, origin_sha = _diverged_clone(tmp_path, conflict=True)

    with caplog.at_level("WARNING", logger="rebar._store.sync"):
        sync.reconverge(tracker)

    assert "could not auto-merge origin/tickets" in caplog.text
    assert "fsck-recover" in caplog.text
    assert "timed out" not in caplog.text
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode != 0
    assert not (tracker / ".git" / "MERGE_HEAD").exists()


def test_fetch_keeps_the_network_bound_and_local_ops_get_the_local_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck remote still fails fast; merge/reset are O(index) local work and are not
    judged by the network bound."""
    tracker, _local_sha, _origin_sha = _diverged_clone(tmp_path)
    bounds: dict[str, set[float | None]] = {}
    real_run_git = sync.run_git

    def recording_run_git(cwd, *args, **kwargs):
        bounds.setdefault(args[0], set()).add(kwargs.get("timeout"))
        return real_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(sync, "run_git", recording_run_git)
    sync.reconverge(tracker)

    assert sync._LOCAL_GIT_TIMEOUT > sync._GIT_TIMEOUT
    assert bounds["fetch"] == {sync._GIT_TIMEOUT}
    assert bounds["merge"] == {sync._LOCAL_GIT_TIMEOUT}


def test_ff_adopt_reset_gets_the_local_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    _commit_event(origin, "0000-base-0000-0000", '{"e":"base"}')
    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _commit_event(origin, "2222-orig-2222-2222", '{"e":"origin"}')
    seen: list[tuple[tuple[str, ...], float | None]] = []
    real_run_git = sync.run_git

    def recording_run_git(cwd, *args, **kwargs):
        seen.append((args, kwargs.get("timeout")))
        return real_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(sync, "run_git", recording_run_git)
    sync.reconverge(tracker)

    resets = [timeout for args, timeout in seen if args[0] == "reset"]
    assert resets == [sync._LOCAL_GIT_TIMEOUT]
    assert (tracker / "2222-orig-2222-2222").is_dir()


# ── The structural predicate (git_outcome.is_watchdog_timeout) ──


def test_watchdog_predicate_is_true_for_the_constructed_timeout() -> None:
    from rebar._store import git_outcome
    from rebar._store.gitutil import run_git_bounded

    def hung(*_a, **_kw):
        raise subprocess.TimeoutExpired(["git", "merge"], 30)

    result = run_git_bounded("/nonexistent", "merge", "x", timeout=30, runner=hung)
    assert git_outcome.is_watchdog_timeout(result)


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (1, "fatal: unable to access 'https://x/': Connection timed out after 30001 ms"),
        (128, "ssh: connect to host x port 22: Operation timed out"),
        (1, "error: Your local changes to the following files would be overwritten by merge"),
        (0, ""),
    ],
)
def test_watchdog_predicate_is_false_for_ordinary_results(returncode: int, stderr: str) -> None:
    """Text that merely mentions a timeout is not the watchdog's outcome."""
    from rebar._store import git_outcome

    result = subprocess.CompletedProcess(["git", "merge"], returncode, "", stderr)
    assert not git_outcome.is_watchdog_timeout(result)
