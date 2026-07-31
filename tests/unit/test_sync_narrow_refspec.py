"""Sync self-heal under a NARROW clone refspec (bug 5546).

A single-branch clone configures ``+refs/heads/main:refs/remotes/origin/main``. A bare
``git fetch <remote> <branch>`` only opportunistically writes
``refs/remotes/<remote>/<branch>`` when the configured refspec COVERS that branch, so under
a narrow refspec the fetch succeeds (rc=0, FETCH_HEAD only) while
``rev-parse --verify <remote>/<branch>`` still fails — and ``sync.reconverge``'s guard
returns before the union merge it was written to perform is ever reached. The tracker then
never reconverges, which is why a fresh single-branch clone reads ``[]`` for a whole session
despite ``sync.pull = on``.

The fix fetches with an EXPLICIT refspec so the merge becomes reachable, and gates that merge
on a conservative safety predicate (AC1's recorded data decision):

* shared ancestry (a ``merge-base`` exists) -> union merge; positive proof of common provenance;
* no common ancestor + local store carries NO ticket events -> adopt; nothing can be lost;
* no common ancestor + local store DOES carry ticket events -> refuse, report DIVERGED.

The third case fails toward reporting rather than toward silently absorbing an unrelated
("orphan") store into the shared tickets branch.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from rebar._store import sync

NARROW_REFSPEC = "+refs/heads/main:refs/remotes/origin/main"


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


def _commit_scaffolding(repo: Path) -> str:
    """Commit ONLY the init scaffolding a fresh orphan store carries.

    Mirrors ``rebar._commands.init``: every non-ticket root entry is dot-prefixed, so such a
    store holds no ticket events and therefore has nothing that a reconverge could lose.
    """
    (repo / ".gitignore").write_text(".env-id\n.state-cache\n")
    (repo / ".gitattributes").write_text(".bridge_state/* merge=ours\n")
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    (repo / ".store-compat.json").write_text('{"format_version": 1, "required_capabilities": []}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", "chore: initialize ticket tracker")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _narrow(tracker: Path) -> None:
    """Reshape the tracker's remote into the single-branch-clone form: a refspec that does
    NOT cover ``tickets``, and no pre-existing ``refs/remotes/origin/tickets``."""
    _git(tracker, "config", "--unset-all", "remote.origin.fetch")
    _git(tracker, "config", "remote.origin.fetch", NARROW_REFSPEC)
    _git(tracker, "update-ref", "-d", "refs/remotes/origin/tickets")
    assert _git(tracker, "rev-parse", "--verify", "origin/tickets").returncode != 0, (
        "precondition: the remote-tracking ref must be absent (narrow-clone shape)"
    )
    assert _git(tracker, "config", "--get-all", "remote.origin.fetch").stdout.strip() == (
        NARROW_REFSPEC
    ), "precondition: the configured refspec must not cover the tickets branch"


# ── AC2: an already-divergent store reconverges, with no event loss ──────────────────


def test_diverged_store_reconverges_under_narrow_refspec(tmp_path: Path) -> None:
    """Two clones of ONE tracker diverge; the local one has a narrow refspec.

    This is the ticket's headline case: shared ancestry exists, so the union merge is the
    intended behaviour — but under a narrow refspec the guard at ``sync.py`` returned before
    reaching it, so the tracker stayed divergent forever.
    """
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    base_sha = _commit_event(origin, "0000-base-0000-0000", '{"e":"base"}')

    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")

    origin_sha = _commit_event(origin, "2222-orig-2222-2222", '{"e":"origin"}')
    local_sha = _commit_event(tracker, "1111-locl-1111-1111", '{"e":"local"}')
    _narrow(tracker)
    assert _git(tracker, "merge-base", base_sha, "HEAD").returncode == 0, (
        "precondition: local and origin share ancestry (a genuine divergent copy)"
    )

    sync.reconverge(tracker)

    # No event loss in EITHER direction — the union is what AC2 requires.
    assert (tracker / "2222-orig-2222-2222").is_dir(), (
        "origin-side ticket never adopted — sync did not reconverge under a narrow refspec"
    )
    assert (tracker / "1111-locl-1111-1111").is_dir(), "local ticket discarded by reconverge"
    for sha in (local_sha, origin_sha):
        assert _git(tracker, "merge-base", "--is-ancestor", sha, "HEAD").returncode == 0, (
            f"{sha} not reachable from HEAD after reconverge"
        )


# ── The empty-orphan case the bug actually produced ──────────────────────────────────


def test_empty_orphan_store_adopts_shared_tracker_under_narrow_refspec(tmp_path: Path) -> None:
    """A fresh single-branch clone whose ``init`` could not see ``origin/tickets`` takes the
    ``worktree add --orphan`` arm and produces an EMPTY orphan store. It has no common
    ancestor with the shared tracker, but it also carries no ticket events, so adopting the
    shared tracker cannot lose anything — this must self-heal."""
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    origin_sha = _commit_event(origin, "0000-aaaa-bbbb-cccc", '{"side":"origin"}')

    # Independently init'd, so there is no common ancestor by construction.
    _new_tickets_repo(tracker)
    _commit_scaffolding(tracker)  # scaffolding only: no ticket events
    assert [e for e in _git(tracker, "ls-tree", "--name-only", "HEAD").stdout.split()] and not [
        e
        for e in _git(tracker, "ls-tree", "--name-only", "HEAD").stdout.split()
        if not e.startswith(".")
    ], "precondition: the local store carries scaffolding only, no ticket events"
    _git(tracker, "remote", "add", "origin", str(origin))
    _narrow(tracker)

    sync.reconverge(tracker)

    assert (tracker / "0000-aaaa-bbbb-cccc").is_dir(), (
        "empty orphan store did not adopt the shared tracker — nothing was at risk"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0


# ── The refusal: an orphan store carrying events is never absorbed ───────────────────


def test_orphan_store_with_events_is_refused_and_reported_diverged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An orphan store that carries its OWN ticket events has no evidence of ever having
    belonged to the shared tracker. Merging it would publish an unrelated store's events onto
    the shared tickets branch, so sync must refuse and report DIVERGED.

    The remote-tracking ref is deliberately RESOLVABLE here (a wide refspec): this pins the
    refusal itself, not the unreachability the narrow refspec used to cause.
    """
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    origin_sha = _commit_event(origin, "0000-aaaa-bbbb-cccc", '{"side":"origin"}')
    _new_tickets_repo(tracker)
    local_sha = _commit_event(tracker, "1111-dddd-eeee-ffff", '{"side":"local"}')
    _git(tracker, "remote", "add", "origin", str(origin))
    _git(tracker, "fetch", "origin", "--quiet")
    assert _git(tracker, "rev-parse", "--verify", "origin/tickets").returncode == 0, (
        "precondition: the remote-tracking ref resolves, so only the predicate can refuse"
    )
    assert _git(tracker, "merge-base", "HEAD", "origin/tickets").returncode != 0, (
        "precondition: no common ancestor (an orphan store)"
    )

    with caplog.at_level(logging.WARNING, logger="rebar._store.sync"):
        sync.reconverge(tracker)

    # The shared tracker's events were NOT pulled in, and HEAD is not a merge.
    assert not (tracker / "0000-aaaa-bbbb-cccc").exists(), (
        "orphan store silently absorbed the shared tracker — the merge must be refused"
    )
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode != 0, (
        "orphan store merged the shared tracker's history"
    )
    parents = _git(tracker, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 1, f"HEAD became a merge commit, expected refusal; got {parents}"

    # Local work is untouched (refusing discards strictly less than merging did).
    assert (tracker / "1111-dddd-eeee-ffff").is_dir(), "local events lost while refusing"
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == local_sha

    # And the state is REPORTED, not silent.
    assert "DIVERGED" in caplog.text, f"no DIVERGED report was logged; got: {caplog.text!r}"
