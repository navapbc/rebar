"""Union recovery on unrelated histories (epic 97e7 / P1.4, WU-2).

Before WU-2, ``_do_reconverge``'s no-common-ancestor path did
``git reset --hard origin/tickets``, which ORPHANED every local-only commit into
the reflog (the sole reason rebar forced ``gc.auto=0``). WU-2 replaces it with
``git merge --allow-unrelated-histories`` so both histories are unioned and no
local commit is ever discarded — which is what makes stock ``git gc`` safe.

These tests drive the real ``sync.reconverge`` engine path against a tracker whose
``origin/tickets`` is an UNRELATED history and assert the local tickets survive.
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


def test_unrelated_history_unions_and_keeps_local(unrelated_origin) -> None:
    tracker, local_sha, origin_sha = unrelated_origin

    sync.reconverge(tracker)

    # Both ticket dirs present in the working tree (union, not adoption).
    assert (tracker / "1111-dddd-eeee-ffff").is_dir(), "local ticket discarded by reconverge"
    assert (tracker / "0000-aaaa-bbbb-cccc").is_dir(), "origin ticket not adopted"

    # Crucially: the local commit is still REACHABLE from HEAD (a merge parent),
    # not orphaned into the reflog. This is the WU-2 invariant.
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0, (
        "local commit was orphaned — reset --hard regression"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0

    # HEAD is a real merge of the two unrelated histories (two parents).
    parents = _git(tracker, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3, f"expected a 2-parent merge commit, got {parents}"


def test_unrelated_history_survives_gc_prune_after_union(unrelated_origin) -> None:
    """The end-to-end invariant: after union recovery, an aggressive
    ``gc --prune=now`` collects nothing reachable — both tickets persist."""
    tracker, local_sha, origin_sha = unrelated_origin

    sync.reconverge(tracker)
    gc = _git(tracker, "gc", "--prune=now")
    assert gc.returncode == 0, gc.stderr

    for sha in (local_sha, origin_sha):
        assert _git(tracker, "cat-file", "-e", sha).returncode == 0, (
            f"{sha} lost after gc --prune=now"
        )
    assert (tracker / "1111-dddd-eeee-ffff").is_dir()
    assert (tracker / "0000-aaaa-bbbb-cccc").is_dir()
