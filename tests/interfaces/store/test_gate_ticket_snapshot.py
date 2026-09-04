"""Feature C — the gates read a PINNED, separate snapshot of the TICKET store.

The code-reading gates run their agent against an attested CODE snapshot, but the ticket
store lives on the orphan ``tickets`` branch (gitignored ``.tickets-tracker/``) and is
ABSENT from that code snapshot — so the agent's rebar ticket tools would error
(``cannot list '<snapshot>/.tickets-tracker'``). This pins a separate, read-only copy of
the ticket store and points the rebar ticket tools at it, mirroring the code-root seam.

The load-bearing contract asserted here: after an attested ``resolve_gate_handle`` + entering
``gate_read_root``, ``current_tickets_root()`` is set to a materialized store whose
``.tickets-tracker/`` holds the ticket's event dir, and ``rebar.show_ticket`` reads it
successfully (no ``cannot list`` error). Local mode leaves ``tickets_path`` ``None``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._snapshot import materialize_tickets
from rebar.llm import gate_source
from rebar.llm.gate_context import current_tickets_root


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def gate_tmpdir(monkeypatch, tmp_path):
    base = tmp_path / "gate-store"
    base.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(base))
    return base


@pytest.fixture
def repo_with_origin(tmp_path, monkeypatch):
    """A rebar repo with an ``origin`` remote (mirrors the fixture in
    ``test_gate_source_threading.py``): a code commit on ``main`` is pushed to origin, and a
    rebar ticket is created (auto-committed + auto-pushed to ``origin/tickets``)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    (repo / "sentinel.txt").write_text("from-main\n")
    _git(repo, "add", "sentinel.txt")
    _git(repo, "commit", "-q", "-m", "main content")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    # A ticket — created AFTER origin is wired, so the write auto-pushes to origin/tickets.
    tid = rebar.create_ticket("task", "feature-C ticket-snapshot test", repo_root=str(repo))
    return repo, tid


# --------------------------------------------------------------------------------------
# (a) materialize_tickets produces a store whose .tickets-tracker/ holds the event dir
# --------------------------------------------------------------------------------------
def test_materialize_tickets_holds_event_dir(repo_with_origin, gate_tmpdir):
    repo, tid = repo_with_origin
    root = Path(materialize_tickets(repo_root=str(repo)))
    tracker = root / ".tickets-tracker"
    assert tracker.is_dir()
    # The ticket's event dir is named by its (full) id; the short id is a prefix of it.
    short = tid.split("-")[0]
    matches = [d for d in tracker.iterdir() if d.is_dir() and d.name.startswith(short)]
    assert matches, f"no event dir for {tid!r} under {tracker}"


def test_materialize_tickets_caches_by_path(repo_with_origin, gate_tmpdir):
    repo, _tid = repo_with_origin
    first = materialize_tickets(repo_root=str(repo))
    # Cache hit: same pinned SHA -> same path, no rebuild.
    second = materialize_tickets(repo_root=str(repo))
    assert first == second
    # The root is namespaced with a `tickets-` prefix (never collides with a code entry).
    assert Path(first).name.startswith("tickets-")


# --------------------------------------------------------------------------------------
# (b) attested: current_tickets_root() is set + show_ticket reads the pinned store
# --------------------------------------------------------------------------------------
def test_attested_gate_reroots_ticket_tools_to_pinned_store(repo_with_origin, gate_tmpdir):
    repo, tid = repo_with_origin
    handle = gate_source.resolve_gate_handle("origin/main", "attested", str(repo))
    assert handle.tickets_path is not None
    assert Path(handle.tickets_path).is_dir()

    with gate_source.gate_read_root(handle):
        pinned = current_tickets_root()
        assert pinned == handle.tickets_path
        # The agent's rebar ticket tools resolve the store under this root — and reading
        # it succeeds (no "cannot list '<snapshot>/.tickets-tracker'" error), because the
        # store was materialized there rather than left absent from the code snapshot.
        state = rebar.show_ticket(tid, repo_root=pinned)
        assert state["title"] == "feature-C ticket-snapshot test"
    # Reverts cleanly once the gate session exits.
    assert current_tickets_root() is None


# --------------------------------------------------------------------------------------
# (c) local mode leaves tickets_path None (the live checkout already has .tickets-tracker)
# --------------------------------------------------------------------------------------
def test_local_mode_leaves_tickets_path_none(repo_with_origin, gate_tmpdir):
    repo, _tid = repo_with_origin
    handle = gate_source.resolve_gate_handle("origin/main", "local", str(repo))
    assert handle.tickets_path is None
    with gate_source.gate_read_root(handle):
        assert current_tickets_root() is None


# --------------------------------------------------------------------------------------
# (d) bug 2a6f — the pin tracks the LIVE store, never the code repo's stale `tickets` mirror
#
# `.tickets-tracker/` is a SEPARATE repository. The code repo's `refs/heads/tickets` is only a
# mirror of it that advances on fetch, so pinning that ref showed the gate an arbitrarily old
# store — in the wild, 6757 commits behind, which made the completion verifier report recorded
# comments as nonexistent and block a correct close. The close gate is the path that hurt:
# `close_precheck` passes `fetch=False` (right for the LOCAL code ref `HEAD`), and that flag was
# threaded into the ticket pin, taking the no-fetch branch onto the stale mirror.
# --------------------------------------------------------------------------------------
def _detach_tracker_as_clone(repo: Path) -> str:
    """Rebuild `.tickets-tracker` in the layout a long-lived checkout actually has: a STANDALONE
    CLONE of the tickets branch rather than a linked worktree of the code repo.

    `rebar.init_repo` creates the tracker as a linked worktree, whose refs are the code repo's
    OWN refs — so `refs/heads/tickets` and the tracker's HEAD are the same pointer and can never
    disagree. A checkout that has been around a while has the clone layout instead (separate
    object store, separate refs), and there `refs/heads/tickets` is only a mirror that advances
    on fetch. That is the layout the bug lives in, so the regression must be exercised in it.

    Returns the code repo's now-frozen `refs/heads/tickets` — the stale sha the pin used to take.
    """
    tracker = repo / ".tickets-tracker"
    remote = _git(tracker, "remote", "get-url", "origin")
    # `.env-id` is gitignored — it is local state, not branch content — so a clone would not
    # carry it and every write would refuse with "ticket system not initialized".
    env_id = (tracker / ".env-id").read_text()
    _git(tracker, "push", "-q", "origin", "tickets")
    _git(repo, "worktree", "remove", "--force", ".tickets-tracker")
    subprocess.run(["git", "clone", "-q", "-b", "tickets", remote, str(tracker)], check=True)
    (tracker / ".env-id").write_text(env_id)
    _git(tracker, "config", "user.email", "t@example.com")
    _git(tracker, "config", "user.name", "Test")
    _git(tracker, "config", "commit.gpgsign", "false")
    return _git(repo, "rev-parse", "refs/heads/tickets")


def test_close_path_pin_sees_writes_made_after_the_mirror_went_stale(repo_with_origin, gate_tmpdir):
    """THE regression. Pre-fix this pinned `refs/heads/tickets` and the comment was invisible,
    so the verifier reported `comments: []` for a ticket that demonstrably had one."""
    repo, tid = repo_with_origin
    stale = _detach_tracker_as_clone(repo)

    rebar.comment(tid, "premise validated — evidence recorded here", repo_root=str(repo))
    assert _git(repo, "rev-parse", "refs/heads/tickets") == stale, (
        "fixture is not reproducing mirror drift — the write advanced the code repo's ref"
    )

    # fetch=False is what the close gate passes.
    root = materialize_tickets(repo_root=str(repo), fetch=False)
    assert Path(root).name != f"tickets-{stale}", "pinned the stale mirror, not the live store"

    state = rebar.show_ticket(tid, repo_root=root)
    bodies = [c.get("body", "") for c in state.get("comments") or []]
    assert any("premise validated" in b for b in bodies), (
        f"comment written before the pin is not visible in the snapshot: {bodies!r}"
    )


def test_fetch_true_pin_loses_nothing_that_origin_holds(repo_with_origin, gate_tmpdir):
    """The `fetch=True` consumers (`review-plan`, standalone `verify-completion`, MCP) used to
    pin `origin/tickets` directly. Preferring the live tracker must not lose content only the
    remote has — the reconverge-then-confirm rule pulls it in first, and falls back to the old
    direct pin when it cannot confirm HEAD is at-or-ahead."""
    repo, tid = repo_with_origin
    tracker = repo / ".tickets-tracker"
    remote_url = _git(tracker, "remote", "get-url", "origin")

    # A second agent pushes an event to the shared ref that this checkout has never seen.
    peer = repo.parent / "peer-tracker"
    subprocess.run(["git", "clone", "-q", "-b", "tickets", remote_url, str(peer)], check=True)
    _git(peer, "config", "user.email", "peer@example.com")
    _git(peer, "config", "user.name", "Peer")
    _git(peer, "config", "commit.gpgsign", "false")
    (peer / "from-peer.marker").write_text("written by another agent\n")
    _git(peer, "add", "from-peer.marker")
    _git(peer, "commit", "-q", "-m", "peer event")
    _git(peer, "push", "-q", "origin", "tickets")

    root = Path(materialize_tickets(repo_root=str(repo), fetch=True))

    assert (root / ".tickets-tracker" / "from-peer.marker").is_file(), (
        "content present only on origin/tickets was lost by the new pin"
    )
    # …and the local ticket is still readable from the same snapshot.
    assert rebar.show_ticket(tid, repo_root=str(root))["title"]


def test_falls_back_to_the_ref_chain_when_no_tracker_repo_is_present(
    repo_with_origin, gate_tmpdir, tmp_path
):
    """CI and checkout-less environments have no `.tickets-tracker` to pin from; those must keep
    the pre-existing `origin/tickets` -> local-branch resolution rather than failing."""
    repo, tid = repo_with_origin
    from rebar._snapshot import repo_snapshot as rs

    trackerless = tmp_path / "trackerless"
    trackerless.mkdir()
    assert rs._pin_tickets_sha(str(trackerless), "tickets", "origin", fetch=True) is None

    # The real repo (which HAS a tracker) still resolves and reads fine through the chain.
    root = materialize_tickets(repo_root=str(repo), fetch=True)
    assert rebar.show_ticket(tid, repo_root=root)["title"]
