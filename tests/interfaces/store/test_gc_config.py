"""Stock ``git gc`` is kept but forced FOREGROUND on the tickets worktree
(epic 97e7 / P1.4, corrected by bug 88eb — see ADR 0051).

A DETACHED auto-gc / ``git maintenance run --auto`` repacks the SHARED linked-worktree
object DB in the background, outside rebar's write lock, racing concurrent writers and
corrupting the store (bug 88eb). So auto-gc stays ENABLED (loose growth is still bounded)
but must never detach. These tests pin the new posture:

  * ``init`` ``--unset``s any stale ``gc.auto`` and sets ``gc.autoDetach=false`` AND
    ``maintenance.autoDetach=false`` (git >= 2.47 honors the latter; ``gc.autoDetach`` is
    only its fallback), and that migration is idempotent across re-inits — including
    healing a tracker an older rebar left at ``gc.auto=0`` OR at ``gc.autoDetach=true``.
  * The serial-gc safety invariant still holds: a hostile ``git gc --prune=now`` (run
    SERIALLY, not concurrently) collects nothing rebar cares about — all tickets remain
    replayable afterwards.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar


@pytest.fixture
def fresh_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A git repo WITHOUT a rebar tracker (no init yet)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "i"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    yield repo


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _config(tracker: Path, key: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(tracker), "config", "--get", key],
        capture_output=True,
        text=True,
    )


# ── migration: gc.auto unset, auto-gc forced FOREGROUND, idempotent ────────────
def test_init_unsets_gc_auto_and_forces_foreground_maintenance(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    # gc.auto must be UNSET (so stock gc.auto default governs — repack still fires and
    # bounds loose-object growth) — non-zero exit.
    assert _config(tracker, "gc.auto").returncode != 0, "init must not force gc.auto"
    # Auto-gc must never DETACH: a backgrounded repack of the shared object DB races
    # concurrent writers and corrupts the store (bug 88eb / ADR 0051). BOTH knobs must be
    # false (git >= 2.47 honors maintenance.autoDetach; gc.autoDetach is only its fallback).
    assert _config(tracker, "gc.autoDetach").stdout.strip() == "false"
    assert _config(tracker, "maintenance.autoDetach").stdout.strip() == "false"


def test_gc_config_migration_heals_legacy_and_is_idempotent(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    # Simulate legacy trackers: an OLDER rebar left gc.auto=0; WU-1-era rebar left
    # gc.autoDetach=true (the DETACH that bug 88eb corrects) and no maintenance.autoDetach.
    subprocess.run(["git", "-C", str(tracker), "config", "gc.auto", "0"], check=True)
    subprocess.run(["git", "-C", str(tracker), "config", "gc.autoDetach", "true"], check=True)
    subprocess.run(["git", "-C", str(tracker), "config", "--unset", "maintenance.autoDetach"])

    # Re-init heals all three (idempotent migration).
    rebar.init_repo(repo_root=str(fresh_repo))
    assert _config(tracker, "gc.auto").returncode != 0, "re-init must unset stale gc.auto"
    assert _config(tracker, "gc.autoDetach").stdout.strip() == "false", "must un-detach gc"
    assert _config(tracker, "maintenance.autoDetach").stdout.strip() == "false"

    # A third init is a no-op and must not error.
    rebar.init_repo(repo_root=str(fresh_repo))
    assert _config(tracker, "gc.auto").returncode != 0
    assert _config(tracker, "gc.autoDetach").stdout.strip() == "false"


# ── the safety invariant: gc --prune=now loses no reachable ticket data ───────
def test_gc_prune_now_preserves_all_tickets(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)

    ids = []
    for i in range(5):
        tid = rebar.create_ticket("task", f"prune-survivor-{i}", repo_root=str(fresh_repo))
        ids.append(tid["id"] if isinstance(tid, dict) else tid)

    # A maximally aggressive gc: repack + drop every UNREACHABLE object now.
    gc = subprocess.run(
        ["git", "-C", str(tracker), "gc", "--prune=now"],
        capture_output=True,
        text=True,
    )
    assert gc.returncode == 0, gc.stderr

    # Every ticket is still replayable — nothing reachable was collected.
    for i in range(5):
        hits = rebar.search(f"prune-survivor-{i}", repo_root=str(fresh_repo))
        assert hits, f"ticket {i} lost after gc --prune=now"
    listed = {t["ticket_id"] for t in rebar.list_tickets(repo_root=str(fresh_repo))}
    for tid in ids:
        assert tid in listed, f"{tid} missing from list after gc"
