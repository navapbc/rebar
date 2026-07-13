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
import sys
from pathlib import Path

import pytest

from rebar import _engine
from rebar._commands import fsck

pytestmark = pytest.mark.integration


# ─────────────────────────── helpers ────────────────────────────────────────
def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True)


_CLI = _engine.in_process_cli()


def _engine_run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a rebar subcommand against *repo* via the in-process CLI.

    ``engine_env`` pins REBAR_ROOT so the command's cwd-relative git
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


def _tags(repo: Path, ticket_id: str) -> set[str]:
    """Replayed tag set for *ticket_id* via `rebar show` (merged union state)."""
    out = _engine_run(repo, "show", ticket_id).stdout
    return set(json.loads(out).get("tags") or [])


def test_two_clone_concurrent_tag_adds_both_survive(two_clones):
    """P2.3: two clones concurrently add DIFFERENT tags to the same ticket; after
    reconvergence BOTH survive on BOTH clones. Under the old whole-field EDIT.tags
    (LWW) one add silently clobbered the other — the bug TAG_DELTA fixes."""
    remote, repo_a, repo_b, seed = two_clones
    tracker_a, tracker_b = _tracker(repo_a), _tracker(repo_b)

    # Diverge offline so neither clone witnesses the other's add.
    _remote_remove(tracker_a)
    _remote_remove(tracker_b)
    _engine_run(repo_a, "edit", seed, "--add-tag=alpha")
    _engine_run(repo_b, "edit", seed, "--add-tag=beta")

    # Reconverge through the real merge-as-union push/sync paths.
    _remote_add(tracker_a, remote)
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)
    _remote_add(tracker_b, remote)
    _engine_run(repo_b, "edit", seed, "--add-tag=gamma")  # B's write triggers reconverge push
    _remote_add(tracker_a, remote)
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")

    # Union of events identical on both clones, and both adds (plus gamma) survive.
    assert _event_files(tracker_a) == _event_files(tracker_b)
    tags_a, tags_b = _tags(repo_a, seed), _tags(repo_b, seed)
    assert tags_a == tags_b, f"non-deterministic tag replay: A={tags_a} B={tags_b}"
    assert {"alpha", "beta", "gamma"} <= tags_a, f"concurrent adds clobbered: {tags_a}"


def _diverge(tracker_a, tracker_b):
    _remote_remove(tracker_a)
    _remote_remove(tracker_b)


def _reconverge(remote, repo_a, repo_b, tracker_a, tracker_b, *trigger):
    """Push A (fast-forward), trigger B's merge-as-union push, read-sync A."""
    _remote_add(tracker_a, remote)
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)
    _remote_add(tracker_b, remote)
    _engine_run(repo_b, *trigger)  # B's write triggers the reconverge push
    _remote_add(tracker_a, remote)
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")


def test_two_clone_set_tags_table_converges(two_clones):
    """P2.3 --set-tags convergence table — set is compiled to a delta (add-wins),
    so concurrent set‖add / set‖set converge identically on both clones and a
    concurrent unobserved add is never silently clobbered by a 'set'."""
    remote, repo_a, repo_b, seed = two_clones
    tracker_a, tracker_b = _tracker(repo_a), _tracker(repo_b)

    # set ‖ add: A sets {x}, B concurrently adds y. add-wins -> {x, y} on both.
    _diverge(tracker_a, tracker_b)
    _engine_run(repo_a, "edit", seed, "--set-tags=x")
    _engine_run(repo_b, "edit", seed, "--add-tag=y")
    _reconverge(remote, repo_a, repo_b, tracker_a, tracker_b, "edit", seed, "--add-tag=z")
    assert _event_files(tracker_a) == _event_files(tracker_b)
    tags_a, tags_b = _tags(repo_a, seed), _tags(repo_b, seed)
    assert tags_a == tags_b, f"set‖add diverged: A={tags_a} B={tags_b}"
    assert {"x", "y"} <= tags_a, f"set silently clobbered a concurrent add: {tags_a}"

    # set ‖ set: two concurrent sets must still converge to ONE deterministic state.
    _diverge(tracker_a, tracker_b)
    _engine_run(repo_a, "edit", seed, "--set-tags=p,q")
    _engine_run(repo_b, "edit", seed, "--set-tags=q,r")
    _reconverge(remote, repo_a, repo_b, tracker_a, tracker_b, "comment", seed, "sync")
    assert _event_files(tracker_a) == _event_files(tracker_b)
    assert _tags(repo_a, seed) == _tags(repo_b, seed), "set‖set non-deterministic replay"

    # set ‖ remove: A sets {m,n} while B removes m (m is in A's observed base from
    # the prior phase? no — re-establish). Both clones must converge identically.
    # Seed a shared 'm' first so B's remove targets a witnessed tag.
    _engine_run(repo_a, "edit", seed, "--set-tags=m,n")  # online; auto-push
    _expire_sync_marker(tracker_b)
    _engine_run(repo_b, "list")
    assert "m" in _tags(repo_b, seed)
    _diverge(tracker_a, tracker_b)
    _engine_run(repo_a, "edit", seed, "--set-tags=m,n,s")  # A keeps m, adds s
    _engine_run(repo_b, "edit", seed, "--remove-tag=m")  # B drops m concurrently
    _reconverge(remote, repo_a, repo_b, tracker_a, tracker_b, "comment", seed, "sync2")
    assert _event_files(tracker_a) == _event_files(tracker_b)
    assert _tags(repo_a, seed) == _tags(repo_b, seed), "set‖remove non-deterministic replay"


def test_two_clone_add_remove_converges(two_clones):
    """P2.3 add ‖ remove (disjoint tags) over a shared base converges
    deterministically: A removes the shared tag, B adds a new one -> {new} on both."""
    remote, repo_a, repo_b, seed = two_clones
    tracker_a, tracker_b = _tracker(repo_a), _tracker(repo_b)

    # Establish a shared base tag on the pushed seed (both clones witness it).
    _engine_run(repo_a, "edit", seed, "--add-tag=shared")
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)
    _expire_sync_marker(tracker_b)  # B still has origin from the fixture -> re-sync
    _engine_run(repo_b, "list")
    assert "shared" in _tags(repo_b, seed)

    _diverge(tracker_a, tracker_b)
    _engine_run(repo_a, "edit", seed, "--remove-tag=shared")
    _engine_run(repo_b, "edit", seed, "--add-tag=extra")
    _reconverge(remote, repo_a, repo_b, tracker_a, tracker_b, "comment", seed, "sync")
    assert _event_files(tracker_a) == _event_files(tracker_b)
    tags_a, tags_b = _tags(repo_a, seed), _tags(repo_b, seed)
    assert tags_a == tags_b, f"add‖remove diverged: A={tags_a} B={tags_b}"
    assert tags_a == {"extra"}, f"expected shared removed + extra added, got {tags_a}"


# ─────────────────── HLC skewed-clock convergence (P2.1) ─────────────────────
# Two 19-digit physical-clock injections (REBAR_HLC_NOW): A runs FAST (a clock far
# in the future), B runs SLOW. The scenario proves the Hybrid Logical Clock makes
# causally-later edits win regardless of wall-clock skew — the gap raw time_ns()
# left open (last-wall-clock-writer silently clobbers).
_FAST_NOW = 5_000_000_000_000_000_000  # ~year 2128, 19 digits
_SLOW_NOW = 1_000_000_000_000_000_000  # ~year 2001, 19 digits


def _engine_run_at(repo: Path, *args: str, now: int, check: bool = True):
    """`_engine_run` with the physical HLC clock pinned to *now* (REBAR_HLC_NOW)."""
    env = _engine.engine_env(repo_root=str(repo))
    env["REBAR_HLC_NOW"] = str(now)
    return subprocess.run(
        [_CLI, *args], cwd=str(repo), env=env, text=True, capture_output=True, check=check
    )


def _show_title(repo: Path, tid: str) -> str:
    out = _engine_run(repo, "show", tid).stdout
    return json.loads(out).get("title", "")


def _edit_prefix_for_title(tracker: Path, ticket_id: str, title: str) -> int:
    """The integer filename-prefix of the EDIT event that set ``title`` (from the
    merged union on the tickets branch)."""
    listing = _git("ls-tree", "-r", "--name-only", "tickets", cwd=tracker).stdout
    for path in listing.splitlines():
        if not path.endswith("-EDIT.json") or path.split("/")[0] != ticket_id:
            continue
        ev = json.loads(_git("show", f"tickets:{path}", cwd=tracker).stdout)
        if ev.get("data", {}).get("fields", {}).get("title") == title:
            return int(path.split("/")[-1].split("-")[0])
    raise AssertionError(f"no EDIT event setting title={title!r} for {ticket_id}")


def test_hlc_skewed_clock_edit_causality_convergence(two_clones):
    """B's edit, made AFTER observing A's edit but on a far-SLOWER wall clock, must
    still win on both clones — because next_tick witnesses A's event prefix and
    ticks strictly above it. Under raw time_ns() B's small timestamp would lose
    (the clobber); the HLC flips it to the causally-correct winner."""
    remote, repo_a, repo_b, seed = two_clones
    tracker_a, tracker_b = _tracker(repo_a), _tracker(repo_b)

    # A (fast clock) edits the shared title, and its write auto-pushes to origin.
    _engine_run_at(repo_a, "edit", seed, "--title=from-A", now=_FAST_NOW)

    # B syncs so it OBSERVES A's edit before writing (the causal dependency).
    _expire_sync_marker(tracker_b)
    _engine_run(repo_b, "list")
    assert _show_title(repo_b, seed) == "from-A", "B did not observe A's edit before writing"

    # B (slow clock) now edits the SAME field. Its next_tick witnesses A's event
    # prefix (~_FAST_NOW) and ticks above it despite B's slow physical clock.
    _engine_run_at(repo_b, "edit", seed, "--title=from-B", now=_SLOW_NOW)

    # A converges via read-side sync.
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")

    # Convergence: both clones agree, and on the causally-LATER value (from-B),
    # not the one with the larger wall clock (from-A).
    assert _show_title(repo_a, seed) == "from-B", "A did not converge to the causal winner"
    assert _show_title(repo_b, seed) == "from-B", "B did not hold the causal winner"

    # The HLC witness is what made it so: B's edit prefix strictly exceeds A's,
    # even though B's physical clock (_SLOW_NOW) is far below A's (_FAST_NOW).
    prefix_a = _edit_prefix_for_title(tracker_a, seed, "from-A")
    prefix_b = _edit_prefix_for_title(tracker_a, seed, "from-B")
    assert prefix_b > prefix_a, (
        f"causal edit did not tick above the witnessed prefix: from-B={prefix_b} "
        f"!> from-A={prefix_a} (slow wall clock {_SLOW_NOW} would have lost without HLC)"
    )


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


# ─────────────────── RC2b: snapshot horizon + rebuild-on-stray (36d1) ─────────
def _seed_dir_files(tracker: Path, ticket_id: str, suffix: str) -> list[Path]:
    return sorted(p for p in (tracker / ticket_id).glob(f"*{suffix}") if not p.name.startswith("."))


def test_compaction_horizon_keeps_young_events_live(two_clones):
    """RC2b Option 3: with a horizon larger than every event's age, nothing is folded
    — recent 'hot-edge' events stay live ``.json`` (no SNAPSHOT, no ``.retired``)."""
    remote, repo_a, _repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    _remote_remove(tracker_a)

    _engine_run(repo_a, "comment", seed, "c1")
    _engine_run(repo_a, "comment", seed, "c2")

    out = _engine_run(
        repo_a, "compact", seed, "--threshold=0", "--horizon=9223372036854775807", "--skip-sync"
    ).stdout
    assert "within the compaction horizon" in out or "nothing to fold" in out, out

    after = _event_files(tracker_a)
    assert not any(n.endswith("-SNAPSHOT.json") for n in after), "no snapshot when all young"
    assert not _seed_dir_files(tracker_a, seed, ".retired"), "no source retired when all young"


def test_sub_horizon_append_orphan_recovered_by_fsck_rebuild(two_clones):
    """RC2b regression (36d1): a comment appended on clone B that clone A never saw,
    merged in AFTER A compacted, sorts before A's SNAPSHOT and is absent from
    ``source_event_uuids`` — the positional skip silently drops it (the RC2 data-loss
    class). ``fsck --repair-snapshots`` rebuilds the snapshot from the full log
    (including ``*.retired``) and folds the orphan back in.

    RED before the rebuild path (the orphan stays dropped); GREEN after.
    """
    remote, repo_a, repo_b, seed = two_clones
    tracker_a, tracker_b = _tracker(repo_a), _tracker(repo_b)

    _remote_remove(tracker_a)
    _remote_remove(tracker_b)

    # B appends a comment A will never witness before compacting.
    _engine_run(repo_b, "comment", seed, "orphan-from-B")
    b_comment = _seed_dir_files(tracker_b, seed, "-COMMENT.json")[-1]

    # A adds its own comments and compacts (folds only what A can see; horizon 0).
    _engine_run(repo_a, "comment", seed, "a1")
    _engine_run(repo_a, "comment", seed, "a2")
    out = _engine_run(repo_a, "compact", seed, "--threshold=0", "--horizon=0", "--skip-sync").stdout
    assert "compacted" in out, out

    # Merge-as-union outcome: B's comment file lands in A's ticket dir (union never
    # drops a file the other side added). It now sorts before A's SNAPSHOT.
    dest = tracker_a / seed / b_comment.name
    dest.write_text(b_comment.read_text())
    _git("add", "-A", cwd=tracker_a)
    _git("commit", "-q", "--no-verify", "-m", "merge: union in B orphan", cwd=tracker_a)

    def _show(repo: Path) -> str:
        return _engine_run(repo, "show", seed).stdout

    # RED surface: the orphan comment is dropped by the snapshot's positional skip.
    assert "orphan-from-B" not in _show(repo_a), "expected the pre-rebuild drop"

    # Remediation: rebuild the snapshot from the full log.
    fsck_out = _engine_run(repo_a, "fsck", "--repair-snapshots", check=False).stdout
    assert "rebuilt SNAPSHOT" in fsck_out, fsck_out

    # GREEN: the orphan comment is recovered, and fsck is now clean.
    assert "orphan-from-B" in _show(repo_a)
    clean = _engine_run(repo_a, "fsck", check=False)
    assert "ORPHAN_EVENT" not in clean.stdout and "SNAPSHOT_INCONSISTENT" not in clean.stdout


def test_a3_repair_dry_run_noop_then_live_repair_pretag_and_rollback(two_clones):
    """A3 (34b1) remediation on a real git-backed store: --dry-run writes/commits
    nothing; the live --repair pre-tags for rollback, retires the still-present folded
    source (SNAPSHOT_INCONSISTENT), commits, and reaches fsck-clean; resetting to the
    pre-tag restores the pre-repair tree."""
    from rebar.reducer import reduce_ticket

    remote, repo_a, _repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    seed_dir = tracker_a / seed

    # Craft SNAPSHOT_INCONSISTENT: a SNAPSHOT that lists the still-present CREATE as a
    # folded source (the live-store fault class, ~2422 of them).
    import json as _json

    create_file = next(seed_dir.glob("*-CREATE.json"))
    create_uuid = _json.loads(create_file.read_text())["uuid"]
    compiled = {k: v for k, v in reduce_ticket(str(seed_dir)).items() if k != "updated_at"}
    snap_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
    snap_name = f"9000000000000000000-{snap_uuid}-SNAPSHOT.json"
    (seed_dir / snap_name).write_text(
        _json.dumps(
            {
                "event_type": "SNAPSHOT",
                "timestamp": 9000000000000000000,
                "uuid": snap_uuid,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Test",
                "data": {"compiled_state": compiled, "source_event_uuids": [create_uuid]},
            }
        )
    )
    _git("add", "-A", cwd=tracker_a)
    _git("commit", "-q", "--no-verify", "-m", "craft: inconsistent snapshot", cwd=tracker_a)

    assert "SNAPSHOT_INCONSISTENT" in _engine_run(repo_a, "fsck", check=False).stdout
    head_before = _git("rev-parse", "HEAD", cwd=tracker_a).stdout.strip()

    # DRY-RUN: describes the repair, writes nothing, commits nothing.
    dry = _engine_run(repo_a, "fsck", "--repair", "--dry-run", check=False).stdout
    assert "0 file writes, 0 commits" in dry, dry
    assert _git("rev-parse", "HEAD", cwd=tracker_a).stdout.strip() == head_before
    assert create_file.exists(), "dry-run must not retire anything"

    # LIVE repair: pre-tag, retire the source, commit, reach fsck-clean.
    live = _engine_run(repo_a, "fsck", "--repair", check=False).stdout
    assert "pre-a3-remediation" in live, live
    assert _git("rev-parse", "pre-a3-remediation", cwd=tracker_a).returncode == 0
    assert not create_file.exists()
    assert (seed_dir / (create_file.name + ".retired")).exists()
    assert "SNAPSHOT_INCONSISTENT" not in _engine_run(repo_a, "fsck", check=False).stdout

    # ROLLBACK rehearsal: the pre-tag restores the pre-repair tree exactly.
    _git("reset", "--hard", "pre-a3-remediation", cwd=tracker_a)
    assert create_file.exists(), "rollback did not restore the pre-repair state"


def _craft_inconsistent_snapshot(tracker: Path, seed: str) -> Path:
    """Craft one SNAPSHOT_INCONSISTENT fault on *seed* (a SNAPSHOT that lists the
    still-present CREATE as a folded source — the live-store fault class) and commit
    it. Returns the still-present CREATE file that repair must retire."""
    from rebar.reducer import reduce_ticket

    seed_dir = tracker / seed
    create_file = next(seed_dir.glob("*-CREATE.json"))
    create_uuid = json.loads(create_file.read_text())["uuid"]
    compiled = {k: v for k, v in reduce_ticket(str(seed_dir)).items() if k != "updated_at"}
    snap_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
    snap_name = f"9000000000000000000-{snap_uuid}-SNAPSHOT.json"
    (seed_dir / snap_name).write_text(
        json.dumps(
            {
                "event_type": "SNAPSHOT",
                "timestamp": 9000000000000000000,
                "uuid": snap_uuid,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Test",
                "data": {"compiled_state": compiled, "source_event_uuids": [create_uuid]},
            }
        )
    )
    _git("add", "-A", cwd=tracker)
    _git("commit", "-q", "--no-verify", "-m", "craft: inconsistent snapshot", cwd=tracker)
    return create_file


def test_a3_repair_aborts_when_push_fails_leaving_pretag_for_rollback(two_clones):
    """A3 safety (34b1): if a batch push is REJECTED (the remote tickets branch diverged
    under us), the live repair ABORTS and surfaces the error rather than silently leaving
    the store partly-pushed — and the pre-tag it wrote first still enables a rollback."""
    _remote, repo_a, repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    create_file = _craft_inconsistent_snapshot(tracker_a, seed)
    assert "SNAPSHOT_INCONSISTENT" in _engine_run(repo_a, "fsck", check=False).stdout

    # Diverge origin/tickets from under A: B writes+auto-pushes a comment, so A's
    # non-fast-forward `push origin HEAD:tickets` in the repair is rejected.
    _engine_run(repo_b, "comment", seed, "divergent-from-b")

    out = _engine_run(repo_a, "fsck", "--repair", check=False).stdout
    assert "ABORT: push failed" in out, out
    # The pre-tag was written BEFORE any mutation → rollback remains possible.
    assert _git("rev-parse", "pre-a3-remediation", cwd=tracker_a).returncode == 0
    # The retire was applied+committed locally (abort is AFTER the failed push), so the
    # operator recovers via the pre-tag, not by hoping nothing was written.
    assert (
        not create_file.exists() and (tracker_a / seed / (create_file.name + ".retired")).exists()
    )


def _reconciler_advisory():
    """Load the reconciler advisory-lock module the way production does (engine dir on
    sys.path so the top-level ``rebar_reconciler`` package resolves)."""
    import sys as _sys

    from rebar._engine import engine_dir

    eng = str(engine_dir())
    if eng not in _sys.path:
        _sys.path.insert(0, eng)
    from rebar_reconciler import _advisory_lock as advisory

    return advisory


def test_a3_repair_aborts_when_a_reconciler_pass_is_in_flight(two_clones):
    """A3 safety (34b1): disabling the GHA schedule stops the NEXT pass, not one already
    running. If ``refs/reconciler/lock`` is held when repair starts, it ABORTS before any
    write (never mutating the store under a live reconciler) and repairs cleanly once the
    lock is released."""
    _remote, repo_a, _repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    create_file = _craft_inconsistent_snapshot(tracker_a, seed)

    advisory = _reconciler_advisory()
    oid = advisory.acquire_pass_lock("a3-test-pass", repo_a)
    assert oid is not None, "failed to acquire the reconciler pass lock for the test"
    try:
        out = _engine_run(repo_a, "fsck", "--repair", check=False).stdout
        assert "ABORT: a reconciler pass is in flight" in out, out
        assert create_file.exists(), "repair must NOT retire under a live reconciler pass"
    finally:
        # Force-clear the lease (the remote CAS wraps the ref in a commit, so a
        # blob-oid release no-ops — this test only needs the ref gone).
        _git("push", "origin", "--delete", "refs/reconciler/lock", cwd=repo_a, check=False)
        _git("update-ref", "-d", "refs/reconciler/lock", cwd=repo_a, check=False)

    # Lock released → the repair now proceeds and retires the still-present source.
    out2 = _engine_run(repo_a, "fsck", "--repair", check=False).stdout
    assert "ABORT" not in out2, out2
    assert not create_file.exists()
    assert "SNAPSHOT_INCONSISTENT" not in _engine_run(repo_a, "fsck", check=False).stdout


def test_a3_marker_is_optimization_not_authority_crash_before_marker(two_clones):
    """A3 safety (34b1): the per-ticket ``a3-repaired`` marker is a LOCAL, uncommitted
    optimization — fsck itself is the authoritative resumability check. A crash AFTER the
    retire+commit but BEFORE the marker write (simulated by deleting the marker) must NOT
    cause a re-repair: the re-run sees the ticket already clean and is a no-op."""
    _remote, repo_a, _repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    create_file = _craft_inconsistent_snapshot(tracker_a, seed)

    _engine_run(repo_a, "fsck", "--repair", check=False)
    assert not create_file.exists()

    git_dir = fsck._resolve_tracker_git_dir(str(tracker_a))
    marker = Path(git_dir) / "a3-repaired" / seed
    assert marker.exists(), "expected a per-ticket repair marker under the git dir"
    # The marker lives under the git dir, never the committed tree.
    assert seed not in _git("ls-files", "a3-repaired", cwd=tracker_a).stdout

    # Simulate crash-before-marker: the fix is committed but the marker never landed.
    marker.unlink()
    out = _engine_run(repo_a, "fsck", "--repair", check=False).stdout
    assert "no repairable faults" in out, out  # fsck-authoritative: nothing to redo
    assert not (tracker_a / seed / (create_file.name + ".retired" + ".retired")).exists()
    assert "SNAPSHOT_INCONSISTENT" not in _engine_run(repo_a, "fsck", check=False).stdout


def test_a3_repair_surfaces_missing_create_without_auto_writing(two_clones):
    """A3 disposition (34b1): MISSING_CREATE is human-triage only — ``fsck --repair``
    SURFACES it but never fabricates a CREATE (no automatic write). The repair plan skips
    the ticket ('no repairable faults') while the re-scan still reports the fault."""
    _remote, repo_a, _repo_b, _seed = two_clones
    tracker_a = _tracker(repo_a)

    # A ticket dir with a lone COMMENT and no CREATE → reduce_ticket returns None.
    ghost = tracker_a / "reb-ghost-nocreate"
    ghost.mkdir()
    (ghost / "1000000000000000000-cccccccc-1111-2222-3333-444444444444-COMMENT.json").write_text(
        json.dumps({"uuid": "cccccccc-1111-2222-3333-444444444444", "event_type": "COMMENT"})
    )
    _git("add", "-A", cwd=tracker_a)
    _git("commit", "-q", "--no-verify", "-m", "craft: ghost ticket missing CREATE", cwd=tracker_a)
    before = {p.name for p in ghost.iterdir()}

    out = _engine_run(repo_a, "fsck", "--repair", check=False).stdout
    assert "MISSING_CREATE" in out and "reb-ghost-nocreate" in out, out
    assert "no repairable faults" in out, out  # nothing auto-written for the ghost
    assert {p.name for p in ghost.iterdir()} == before, "repair must not write for MISSING_CREATE"


def test_two_clone_compaction_resurrection_no_data_loss_and_repairable(two_clones):
    """b306 (I1) RC1 regression: clone A compacts (folding a source), and a merge with
    clone B — which never compacted — resurrects the folded source file. Because A1
    RENAMES folded sources to ``*.retired`` (never deletes), the source bytes are never
    lost; the resurrected ``.json`` trips SNAPSHOT_INCONSISTENT, which ``fsck --repair``
    resolves by re-retiring it. RED on the pre-b306 delete behavior (the resurrected
    file would be an un-recoverable orphan); GREEN now.
    """
    remote, repo_a, _repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    seed_dir = tracker_a / seed

    # A compacts: the CREATE source is folded and RENAMED to *.retired (not deleted).
    create_file = next(seed_dir.glob("*-CREATE.json"))
    out = _engine_run(repo_a, "compact", seed, "--threshold=0", "--horizon=0", "--skip-sync").stdout
    assert "compacted" in out, out
    retired = seed_dir / (create_file.name + ".retired")
    assert retired.exists(), "b306: source must be retired"
    assert not create_file.exists(), "b306: source must be retired, not deleted"

    # Merge-as-union with a clone that still held the source resurrects the .json file.
    create_file.write_text(retired.read_text())
    _git("add", "-A", cwd=tracker_a)
    _git("commit", "-q", "--no-verify", "-m", "merge: union resurrects source", cwd=tracker_a)
    assert "SNAPSHOT_INCONSISTENT" in _engine_run(repo_a, "fsck", check=False).stdout

    # No data loss (the retired copy preserved it) and fsck --repair drives it clean.
    _engine_run(repo_a, "fsck", "--repair", check=False)
    clean = _engine_run(repo_a, "fsck", check=False).stdout
    assert "SNAPSHOT_INCONSISTENT" not in clean
    assert _engine_run(repo_a, "show", seed).returncode == 0  # ticket still reduces


def test_rebuild_restarts_from_stale_bak_sentinel(two_clones):
    """36d1 (RC2b) interrupted-rebuild restart: a ``.snapshot-rebuild.bak`` present at
    entry means a prior rebuild crashed mid-flight. rebuild_snapshot_from_full_log must
    rebuild again (idempotent), fold the merged-in orphan, and remove the sentinel after
    a clean round-trip."""
    import json as _json

    from rebar._commands.compact import rebuild_snapshot_from_full_log
    from rebar.reducer import reduce_ticket

    remote, repo_a, _repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    seed_dir = tracker_a / seed

    # An orphan COMMENT (absent from the snapshot's source_event_uuids), sorting before
    # the snapshot → the RC2 silent-drop shape.
    # Compile a CREATE-only baseline, append a COMMENT (normal HLC ts), THEN craft a
    # future-dated SNAPSHOT whose source set excludes the comment → the comment sorts
    # before the snapshot and is a genuine orphan the positional skip drops. (The
    # snapshot must be written AFTER the comment so it does not poison the HLC clock.)
    create_file = next(seed_dir.glob("*-CREATE.json"))
    create_uuid = _json.loads(create_file.read_text())["uuid"]
    compiled = {k: v for k, v in reduce_ticket(str(seed_dir)).items() if k != "updated_at"}
    _engine_run(repo_a, "comment", seed, "orphan-comment-body")
    snap_uuid = "bbbbbbbb-1111-2222-3333-444444444444"
    (seed_dir / f"9000000000000000000-{snap_uuid}-SNAPSHOT.json").write_text(
        _json.dumps(
            {
                "event_type": "SNAPSHOT",
                "timestamp": 9000000000000000000,
                "uuid": snap_uuid,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Test",
                "data": {"compiled_state": compiled, "source_event_uuids": [create_uuid]},
            }
        )
    )
    assert not any(
        "orphan-comment-body" in (c.get("body") or "")
        for c in (reduce_ticket(str(seed_dir)) or {}).get("comments", [])
    ), "orphan must be dropped before the rebuild"
    # Simulate a crashed prior rebuild: the sentinel is present at entry.
    bak = seed_dir / ".snapshot-rebuild.bak"
    bak.write_text("stale sentinel from an interrupted rebuild")

    did = rebuild_snapshot_from_full_log(str(tracker_a), seed, str(seed_dir), no_commit=True)

    assert did is True, "must restart the rebuild when a stale .bak is present"
    assert not bak.exists(), ".bak must be removed after a clean round-trip"
    # The orphan comment is folded back in — its body is present in reduced state.
    state = reduce_ticket(str(seed_dir))
    assert any("orphan-comment-body" in (c.get("body") or "") for c in state.get("comments", []))


def test_push_retry_merge_under_lock_preserves_events(two_clones):
    """A write on B that triggers a non-fast-forward push-retry merge (now taken under the
    write lock) must succeed without a spurious StoreError and lose no events — B's write,
    a second B write, and A's already-pushed write all survive (audit reliability #2, e699)."""
    remote, repo_a, repo_b, seed = two_clones
    tracker_b = _tracker(repo_b)

    # Prime B's read-side sync marker so B writes against a STALE base (does not first
    # fetch A's write) — that is what forces its push to be non-fast-forward and drives
    # the locked push-retry merge path.
    _engine_run(repo_b, "list")

    # A writes and auto-pushes, advancing origin/tickets.
    assert _engine_run(repo_a, "comment", seed, "from A").returncode == 0

    # B writes twice: each commits locally, then push_after_commit performs the non-ff
    # fetch+merge under the write lock. Both must succeed (no StoreError / non-zero exit).
    r1 = _engine_run(repo_b, "comment", seed, "from B one", check=False)
    assert r1.returncode == 0, f"B write during push-retry merge failed: {r1.stderr}"
    r2 = _engine_run(repo_b, "comment", seed, "from B two", check=False)
    assert r2.returncode == 0, f"second B write failed: {r2.stderr}"

    # No event lost: after B converges, its store carries A's comment and both of B's.
    _expire_sync_marker(tracker_b)
    _engine_run(repo_b, "list")
    shown = json.loads(_engine_run(repo_b, "show", seed).stdout)
    bodies = " ".join(c.get("body", "") for c in shown.get("comments", []))
    for expected in ("from A", "from B one", "from B two"):
        assert expected in bodies, f"event lost — {expected!r} missing from: {bodies!r}"


def test_two_clone_concurrent_claim_loser_detects_and_fork_surfaced(two_clones):
    """Two clones claim the same open ticket. The loser (lower-HLC assignee) is told it
    lost (exit 10) once its push merges the winner's claim, and the resolved STATUS fork
    is surfaced via fsck + show (audit reliability #1, story 3003)."""
    remote, repo_a, repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)

    # Prime B's read-side sync marker (fresh) so B does NOT re-fetch on its next op — B's
    # view stays at the base (seed open, A's claim not yet seen). This is what makes the
    # two clones genuinely race: B claims against a stale-open view.
    _engine_run(repo_b, "list")

    # A claims on a FAST clock; its claim auto-pushes to origin.
    a = _engine_run_at(repo_a, "claim", seed, "--assignee=alice", now=_FAST_NOW)
    assert a.returncode == 0

    # B still sees the ticket as open locally (has NOT synced A's claim). It claims on a
    # SLOW clock; its push_after_commit merges A's already-pushed claim, and B's post-push
    # re-read sees the merged assignee is alice (A's higher-HLC EDIT wins), not bob.
    b = _engine_run_at(repo_b, "claim", seed, "--assignee=bob", now=_SLOW_NOW, check=False)
    assert b.returncode == 10, f"the losing claimant must exit 10; got {b.returncode}: {b.stderr}"
    assert "claim lost" in (b.stderr + b.stdout).lower()

    # Deterministic convergence: both clones agree the ticket is assigned to alice.
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")
    assignee_a = json.loads(_engine_run(repo_a, "show", seed).stdout).get("assignee")
    assignee_b = json.loads(_engine_run(repo_b, "show", seed).stdout).get("assignee")
    assert assignee_a == assignee_b == "alice", f"assignees diverged: A={assignee_a} B={assignee_b}"

    # The resolved STATUS fork is surfaced: show carries the derived record and fsck flags it.
    shown = json.loads(_engine_run(repo_b, "show", seed).stdout)
    assert shown.get("status_fork_resolutions"), "show must surface the resolved fork record"
    fsck = json.loads(_engine_run(repo_b, "fsck", "--output", "json", check=False).stdout)
    kinds = [f.get("kind") for f in fsck.get("issues", [])]
    assert "status_fork_resolved" in kinds, f"fsck must flag the resolved fork; kinds={kinds}"


# ─────────── Real push/fetch/merge during compaction (story 1fdc) ─────────────
# The two compaction-resurrection/orphan tests above (…recovered_by_fsck_rebuild,
# …resurrection_no_data_loss_and_repairable) SIMULATE a remote append by hand-copying
# clone B's event file into clone A's tracker dir plus a ``--no-verify`` commit — they
# never drive a real ``git push``/``fetch``/merge, so real publish/reconverge timing is
# unexercised. The two tests below drive the actual engine sync (real push/fetch/merge)
# across two clones so the snapshot-horizon safety property is exercised end-to-end.
#
# HLC note (why the timestamps are injected rather than "natural"): ``hlc.next_tick`` is
# MONOTONIC — ``max(cache, witness, physical_now())+1`` — so on a ticket that already has
# a current-time event (the fixture ``seed``'s CREATE), a ``REBAR_HLC_NOW=<far-past>``
# append is floored back up to current time and can NOT produce a genuinely far-past
# event. To place events at deterministic timestamps we either (a) disable the monotonic
# clock with ``REBAR_HLC=0`` (then ``next_tick`` returns exactly ``physical_now()`` =
# the injected ``REBAR_HLC_NOW``), or (b) inject a FUTURE ``REBAR_HLC_NOW`` (which the
# monotonic floor accepts). All cross-clone ordering below is fixed by explicit
# timestamps, never by a wall-clock race (SDET: no timing races).


def _engine_run_env(repo: Path, *args: str, env_extra=None, check: bool = True):
    """``_engine_run`` with EXTRA env merged over ``engine_env`` — the injection point for
    ``REBAR_HLC_NOW`` (physical clock) / ``REBAR_HLC`` (disable the monotonic tick) /
    ``REBAR_COMPACTION_HORIZON_NS`` so the test controls event + compaction timestamps."""
    env = _engine.engine_env(repo_root=str(repo))
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.run(
        [_CLI, *args], cwd=str(repo), env=env, text=True, capture_output=True, check=check
    )


# Scenario-A constants (see the module note above for why they are injected).
_A_FAR_PAST = 10**9  # ~1970: the CREATE + folded comments' timestamp band (via REBAR_HLC=0).
_A_HORIZON = 1800 * 10**9  # 1800s — a normal conservative fold horizon.
# Compaction "now": between (_A_FAR_PAST + _A_HORIZON) and current time, so the far-past
# events fold (now - ts >= horizon) while the current-time "young" event does NOT.
_A_COMPACT_NOW = 10**15
# Scenario-B constants.
_B_FAR_FUTURE = 4 * 10**18  # ~year 2096: the adversarial far-future SNAPSHOT timestamp.
_B_NORMAL_TS = 3 * 10**18  # B's "normal-clock" orphan comment — below the far-future snapshot.


def test_scenario_a_normal_horizon_real_remote_append_visible_no_repair(two_clones):
    """Scenario A (story 1fdc): the conservative horizon keeps a concurrent REAL remote
    append safe with NO repair. A folds only FAR-PAST events while a YOUNG (current-time)
    event stays live, so the SNAPSHOT timestamp is bounded far below any current-time
    event; B's real current-time push therefore sorts AFTER the snapshot and replays on
    top — visible immediately, with no ``fsck --repair`` and no SNAPSHOT_INCONSISTENT/
    ORPHAN_EVENT. Unlike the two manual-copy regressions above, the remote append travels
    through real ``git push``/``fetch``/merge via the engine's own sync.

    A purpose-built far-past ticket is used rather than the fixture ``seed`` because the
    monotonic HLC floor (see module note) forbids a genuinely far-past event on ``seed``
    (its CREATE is at real current time); the far-past band is what makes the snapshot ts
    provably below the concurrent append."""
    remote, repo_a, repo_b, _seed = two_clones
    tracker_a, tracker_b = _tracker(repo_a), _tracker(repo_b)

    # A builds a far-past ticket: CREATE + two comments at ~1970 (REBAR_HLC=0 makes the
    # injected clock authoritative), then ONE young comment at real current time (no
    # override) that stays live and bounds the snapshot ts below current time.
    hlc0 = {"REBAR_HLC": "0"}
    tid = (
        _engine_run_env(
            repo_a,
            "create",
            "task",
            "far-past compaction subject",
            env_extra={**hlc0, "REBAR_HLC_NOW": _A_FAR_PAST},
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    _engine_run_env(
        repo_a, "comment", tid, "fold-1", env_extra={**hlc0, "REBAR_HLC_NOW": _A_FAR_PAST + 1}
    )
    _engine_run_env(
        repo_a, "comment", tid, "fold-2", env_extra={**hlc0, "REBAR_HLC_NOW": _A_FAR_PAST + 2}
    )
    _engine_run(repo_a, "comment", tid, "young-live")  # current time → stays live
    # Publish the uncompacted ticket so origin holds it.
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)

    # A compacts: folds only the far-past events (now - ts >= horizon), leaving the young
    # one live. --skip-sync keeps the timing under the test's control. Then A publishes
    # the compacted result (a fast-forward — no other clone has pushed) so origin reflects
    # the retirement (no folded source survives as live .json to later resurrect).
    out = _engine_run_env(
        repo_a,
        "compact",
        tid,
        "--threshold=0",
        f"--horizon={_A_HORIZON}",
        "--skip-sync",
        env_extra={"REBAR_HLC_NOW": _A_COMPACT_NOW},
    ).stdout
    assert "compacted" in out, out
    assert any(n.endswith("-SNAPSHOT.json") for n in _event_files(tracker_a)), (
        "compaction must have produced a real SNAPSHOT event"
    )
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)

    # B fetches the compacted ticket via the engine's read-side sync, then appends a
    # comment at REAL current time. current time >> the far-past snapshot ts, so the
    # append sorts AFTER the snapshot. The write auto-pushes to origin (real push).
    _expire_sync_marker(tracker_b)
    _engine_run(repo_b, "list")
    _engine_run(repo_b, "comment", tid, "remote-append-from-B")
    assert "remote-append-from-B" in _engine_run(repo_b, "show", tid).stdout

    # A reconverges through the read-side sync (fetch origin + union) — NO repair.
    _expire_sync_marker(tracker_a)
    show_a = _engine_run(repo_a, "show", tid).stdout
    assert "remote-append-from-B" in show_a, (
        "B's real remote append must be visible on A immediately (sorts after the "
        f"far-below snapshot ts), with no repair. show={show_a}"
    )
    # The folded far-past events survive in the snapshot's compiled_state (no data loss).
    assert "fold-1" in show_a and "fold-2" in show_a and "young-live" in show_a

    # fsck is clean: no positional-skip drop (ORPHAN_EVENT) and no resurrected folded
    # source (SNAPSHOT_INCONSISTENT). This is the whole point of the conservative horizon.
    fsck_out = _engine_run(repo_a, "fsck", check=False).stdout
    assert "SNAPSHOT_INCONSISTENT" not in fsck_out, fsck_out
    assert "ORPHAN_EVENT" not in fsck_out, fsck_out


def test_scenario_b_far_future_snapshot_orphan_real_fsck_repair_converges(two_clones):
    """Scenario B (story 1fdc): an ADVERSARIAL far-FUTURE snapshot timestamp forces the
    positional-skip data-loss class, and a REAL push/fetch/merge + ``fsck
    --repair-snapshots`` converges BOTH clones. Mirrors
    ``…resurrection_no_data_loss_and_repairable`` intent but via real sync (not a manual
    file copy) and a far-future snapshot ts.

    A compacts ``seed`` under a far-future clock so its SNAPSHOT carries a far-future
    timestamp and pushes it. B, on a normal clock, appends a comment whose timestamp
    sorts BEFORE the far-future snapshot — the merged-in orphan the snapshot's positional
    skip silently drops (RED). ``fsck --repair-snapshots`` rebuilds the snapshot from the
    full log and folds the orphan back in (GREEN); both clones then hold byte-identical
    replayed state including B's specific comment."""
    remote, repo_a, repo_b, seed = two_clones
    tracker_a, tracker_b = _tracker(repo_a), _tracker(repo_b)

    # A compacts seed with a far-future SNAPSHOT ts (horizon 0 → fold everything; the
    # SNAPSHOT ts is next_tick, floored up to REBAR_HLC_NOW). Then A publishes it.
    out = _engine_run_env(
        repo_a,
        "compact",
        seed,
        "--threshold=0",
        "--horizon=0",
        "--skip-sync",
        env_extra={"REBAR_HLC_NOW": _B_FAR_FUTURE},
    ).stdout
    assert "compacted" in out, out
    snap = _seed_dir_files(tracker_a, seed, "-SNAPSHOT.json")
    assert snap and int(snap[-1].name.split("-")[0]) >= _B_FAR_FUTURE, (
        f"SNAPSHOT must carry a far-future timestamp; files={[p.name for p in snap]}"
    )
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)

    # B, on a NORMAL clock, appends a comment. REBAR_HLC=0 pins its timestamp to
    # _B_NORMAL_TS (below the far-future snapshot) regardless of the far-future snapshot
    # its write-side sync merges in — so its filename prefix sorts BEFORE A's SNAPSHOT.
    # The write auto-pushes; its merge-as-union brings A's SNAPSHOT into B's clone.
    _engine_run_env(
        repo_b,
        "comment",
        seed,
        "orphan-from-B",
        env_extra={"REBAR_HLC": "0", "REBAR_HLC_NOW": _B_NORMAL_TS},
    )
    b_comment = _seed_dir_files(tracker_b, seed, "-COMMENT.json")[-1]
    b_uuid = json.loads(b_comment.read_text())["uuid"]

    def _comment_bodies(repo: Path) -> list[str]:
        shown = json.loads(_engine_run(repo, "show", seed, "--output", "json").stdout)
        return [c.get("body", "") for c in shown.get("comments", [])]

    # RED surface: the orphan is dropped by the far-future snapshot's positional skip.
    assert "orphan-from-B" not in _comment_bodies(repo_b), "expected the pre-repair positional drop"

    # And `fsck` DETERMINISTICALLY FLAGS the orphan BEFORE any repair — B's merged-in
    # comment sorts before the far-future snapshot yet is absent from its
    # source_event_uuids, which is exactly the ORPHAN_EVENT / SNAPSHOT_INCONSISTENT the
    # detector reports (so the drop is operator-detectable, not silent).
    pre_repair_fsck = _engine_run(repo_b, "fsck", check=False).stdout
    assert ("ORPHAN_EVENT" in pre_repair_fsck) or ("SNAPSHOT_INCONSISTENT" in pre_repair_fsck), (
        f"fsck must flag the orphan before repair; got: {pre_repair_fsck}"
    )

    # ── Remediation in the AC's prescribed (critical) sequence: A repairs FIRST. ──
    # A first fetches B's merged-in orphan (real fetch/merge), THEN rebuilds its snapshot
    # from the full log — folding the orphan back in — and PUBLISHES the repaired commit.
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")  # real fetch/merge: A now holds B's orphan
    a_repair = _engine_run(repo_a, "fsck", "--repair-snapshots", check=False).stdout
    assert "rebuilt SNAPSHOT" in a_repair, a_repair
    assert "orphan-from-B" in _comment_bodies(repo_a), "A repair must fold in the orphan"
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)

    # THEN B fetches+syncs A's repaired snapshot and runs `fsck --repair` to converge.
    _expire_sync_marker(tracker_b)
    _engine_run(repo_b, "list")  # real fetch/merge of A's repaired commit
    _engine_run(repo_b, "fsck", "--repair", check=False)
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_b, check=False)
    assert "orphan-from-B" in _comment_bodies(repo_b), "B must hold the orphan after sync + repair"
    # A reconverges onto any commit B published during its repair.
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")

    # GREEN convergence oracle — concrete, not merely exit 0. Parse each clone's replayed
    # state and assert byte-equality. ``updated_at`` is a DERIVED presentation field
    # (recomputed on every replay from the latest event) — popped from both before the
    # comparison so an equality mismatch reflects real state divergence, not the derived
    # clock. Everything else must match exactly.
    state_a = json.loads(_engine_run(repo_a, "show", seed, "--output", "json").stdout)
    state_b = json.loads(_engine_run(repo_b, "show", seed, "--output", "json").stdout)
    state_a.pop("updated_at", None)
    state_b.pop("updated_at", None)
    assert state_a == state_b, f"clones did not converge byte-equal: A={state_a} B={state_b}"

    # B's SPECIFIC pre-push comment is present in BOTH clones — by its replayed body and by
    # its captured event UUID's event file surviving on both trackers. The rebuild folds
    # the comment into the new snapshot and RENAMES its source to ``*.retired`` (never
    # deletes — no data loss), so the presence check spans the full committed listing
    # (``.json`` + ``.retired``), not just live ``.json`` events.
    def _committed_files(tracker: Path) -> str:
        return _git("ls-tree", "-r", "--name-only", "tickets", cwd=tracker).stdout

    assert "orphan-from-B" in _comment_bodies(repo_a)
    assert "orphan-from-B" in _comment_bodies(repo_b)
    assert b_uuid in _committed_files(tracker_a), f"B's comment {b_uuid} lost on A"
    assert b_uuid in _committed_files(tracker_b), f"B's comment {b_uuid} lost on B"


# ──────────── Parent-first claim cascade cross-agent races (story f476) ───────────
# The single-agent parent-first cascade (claiming an open child pulls its still-open
# parent into progress under the same assignee) is covered elsewhere. The two tests
# below add the CROSS-agent race coverage that Concurrency Doctrine sub-cases (a)
# [same-tracker, two processes] and (b) [two offline clones] describe.
def _last_id(cp: subprocess.CompletedProcess) -> str:
    """The ticket id a `create` prints on the last stdout line (warnings go to stderr)."""
    return cp.stdout.strip().splitlines()[-1]


def _status_assignee(repo: Path, ticket_id: str) -> tuple[str, str | None]:
    d = json.loads(_engine_run(repo, "show", ticket_id).stdout)
    return d.get("status", ""), d.get("assignee")


def _dirs_with_blob(tracker: Path, needle: str) -> set[str]:
    """Ticket-dir ids whose committed union holds an event file containing *needle*."""
    listing = _git("ls-tree", "-r", "--name-only", "tickets", cwd=tracker).stdout
    hit: set[str] = set()
    for path in listing.splitlines():
        if not path.endswith(".json"):
            continue
        if needle in _git("show", f"tickets:{path}", cwd=tracker).stdout:
            hit.add(path.split("/")[0])
    return hit


def test_parent_cascade_same_tracker_race_winner_takes_parent_loser_aborts(two_clones):
    """Same-tracker parent-cascade race (Concurrency Doctrine sub-case a). A parent
    story P (open) with two open children C1/C2; two REAL processes concurrently claim
    *different* children on the ONE tracker. Each child needs only its own single
    claim — the sole point of contention is P's ``open -> in_progress`` driven by the
    parent-first cascade.

    Contract asserted here: the two claims of DIFFERENT children never truly conflict,
    so BOTH succeed (exit 0) and BOTH children end ``in_progress`` under their own
    assignee. The only real contention is P's ``open -> in_progress``: whichever
    cascade commits P first wins its ownership; the other process, arriving after the
    lock shows P already ``in_progress``, does NOT re-cascade — it just claims its own
    child (matching the single-agent contract "parent already in_progress -> only the
    requested ticket moves"). Which agent's name lands on P is nondeterministic (either
    is valid); everything else is deterministic.

    Regression note: this pins the fix for the cascade TOCTOU where a concurrent
    different-child claim used to abort with exit 10 because the parent-claim decision
    was taken on an unlocked, stale ``open`` read and the locked parent claim then
    rejected the second cascade. The cascade now treats a parent that a peer has
    already progressed as a benign no-op and proceeds to claim the child.
    """
    _remote, repo_a, _repo_b, _seed = two_clones
    parent = _last_id(_engine_run(repo_a, "create", "story", "cascade race parent"))
    c1 = _last_id(_engine_run(repo_a, "create", "task", "cascade child one", "--parent", parent))
    c2 = _last_id(_engine_run(repo_a, "create", "task", "cascade child two", "--parent", parent))

    env = _engine.engine_env(repo_root=str(repo_a))

    def _spawn(child: str, who: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "rebar.cli", "claim", child, f"--assignee={who}"],
            cwd=str(repo_a),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    p1 = _spawn(c1, "alice")
    p2 = _spawn(c2, "bob")
    _o1, e1 = p1.communicate()
    _o2, e2 = p2.communicate()

    # Different children never conflict: BOTH processes succeed.
    assert p1.returncode == 0, (p1.returncode, e1)
    assert p2.returncode == 0, (p2.returncode, e2)

    # Each child is claimed under its own assignee (the lock serialized the shared parent
    # cascade; neither child claim was aborted).
    assert _status_assignee(repo_a, c1) == ("in_progress", "alice"), _status_assignee(repo_a, c1)
    assert _status_assignee(repo_a, c2) == ("in_progress", "bob"), _status_assignee(repo_a, c2)

    # P lands in_progress, owned by whichever cascade committed it first (either is valid).
    p_status, p_assignee = _status_assignee(repo_a, parent)
    assert p_status == "in_progress", p_status
    assert p_assignee in {"alice", "bob"}, p_assignee


def test_parent_cascade_two_clone_offline_race_forks_resolved_independently(two_clones):
    """Two-clone offline parent-cascade race (Concurrency Doctrine sub-case b), modeled
    on ``test_two_clone_concurrent_claim_loser_detects_and_fork_surfaced`` but driven
    THROUGH the parent-first cascade and onto the SAME child.

    A (FAST clock) claims child C → cascades to claim parent P (both under alice).
    B (SLOW clock) claims the SAME C → cascades to claim P (both under bob). Both
    succeed locally because each clone is fully offline and never witnesses the other.
    After reconvergence BOTH P and C are STATUS forks, each resolved INDEPENDENTLY by
    the HLC/UUID tie-break to the FAST-clock winner (alice). The loser's (bob)
    already-written claim events are LEFT IN PLACE (orphaned under bob), never rolled
    back or tombstoned — convergence is by tie-break + the ``STATUS_FORK_RESOLVED``
    signal, not by deletion.
    """
    remote, repo_a, repo_b, _seed = two_clones
    tracker_a, tracker_b = _tracker(repo_a), _tracker(repo_b)

    # Parent + child created on A (online → auto-push); B syncs and sees both as open.
    parent = _last_id(_engine_run(repo_a, "create", "story", "offline cascade parent"))
    child = _last_id(
        _engine_run(repo_a, "create", "task", "offline cascade child", "--parent", parent)
    )
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)
    _expire_sync_marker(tracker_b)
    _engine_run(repo_b, "list")
    assert _status_assignee(repo_b, parent)[0] == "open", "B must see parent open pre-race"
    assert _status_assignee(repo_b, child)[0] == "open", "B must see child open pre-race"

    # Diverge offline; prime each read marker so neither re-syncs during the claims.
    _remote_remove(tracker_a)
    _remote_remove(tracker_b)
    _engine_run(repo_a, "list")
    _engine_run(repo_b, "list")

    # Both claim the SAME child; the cascade pulls the open parent into progress too.
    a = _engine_run_at(repo_a, "claim", child, "--assignee=alice", now=_FAST_NOW)
    b = _engine_run_at(repo_b, "claim", child, "--assignee=bob", now=_SLOW_NOW)
    assert a.returncode == 0, a.stderr
    assert b.returncode == 0, b.stderr
    # Offline, each clone locally believes it owns BOTH P and C (no contention seen yet).
    assert _status_assignee(repo_a, parent) == ("in_progress", "alice")
    assert _status_assignee(repo_a, child) == ("in_progress", "alice")
    assert _status_assignee(repo_b, parent) == ("in_progress", "bob")
    assert _status_assignee(repo_b, child) == ("in_progress", "bob")

    # Reconverge: A fast-forwards over the (base) remote; B's next write triggers the
    # real merge-as-union push that folds in A's already-pushed claims; A read-syncs.
    _remote_add(tracker_a, remote)
    _git("push", "-q", "origin", "HEAD:tickets", cwd=tracker_a)
    _remote_add(tracker_b, remote)
    _engine_run(repo_b, "comment", child, "trigger reconverge push from B")
    _remote_add(tracker_a, remote)
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")
    _expire_sync_marker(tracker_a)
    _engine_run(repo_a, "list")

    # (1) Deterministic convergence: both clones agree, on the FAST-clock winner (alice),
    # for BOTH the parent and the child fork.
    for tid in (parent, child):
        sa, sb = _status_assignee(repo_a, tid), _status_assignee(repo_b, tid)
        assert sa == sb, f"{tid} diverged across clones: A={sa} B={sb}"
        assert sa == ("in_progress", "alice"), f"{tid} did not converge to the FAST winner: {sa}"

    # (2) BOTH P and C surface the resolved fork via show, and the two resolutions are
    # INDEPENDENT records (the child fork is resolved in its own right — disjoint event
    # UUIDs from the parent's resolution).
    p_res = json.loads(_engine_run(repo_b, "show", parent).stdout).get("status_fork_resolutions")
    c_res = json.loads(_engine_run(repo_b, "show", child).stdout).get("status_fork_resolutions")
    assert p_res, "parent must surface its resolved fork"
    assert c_res, "child must surface its OWN resolved fork (resolved independently)"
    p_uuids = {u for r in p_res for u in (r.get("winner_uuid"), r.get("dropped_uuid")) if u}
    c_uuids = {u for r in c_res for u in (r.get("winner_uuid"), r.get("dropped_uuid")) if u}
    assert p_uuids and c_uuids, (p_uuids, c_uuids)
    assert p_uuids.isdisjoint(c_uuids), f"parent/child forks not independent: {p_uuids} & {c_uuids}"

    # ...and fsck flags BOTH tickets' resolved forks.
    fsck_out = json.loads(_engine_run(repo_b, "fsck", "--output", "json", check=False).stdout)
    forked = {
        f.get("ticket_id")
        for f in fsck_out.get("issues", [])
        if f.get("kind") == "status_fork_resolved"
    }
    assert {parent, child} <= forked, f"fsck must flag both forks; got {forked}"

    # (3) The loser's (bob) claim is NOT rolled back: bob's assignee claim EVENT still
    # exists in the committed union under BOTH the parent and child ticket dirs. (The
    # STATUS fork resolved against bob, but the event file that recorded bob's ownership
    # is left in place — orphaned, never deleted/tombstoned.)
    bob_dirs = _dirs_with_blob(tracker_a, '"assignee":"bob"')
    assert parent in bob_dirs, f"loser's parent claim event was rolled back (dirs={bob_dirs})"
    assert child in bob_dirs, f"loser's child claim event was rolled back (dirs={bob_dirs})"
