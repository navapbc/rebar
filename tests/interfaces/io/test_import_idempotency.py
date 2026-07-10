"""Import idempotency + deferred-push (P1.2 T4).

Re-running an import (or resuming a partial one) must never duplicate: a record
whose source_id already exists in the target is skipped, and existing tickets are
never updated. Push is deferred during the import and a single push runs at the end.
"""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import rebar
from rebar import config


def _commit_count(repo: Path) -> int:
    """Number of commits on the tracker's tickets branch (git-level, not event count)."""
    tracker = str(config.tracker_dir(str(repo)))
    r = subprocess.run(
        ["git", "-C", tracker, "rev-list", "--count", "HEAD"], capture_output=True, text=True
    )
    return int(r.stdout.strip())


def _fresh_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _seed(repo: Path) -> None:
    root = str(repo)
    epic = rebar.create_ticket("epic", "Epic", description="e" * 60, repo_root=root)
    t1 = rebar.create_ticket("task", "Task one", repo_root=root)
    rebar.edit_ticket(t1, parent=epic, repo_root=root)
    rebar.comment(t1, "note", repo_root=root)
    rebar.transition(t1, "open", "closed", repo_root=root)


def _export(repo: Path) -> list[str]:
    buf = io.StringIO()
    rebar.export_tickets(out=buf, repo_root=str(repo))
    return buf.getvalue().splitlines()


def test_rerun_produces_zero_duplicates(tmp_path: Path) -> None:
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    _seed(src)
    lines = _export(src)

    first = rebar.import_tickets(lines, repo_root=str(dst))
    assert first["created"] == 2
    count_after_first = len(rebar.list_tickets(repo_root=str(dst)))
    commits_after_first = _commit_count(dst)

    second = rebar.import_tickets(lines, repo_root=str(dst))
    assert second["created"] == 0
    assert second["skipped"] == 2
    assert len(rebar.list_tickets(repo_root=str(dst))) == count_after_first
    # The re-run must add ZERO git commits (every record skipped by source_id) —
    # a git-level proof of idempotency stronger than the created==0 count.
    assert _commit_count(dst) == commits_after_first


def test_resume_after_partial_completes_without_duplicates(tmp_path: Path) -> None:
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    _seed(src)
    lines = _export(src)
    assert len(lines) == 2

    # Simulate a crash mid-run: import only the first record.
    rebar.import_tickets(lines[:1], repo_root=str(dst))
    assert len(rebar.list_tickets(repo_root=str(dst))) == 1

    # Resume with the full set: the already-imported one is skipped, the rest created.
    resume = rebar.import_tickets(lines, repo_root=str(dst))
    assert resume["created"] == 1
    assert resume["skipped"] == 1
    assert len(rebar.list_tickets(repo_root=str(dst))) == 2


def test_existing_ticket_is_not_updated(tmp_path: Path) -> None:
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    _seed(src)
    lines = _export(src)

    rebar.import_tickets(lines, repo_root=str(dst))
    imported = next(t for t in rebar.list_tickets(repo_root=str(dst)) if t["title"] == "Task one")
    # Locally edit the imported ticket, then re-import: the edit must survive (skip,
    # never update).
    rebar.edit_ticket(imported["ticket_id"], title="Task one (local edit)", repo_root=str(dst))
    rebar.import_tickets(lines, repo_root=str(dst))
    titles = {t["title"] for t in rebar.list_tickets(repo_root=str(dst))}
    assert "Task one (local edit)" in titles
    assert "Task one" not in titles


def test_dry_run_counts_skips_against_existing(tmp_path: Path) -> None:
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    _seed(src)
    lines = _export(src)
    rebar.import_tickets(lines, repo_root=str(dst))

    meta = rebar.import_tickets(lines, dry_run=True, repo_root=str(dst))
    assert meta["dry_run"] is True
    assert meta["would_create"] == 0
    assert meta["skipped"] == 2


def test_push_env_deferred_and_restored(tmp_path: Path, monkeypatch) -> None:
    """During the import REBAR_SYNC_PUSH is 'off'; afterward the prior value is restored."""
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    _seed(src)
    lines = _export(src)

    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")

    seen_during: list[str | None] = []
    orig_create = rebar.create_ticket

    def _spy_create(*a, **k):
        seen_during.append(os.environ.get("REBAR_SYNC_PUSH"))
        return orig_create(*a, **k)

    monkeypatch.setattr(rebar, "create_ticket", _spy_create)
    rebar.import_tickets(lines, repo_root=str(dst))

    # every write during the import saw push deferred...
    assert seen_during and all(v == "off" for v in seen_during)
    # ...and the caller's value is restored afterward.
    assert os.environ.get("REBAR_SYNC_PUSH") == "always"
