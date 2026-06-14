"""WS3 concurrency-regression harness — the executable form of the Concurrency
Doctrine (§0, invariants I1-I9).

Two independent clones of one tracker write disjoint events (create + comment on
different tickets) and overlapping events (concurrent transitions of the SAME
ticket to DIFFERENT targets), reconverge through the real engine sync/push paths
(merge-as-union, never rebase), and must end at ONE deterministic state on both
clones:

  (a) union          — every append-only, UUID-named event file from both clones
                       is present on both clones after reconvergence (I1/I2/I6).
  (b) deterministic  — replay yields identical ticket state on both clones (I8).
  (c) fork tie-break — the concurrent-transition fork resolves to the SAME winner
                       on both clones, skew-independently by UUID (I8).
  (d) no data loss   — a failed push never drops a local-only commit (WS3).

This is the characterization gate every later write/sync change (WS2, WS5c) runs
against. It exercises the actual engine paths, not a simulation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from rebar import _engine

pytestmark = pytest.mark.integration


# ─────────────────────────── helpers ────────────────────────────────────────
def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True
    )


def _engine_run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a rebar engine subcommand against *repo* via the real dispatcher."""
    return _engine.run(list(args), repo_root=str(repo), cwd=str(repo), check=check)


def _make_repo(remote: Path, path: Path) -> Path:
    """Clone *remote* into *path*, configure identity, return the repo path."""
    _git("clone", "-q", str(remote), str(path), cwd=path.parent)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    return path


def _tracker(repo: Path) -> Path:
    return Path(os.path.realpath(repo / ".tickets-tracker"))


def _expire_sync_marker(tracker: Path) -> None:
    """Delete the once-a-minute sync marker so the next read actually syncs."""
    h = hashlib.md5(str(tracker).encode()).hexdigest()[:12]
    for cand in (f"/tmp/.ticket-sync-{h}", "/tmp/.ticket-sync-fallback"):
        try:
            os.unlink(cand)
        except FileNotFoundError:
            pass


def _remote_remove(tracker: Path) -> None:
    _git("remote", "remove", "origin", cwd=tracker, check=False)


def _remote_add(tracker: Path, remote: Path) -> None:
    _git("remote", "add", "origin", str(remote), cwd=tracker, check=False)
    _git("fetch", "-q", "origin", "tickets", cwd=tracker, check=False)


def _event_files(tracker: Path) -> set[str]:
    """All append-only event filenames committed on the tickets branch."""
    out = _git("ls-tree", "-r", "--name-only", "tickets", cwd=tracker).stdout
    return {
        line.split("/")[-1]
        for line in out.splitlines()
        if line.endswith(".json") and "-" in line.split("/")[-1]
    }


def _list_status(repo: Path) -> dict[str, str]:
    """Map ticket_id -> status from `rebar list` (replayed state)."""
    out = _engine_run(repo, "list").stdout
    tickets = json.loads(out)
    return {t["ticket_id"]: t["status"] for t in tickets}


def _create(repo: Path, ttype: str, title: str) -> str:
    return _engine_run(repo, "create", ttype, title).stdout.strip().splitlines()[-1]


# ─────────────────────────── fixtures ───────────────────────────────────────
@pytest.fixture
def two_clones(tmp_path: Path):
    """A bare remote + two initialized clones (A, B) sharing one tickets branch.

    A creates a seed ticket and pushes it; B mounts the same tickets branch.
    Returns (remote, repo_a, repo_b, seed_ticket_id).
    """
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)

    repo_a = _make_repo(remote, tmp_path / "a")
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo_a)
    _git("push", "-q", "-u", "origin", "HEAD:main", cwd=repo_a)

    # Init tickets in A (no sync during setup), seed a ticket, push the branch.
    env_no_sync = {"_TICKET_TEST_NO_SYNC": "1"}
    _engine.run(["init"], repo_root=str(repo_a), cwd=str(repo_a))
    seed = _create(repo_a, "task", "seed shared ticket")
    tracker_a = _tracker(repo_a)
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)
    _git("fetch", "-q", "origin", "tickets", cwd=tracker_a)

    # B clones main + the tickets branch, then mounts it via init.
    repo_b = _make_repo(remote, tmp_path / "b")
    _git("fetch", "-q", "origin", "tickets", cwd=repo_b)
    _engine.run(["init"], repo_root=str(repo_b), cwd=str(repo_b))
    tracker_b = _tracker(repo_b)
    # Ensure B's tickets branch points at origin/tickets (shared base).
    _git("fetch", "-q", "origin", "tickets", cwd=tracker_b)

    return remote, repo_a, repo_b, seed


# ─────────────────────────── tests ──────────────────────────────────────────
def test_two_clone_union_deterministic_replay_and_fork_tiebreak(two_clones):
    remote, repo_a, repo_b, seed = two_clones
    tracker_a, tracker_b = _tracker(repo_a), _tracker(repo_b)

    # Sanity: both clones see the seed ticket as open on a shared base.
    assert _list_status(repo_a).get(seed) == "open"
    assert _list_status(repo_b).get(seed) == "open"

    # ── Phase 1: diverge offline (remove origin so writes don't push) ────────
    _remote_remove(tracker_a)
    _remote_remove(tracker_b)

    # Disjoint events.
    ta = _create(repo_a, "task", "A-only ticket")
    _engine_run(repo_a, "comment", seed, "comment from A")
    tb = _create(repo_b, "bug", "B-only ticket")
    _engine_run(repo_b, "comment", seed, "comment from B")

    # Overlapping events: concurrent transition of the SAME ticket to DIFFERENT
    # targets (both see it as 'open', so both are valid optimistic transitions).
    _engine_run(repo_a, "transition", seed, "open", "in_progress")
    _engine_run(repo_b, "transition", seed, "open", "blocked")

    # ── Phase 2: reconverge through the real engine paths ────────────────────
    # A is a pure fast-forward over the shared base → push succeeds.
    _remote_add(tracker_a, remote)
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)

    # B diverged → its next write triggers the real merge-as-union push retry.
    _remote_add(tracker_b, remote)
    _engine_run(repo_b, "comment", tb, "trigger reconverge push from B")

    # A converges by the read-side sync (marker expired) — fetch + reconverge.
    _remote_add(tracker_a, remote)
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")
    # Belt-and-suspenders: a second expired-marker read in case the first only
    # fetched. (Reconvergence must be reached; this must not loop forever.)
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")

    # ── Assertions ───────────────────────────────────────────────────────────
    events_a = _event_files(tracker_a)
    events_b = _event_files(tracker_b)

    # (a) union — both clones hold every event from both sides.
    assert events_a == events_b, (
        f"event sets diverged:\n  only in A: {sorted(events_a - events_b)}\n"
        f"  only in B: {sorted(events_b - events_a)}"
    )
    # The disjoint creates must both be present.
    assert any(f.endswith("-CREATE.json") for f in events_a)
    assert all(t in _list_status(repo_a) for t in (seed, ta, tb))
    assert all(t in _list_status(repo_b) for t in (seed, ta, tb))

    # (b)+(c) deterministic replay incl. the fork tie-break: identical state on
    # both clones, and the concurrent transition resolved to the SAME winner.
    status_a = _list_status(repo_a)
    status_b = _list_status(repo_b)
    assert status_a == status_b, f"non-deterministic replay: A={status_a} B={status_b}"
    assert status_a[seed] in ("in_progress", "blocked"), status_a[seed]


def test_failed_push_never_drops_local_commit(two_clones):
    """A push to an unreachable remote must not discard the local-only commit,
    and a subsequent sync must still preserve it (WS3 no-data-loss)."""
    remote, repo_a, repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)

    # Point origin at a non-existent remote so every push fails.
    _git("remote", "set-url", "origin", str(remote.parent / "does-not-exist.git"), cwd=tracker_a)

    before = _git("rev-parse", "HEAD", cwd=tracker_a).stdout.strip()
    local_ticket = _create(repo_a, "task", "local ticket whose push will fail")
    after = _git("rev-parse", "HEAD", cwd=tracker_a).stdout.strip()
    assert after != before, "create did not advance HEAD"

    # The local-only ticket must be present despite the failed push.
    assert local_ticket in _list_status(repo_a)

    # A sync against the (now broken) origin must not drop it either.
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")
    assert local_ticket in _list_status(repo_a), "local commit dropped after failed-push sync"
