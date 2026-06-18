"""Stock ``git gc`` is trusted on the tickets worktree (epic 97e7 / P1.4, WU-1).

rebar no longer forces ``gc.auto=0``. These tests pin the new posture:

  * ``init`` ``--unset``s any stale ``gc.auto`` and sets ``gc.autoDetach=true``,
    and that migration is idempotent across re-inits (incl. healing a tracker that
    an older rebar left at ``gc.auto=0``).
  * The safety invariant holds end-to-end: because every ticket commit is
    reachable from the ``tickets`` ref, a hostile ``git gc --prune=now`` collects
    nothing rebar cares about — all tickets remain replayable afterwards.
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


# ── migration: gc.auto unset, autoDetach set, idempotent ──────────────────────
def test_init_unsets_gc_auto_and_sets_autodetach(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    # gc.auto must be UNSET (so stock gc.auto default governs) — non-zero exit.
    assert _config(tracker, "gc.auto").returncode != 0, "init must not force gc.auto"
    assert _config(tracker, "gc.autoDetach").stdout.strip() == "true"


def test_gc_config_migration_heals_legacy_and_is_idempotent(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    # Simulate a tracker an OLDER rebar left at gc.auto=0.
    subprocess.run(["git", "-C", str(tracker), "config", "gc.auto", "0"], check=True)
    assert _config(tracker, "gc.auto").stdout.strip() == "0"

    # Re-init heals it (idempotent migration).
    rebar.init_repo(repo_root=str(fresh_repo))
    assert _config(tracker, "gc.auto").returncode != 0, "re-init must unset stale gc.auto"
    assert _config(tracker, "gc.autoDetach").stdout.strip() == "true"

    # A third init is a no-op and must not error.
    rebar.init_repo(repo_root=str(fresh_repo))
    assert _config(tracker, "gc.auto").returncode != 0


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
