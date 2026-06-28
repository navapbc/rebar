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
from rebar.llm.config import current_tickets_root


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
