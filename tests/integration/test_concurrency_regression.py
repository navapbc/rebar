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
    return subprocess.run(["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True)


_CLI = _engine.in_process_cli()


def _engine_run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a rebar subcommand against *repo* via the in-process CLI.

    ``engine_env`` pins REBAR_ROOT/PROJECT_ROOT so the command's cwd-relative git
    operations resolve the right repository. This drives the Python
    write/sync/lock path under real cross-process concurrency — the contract this
    harness characterizes."""
    return subprocess.run(
        [_CLI, *args],
        cwd=str(repo),
        env=_engine.engine_env(repo_root=str(repo)),
        text=True,
        capture_output=True,
        check=check,
    )


def _make_repo(remote: Path, path: Path) -> Path:
    """Clone *remote* into *path*, configure identity, return the repo path."""
    _git("clone", "-q", str(remote), str(path), cwd=path.parent)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    return path


def _tracker(repo: Path) -> Path:
    return Path(os.path.realpath(repo / ".tickets-tracker"))


def _expire_sync_marker(tracker: Path) -> None:
    """Delete the once-a-minute sync marker so the next read actually syncs.

    The production throttle marker (``rebar._engine_support.reads.ensure_fresh``)
    is ``/tmp/.ticket-sync-<md5(realpath(tracker))[:12]>`` -- its name is uniquely
    derived from THIS tracker's realpath, which lives under the test's ``tmp_path``,
    so it is already namespaced per-test/per-worker and cannot perturb a sibling
    test or xdist worker. We delete ONLY that tracker-specific marker. We do NOT
    touch any fixed/global ``/tmp`` path (e.g. a non-namespaced
    ``/tmp/.ticket-sync-fallback``): no production code reads such a path, and
    deleting a global path would race other tests/workers (SDET I6).
    """
    h = hashlib.md5(str(tracker).encode()).hexdigest()[:12]
    try:
        os.unlink(f"/tmp/.ticket-sync-{h}")
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


def _status_events_for(tracker: Path, ticket_id: str) -> list[dict[str, str]]:
    """Return the STATUS events committed for *ticket_id*, each as
    ``{"uuid", "status", "current_status", "filename"}``.

    Event files are committed at ``<ticket_id>/<ts>-<uuid>-STATUS.json``; the
    canonical bytes carry the event's own ``uuid`` and ``data.status`` /
    ``data.current_status``. ``filename`` is the basename (``<ts>-<uuid>-STATUS``)
    -- the reducer (``rebar.reducer._api.reduce_ticket``) replays events in
    lexicographic filename order, so the basename is the authoritative replay-order
    key. Reads from git so it sees the merged union, not the working tree.
    """
    listing = _git("ls-tree", "-r", "--name-only", "tickets", cwd=tracker).stdout
    out: list[dict[str, str]] = []
    for path in listing.splitlines():
        if not path.endswith("-STATUS.json"):
            continue
        if path.split("/")[0] != ticket_id:
            continue
        blob = _git("show", f"tickets:{path}", cwd=tracker).stdout
        ev = json.loads(blob)
        data = ev.get("data", {})
        out.append(
            {
                "uuid": ev.get("uuid", ""),
                "status": data.get("status", ""),
                "current_status": data.get("current_status", ""),
                "filename": path.split("/")[-1],
            }
        )
    return out


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

    # Init tickets in A, seed a ticket, push the branch.
    _engine_run(repo_a, "init")
    seed = _create(repo_a, "task", "seed shared ticket")
    tracker_a = _tracker(repo_a)
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)
    _git("fetch", "-q", "origin", "tickets", cwd=tracker_a)

    # B clones main + the tickets branch, then mounts it via init.
    repo_b = _make_repo(remote, tmp_path / "b")
    _git("fetch", "-q", "origin", "tickets", cwd=repo_b)
    _engine_run(repo_b, "init")
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
    # The disjoint creates must BOTH survive the union -- not merely "some CREATE
    # file is present". A union that dropped exactly one side's CREATE would still
    # satisfy any(...), so assert each ticket's own CREATE event file is present.
    # CREATE events are committed under a per-ticket-id directory; read that dir
    # set from git rather than guessing the (UUID-embedding) filename shape.
    listing = _git("ls-tree", "-r", "--name-only", "tickets", cwd=tracker_a).stdout
    create_dirs = {
        line.split("/")[0] for line in listing.splitlines() if line.endswith("-CREATE.json")
    }
    assert ta in create_dirs, f"ta CREATE event lost in union (have {sorted(create_dirs)})"
    assert tb in create_dirs, f"tb CREATE event lost in union (have {sorted(create_dirs)})"
    assert all(t in _list_status(repo_a) for t in (seed, ta, tb))
    assert all(t in _list_status(repo_b) for t in (seed, ta, tb))

    # (b)+(c) deterministic replay incl. the fork tie-break: identical state on
    # both clones, and the concurrent transition resolved to the SAME winner.
    status_a = _list_status(repo_a)
    status_b = _list_status(repo_b)
    assert status_a == status_b, f"non-deterministic replay: A={status_a} B={status_b}"

    # -- Fork tie-break: assert the SPECIFIC winner, not mere set-membership --
    # Two concurrent STATUS events on the seed both forked from current_status
    # == "open" (A->in_progress, B->blocked). The OLD assertion only checked
    # ``status_a[seed] in ("in_progress","blocked")`` -- it could not tell WHICH
    # event won, so any resolution rule (UUID, wall-clock, insertion order) passed.
    #
    # The reducer (``process_status``) resolves the fork by LEXICAL EVENT UUID:
    # the lexically-LOWER of the two siblings' own UUIDs wins, deterministically
    # and INDEPENDENT of replay/insertion order (bug 8874 fixed: the non-fork
    # branch now advances ``parent_status_uuid`` to the event's own UUID, so a
    # sibling forks against the prior sibling's identity rather than an empty
    # parent pointer that would let the later-replayed event win by insertion
    # order). So the expected winner is the forked STATUS event with the smallest
    # UUID; asserting that exact target status makes the test FAIL if the rule
    # regresses to insertion-order / a different key.
    seed_status_events = _status_events_for(tracker_a, seed)
    forked = [e for e in seed_status_events if e["current_status"] == "open"]
    assert len(forked) == 2, (
        f"expected exactly 2 concurrent STATUS events forking from open, got {forked}"
    )
    assert {e["status"] for e in forked} == {"in_progress", "blocked"}, forked
    # Lexically-lower event UUID wins (process_status tie-break) — replay-order-independent.
    winner = min(forked, key=lambda e: e["uuid"])
    expected_status = winner["status"]
    assert status_a[seed] == expected_status, (
        f"fork tie-break did not select the lower-UUID winner: expected "
        f"{expected_status!r} (winner uuid={winner['uuid']}), got {status_a[seed]!r}; "
        f"events={forked}"
    )
    # Both clones must agree on that SAME specific winner (skew-independent union).
    assert status_b[seed] == expected_status


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
