"""Concurrent ref-updating fetches are coordinated by the Git COMMON directory.

Regression coverage for bug ``agrologic-oval-bobolink``: two uncoordinated ref-updating
fetches that share one Git common directory race git's ref compare-and-swap, and the loser
fails ``cannot lock ref 'refs/remotes/origin/tickets': is at <new> but expected <old>`` — the
snapshot fetch aborts an attested op, the sync fetch silently drops a freshness round.

The fix has two composed mechanisms, both exercised here at BOTH affected entry points
(``rebar._store.sync`` and ``rebar._snapshot.git_fetch``):

* a BLOCKING cross-process fetch lock keyed on the canonical (symlink/worktree-resolved) Git
  common directory, so rebar peers serialize instead of racing; and
* a bounded retry of the exact CAS mismatch, so a residual race with a NON-rebar git peer
  (which never takes the lock) recovers rather than surfacing or silently dropping.

The seam-injected tests are the deterministic "observe the failure before the fix" controls:
a single injected CAS is exactly what a parallel race produces, and WITHOUT the retry the
sync path would return un-converged and the snapshot path would raise on the first CAS. The
real-git tests pin the common-dir identity (follows worktrees + symlinks) and prove a
parallel run of real fetches sharing one common dir converges with zero CAS surfaced.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import threading
from pathlib import Path

import pytest

from rebar._snapshot import git_fetch
from rebar._store import git_outcome, sync
from rebar._store.gitutil import _resolve_common_git_dir, fetch_coordination_lock

# The exact production stderr recorded on the ticket.
_CAS_STDERR = (
    "error: cannot lock ref 'refs/remotes/origin/tickets': is at "
    "85e049ab2715b3fd5aeac586658ac859a44b7224 but expected "
    "65efa1967a9b0c54ee7c2182c1593954f3896ab2"
)


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
    tdir = repo / ticket_uuid
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"1700000000000000000-{ticket_uuid}-CREATE.json").write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", f"ticket: CREATE {ticket_uuid}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# ── The classifier: the CAS mismatch is its OWN outcome, distinct from a stuck lock ──


def test_ref_cas_mismatch_classifier_matches_production_stderr() -> None:
    assert git_outcome.is_ref_cas_mismatch(_CAS_STDERR)
    # A held <name>.lock create conflict is contention, NOT ref movement — different outcome.
    assert not git_outcome.is_ref_cas_mismatch(
        "fatal: Unable to create '/x/.git/index.lock': File exists. Another git process…"
    )
    # A plain ref-lock line without the CAS "is at … but expected …" is not a CAS mismatch.
    assert not git_outcome.is_ref_cas_mismatch(
        "error: cannot lock ref 'refs/heads/tickets': unable to resolve reference"
    )
    assert not git_outcome.is_ref_cas_mismatch("fatal: could not read from remote repository")


# ── AC3: coordination identity follows linked worktrees AND symlinks to the common dir ──


def test_common_dir_identity_follows_worktrees_and_symlinks(tmp_path: Path) -> None:
    main = tmp_path / "main"
    _new_tickets_repo(main)
    _commit_event(main, "0000-aaaa-bbbb-cccc", "{}")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "--detach", str(linked))
    symlink = tmp_path / "symlinked"
    symlink.symlink_to(main)

    common_main = _resolve_common_git_dir(str(main))
    common_linked = _resolve_common_git_dir(str(linked))
    common_symlink = _resolve_common_git_dir(str(symlink))

    assert common_main is not None
    assert common_main == common_linked, (
        "a linked worktree must resolve to the SAME common dir as its main checkout"
    )
    assert common_main == common_symlink, "a symlinked checkout must resolve canonically"
    assert os.path.isabs(common_main) and os.path.realpath(common_main) == common_main
    assert _resolve_common_git_dir(str(tmp_path / "not-a-repo")) is None


# ── The lock actually mutually excludes on a SHARED common dir (blocking, cross-FD) ──


def test_fetch_coordination_lock_serializes_on_shared_common_dir(tmp_path: Path) -> None:
    main = tmp_path / "main"
    _new_tickets_repo(main)
    _commit_event(main, "0000-aaaa-bbbb-cccc", "{}")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "--detach", str(linked))

    common = _resolve_common_git_dir(str(main))
    assert common is not None
    lock_file = os.path.join(common, "rebar-fetch.lock")

    # Hold the lock via the MAIN worktree; a fresh FD (a peer's view via the LINKED worktree,
    # same shared common dir) must NOT be able to take LOCK_EX non-blocking.
    with fetch_coordination_lock(str(main)):
        assert os.path.exists(lock_file), "the lock file lives in the shared common dir"
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    # Released: a peer can now acquire it.
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── SYNC entry point: bounded retry of the CAS mismatch converges (RED before the fix) ──


def _clone_tracker(origin: Path, tracker: Path) -> None:
    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")


def test_reconverge_retries_ref_cas_mismatch_then_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    _commit_event(origin, "0000-base-0000-0000", '{"e":"base"}')
    _clone_tracker(origin, tracker)
    # A new origin-side ticket the tracker must adopt once the fetch actually lands.
    origin_sha = _commit_event(origin, "2222-orig-2222-2222", '{"e":"origin"}')

    real_git = sync._git
    calls = {"fetch": 0}

    def fake_git(tr: str, *args: str) -> subprocess.CompletedProcess:
        if args and args[0] == "fetch":
            calls["fetch"] += 1
            if calls["fetch"] == 1:
                # Exactly what a racing peer fetch leaves behind on the first attempt.
                return subprocess.CompletedProcess(["git", *args], 128, "", _CAS_STDERR)
        return real_git(tr, *args)

    monkeypatch.setattr(sync, "_git", fake_git)

    sync.reconverge(tracker)

    assert calls["fetch"] >= 2, "the CAS mismatch must be retried, not surfaced/dropped"
    assert (tracker / "2222-orig-2222-2222").is_dir(), (
        "reconverge did not converge after the retried fetch — the freshness round was "
        "silently dropped (the pre-fix behaviour)"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0


def test_reconverge_cas_exhausted_is_best_effort_no_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    _commit_event(origin, "0000-base-0000-0000", '{"e":"base"}')
    _clone_tracker(origin, tracker)
    _commit_event(origin, "2222-orig-2222-2222", '{"e":"origin"}')

    real_git = sync._git
    calls = {"fetch": 0}

    def always_cas(tr: str, *args: str) -> subprocess.CompletedProcess:
        if args and args[0] == "fetch":
            calls["fetch"] += 1
            return subprocess.CompletedProcess(["git", *args], 128, "", _CAS_STDERR)
        return real_git(tr, *args)

    monkeypatch.setattr(sync, "_git", always_cas)
    monkeypatch.setattr(sync, "_FETCH_CAS_BACKOFF_S", 0.0)

    # Best-effort: an exhausted CAS retry NEVER raises — it just skips this freshness round.
    sync.reconverge(tracker)

    assert calls["fetch"] == sync._FETCH_CAS_ATTEMPTS, "the retry must be BOUNDED"
    assert not (tracker / "2222-orig-2222-2222").exists(), (
        "a fetch that never landed must not have converged"
    )


# ── SNAPSHOT entry point: bounded retry of the CAS mismatch (RED before the fix) ──


def _snapshot_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "code"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", "c")
    return repo


def test_fetch_origin_retries_ref_cas_mismatch_then_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _snapshot_repo(tmp_path)
    lock_path = tmp_path / "locks" / "fetch.lock"
    calls = {"n": 0}

    def fake_run(argv, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(argv, 128, "", _CAS_STDERR)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(git_fetch.subprocess, "run", fake_run)
    monkeypatch.setattr(git_fetch, "_CAS_RETRY_BACKOFF_S", 0.0)

    # Must NOT raise — the racing CAS is retried and the second attempt lands.
    git_fetch.fetch_origin(str(repo), lock_path=lock_path)
    assert calls["n"] == 2


def test_fetch_origin_cas_exhausted_raises_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _snapshot_repo(tmp_path)
    lock_path = tmp_path / "locks" / "fetch.lock"
    calls = {"n": 0}

    def always_cas(argv, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(argv, 128, "", _CAS_STDERR)

    monkeypatch.setattr(git_fetch.subprocess, "run", always_cas)
    monkeypatch.setattr(git_fetch, "_CAS_RETRY_BACKOFF_S", 0.0)

    with pytest.raises(git_fetch.SnapshotFetchError) as exc:
        git_fetch.fetch_origin(str(repo), lock_path=lock_path)

    assert "compare-and-swap" in str(exc.value)
    assert exc.value.stderr == _CAS_STDERR
    assert calls["n"] >= 2, "the CAS must be retried before the exhausted-recovery error"


# ── Parallel/serialized control (real git): coordinated fetches share one common dir ──


def _make_shared_worktrees(tmp_path: Path, n: int) -> tuple[Path, list[Path]]:
    """One origin + a base clone with ``n`` linked worktrees that all share ONE common dir
    (so every fetch updates the SAME ``refs/remotes/origin/tickets``)."""
    origin = tmp_path / "origin"
    _new_tickets_repo(origin)
    _commit_event(origin, "0000-base-0000-0000", '{"e":"base"}')
    base = tmp_path / "base"
    _clone_tracker(origin, base)
    worktrees: list[Path] = []
    for i in range(n):
        wt = tmp_path / f"wt{i}"
        _git(base, "worktree", "add", "-q", "--detach", str(wt))
        worktrees.append(wt)
    # All worktrees resolve to one common dir (the coordination identity).
    commons = {_resolve_common_git_dir(str(wt)) for wt in worktrees}
    assert len(commons) == 1
    return origin, worktrees


def test_parallel_coordinated_fetches_share_common_dir_no_cas(tmp_path: Path) -> None:
    """The serialized/coordinated control: many real fetches against one shared common dir,
    driven concurrently while origin advances, complete with ZERO CAS surfaced. This is
    deterministic — the common-dir lock serializes them regardless of thread timing."""
    n = 6
    origin, worktrees = _make_shared_worktrees(tmp_path, n)
    refspec = "+refs/heads/tickets:refs/remotes/origin/tickets"
    # Advance origin so there is real ref movement for fetches to contend over.
    for i in range(n):
        _commit_event(origin, f"1111-{i:04d}-1111-1111", f'{{"e":{i}}}')

    barrier = threading.Barrier(n)
    failures: list[str] = []

    def worker(wt: Path) -> None:
        barrier.wait()
        for _ in range(4):
            if not sync._coordinated_fetch(str(wt), "origin", refspec):
                failures.append(str(wt))

    threads = [threading.Thread(target=worker, args=(wt,)) for wt in worktrees]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not failures, f"coordinated fetches must not surface a CAS mismatch: {failures}"
    # Every worktree ended converged on origin's tip (they share the common-dir ref).
    tip = _git(origin, "rev-parse", "tickets").stdout.strip()
    assert _git(worktrees[0], "rev-parse", "refs/remotes/origin/tickets").stdout.strip() == tip


def test_uncoordinated_parallel_fetches_can_race_but_coordination_prevents_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parallel control: with coordination DISABLED, concurrent real fetches sharing one
    common dir race git's ref CAS. We drive rounds until the race is observed (its base rate
    is high, so this converges in a handful of rounds) — then confirm coordination removes it.
    """
    n = 6
    origin, worktrees = _make_shared_worktrees(tmp_path, n)
    refspec = "+refs/heads/tickets:refs/remotes/origin/tickets"

    def raw_fetch(wt: Path) -> subprocess.CompletedProcess:
        return _git(wt, "fetch", "origin", refspec, "--quiet")

    def run_round(coordinated: bool) -> int:
        """One concurrent round; returns the count of CAS mismatches observed."""
        for i in range(n):
            _commit_event(origin, f"race-{i:04d}-{threading.get_ident() % 9999:04d}", "{}")
        barrier = threading.Barrier(n)
        seen: list[int] = []

        def worker(wt: Path) -> None:
            barrier.wait()
            if coordinated:
                ctx: contextlib.AbstractContextManager = fetch_coordination_lock(str(wt))
            else:
                ctx = contextlib.nullcontext()
            with ctx:
                proc = raw_fetch(wt)
            if proc.returncode != 0 and git_outcome.is_ref_cas_mismatch(
                (proc.stderr or "") + (proc.stdout or "")
            ):
                seen.append(1)

        threads = [threading.Thread(target=worker, args=(wt,)) for wt in worktrees]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return len(seen)

    # Observe the race WITHOUT coordination. High base rate → a small cap is more than
    # enough; hitting the cap with zero races would itself be astronomically unlikely.
    observed = 0
    for _ in range(40):
        observed += run_round(coordinated=False)
        if observed:
            break
    assert observed, "expected the uncoordinated parallel fetches to race git's ref CAS"

    # WITH coordination the same shared-common-dir contention produces ZERO races,
    # deterministically, across many rounds.
    coordinated_races = sum(run_round(coordinated=True) for _ in range(8))
    assert coordinated_races == 0, "the common-dir fetch lock must eliminate the CAS race"
