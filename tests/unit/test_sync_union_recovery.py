"""Recovery on unrelated histories (epic 97e7 / P1.4, WU-2; narrowed by bug 5546).

Before WU-2, ``_do_reconverge``'s no-common-ancestor path did
``git reset --hard origin/tickets``, which ORPHANED every local-only commit into
the reflog (the sole reason rebar forced ``gc.auto=0``). WU-2 replaced it with
``git merge --allow-unrelated-histories`` so no local commit is ever discarded — which
is what makes stock ``git gc`` safe.

**Bug 5546 narrowed WHICH unrelated histories are merged, without weakening that
invariant.** A store with no common ancestor that carries its OWN ticket events is an
accidentally-created ORPHAN store, not a divergent copy of the shared tracker; unioning it
would publish an unrelated store's events onto the shared tickets branch. Sync now REFUSES
that case and reports DIVERGED (AC1's recorded data decision), and unions an unrelated
history only when the local store has no ticket events to lose. Refusing discards strictly
less than merging did, so WU-2's data-safety invariant — every local commit stays reachable
from HEAD and therefore survives an aggressive ``gc --prune=now`` — is preserved, and these
tests still assert it. Only the mechanism assertion changed: union -> refusal.

The union path itself is still covered end-to-end (empty-orphan adopt, and the
shared-ancestry diverged merge) in ``tests/unit/test_sync_narrow_refspec.py``.
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
