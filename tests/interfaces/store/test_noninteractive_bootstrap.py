"""Non-interactive bootstrap when origin/tickets already exists (bug wet-chair-peg).

A fresh clone whose remote already has a ``tickets`` branch could not be used in a
non-interactive environment: any auto-init-triggering command died with
"ticket system not initialized ... (auto-init requires an interactive terminal)".
Attaching to an existing ``origin/tickets`` only MOUNTS existing shared state (it
does not fabricate a new orphan store), so — like the worktree-symlink case — it
must happen automatically, even with no TTY.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._cli import _init


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def clone_with_origin_tickets(tmp_path, monkeypatch):
    """A bare origin carrying a seeded `tickets` branch, plus a FRESH clone that has
    no local `.tickets-tracker` yet. Yields (clone_path, seeded_ticket_id)."""
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", cwd=seed)
    _git("config", "user.email", "t@t", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    _git("commit", "-q", "--allow-empty", "-m", "root", cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)

    monkeypatch.setenv("REBAR_ROOT", str(seed))
    rebar.init_repo(repo_root=str(seed))
    tid = rebar.create_ticket("task", "findme via bootstrap", repo_root=str(seed))
    # Publish the tickets branch to origin.
    _git("push", "-q", "origin", "tickets:tickets", cwd=seed / ".tickets-tracker")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    assert not (clone / ".tickets-tracker").exists()
    # Confirm the remote-tracking ref exists in the clone.
    _git("rev-parse", "--verify", "origin/tickets", cwd=clone)
    return clone, tid


def test_noninteractive_read_attaches_to_existing_origin_tickets(
    clone_with_origin_tickets, monkeypatch
):
    clone, tid = clone_with_origin_tickets
    monkeypatch.setenv("REBAR_ROOT", str(clone))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)
    # Force the non-interactive branch deterministically (the bug's environment).
    monkeypatch.setattr(_init, "_is_interactive", lambda: False)

    # Previously raised SystemExit("... auto-init requires an interactive terminal").
    _init.ensure_initialized(init_only=True)

    # The tracker is now mounted by attaching to the existing shared store, so the
    # seeded ticket is visible (not a fresh empty orphan).
    assert (clone / ".tickets-tracker").is_dir()
    hits = rebar.search("findme via bootstrap", repo_root=str(clone))
    assert any(h["ticket_id"] == tid or h.get("id") == tid for h in hits), hits


def test_genuine_first_time_init_still_requires_consent_noninteractively(tmp_path, monkeypatch):
    """The consent gate must be preserved: with NO tickets branch to attach to
    (local or remote), a first-time init still mutates the host repo, so a
    non-interactive auto-init must still refuse — not silently fabricate a store."""
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    repo = tmp_path / "greenfield"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "root", cwd=repo)

    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)
    monkeypatch.setattr(_init, "_is_interactive", lambda: False)

    with pytest.raises(SystemExit):
        _init.ensure_initialized(init_only=True)
    assert not (repo / ".tickets-tracker").exists()
