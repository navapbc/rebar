"""Shared fixture helpers for writes on a clone that a reclaim publish-swap superseded.

A reclaim replaces the shared ``tickets`` history with a parentless baseline whose store
epoch record names the epoch it replaced. These helpers build a real bare origin, a converged
rebar repo, publish such a baseline, and pin the read-path throttle so a write's own reads do
not reconverge first (bugs smiling-ancient-finwhale and its follow-up). Import from here, not
from a test module.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from _git_upkeep import init_bare_remote

import rebar
from rebar._engine_support import reads
from rebar._store import compat

NEW_EPOCH = "finwhale-reclaimed-epoch"


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def git_rc(*args: str, cwd: Path) -> int:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False).returncode


def build_shared_store(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path, str]:
    """An initialized repo whose tracker is converged with a real bare origin.

    Returns ``(repo, tracker, origin, seed_ticket_id)``.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    init_bare_remote(origin)
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("config", "user.email", "t@t.co", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    git("remote", "add", "origin", str(origin), cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)  # a code HEAD for gate reads
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    rebar.init_repo(repo_root=str(repo))
    tracker = repo / ".tickets-tracker"
    seed = rebar.create_ticket("task", "seed", repo_root=str(repo))
    git("fetch", "-q", "origin", "tickets", cwd=tracker)
    assert git("rev-parse", "HEAD", cwd=tracker) == git("rev-parse", "origin/tickets", cwd=tracker)
    return repo, tracker, origin, seed


def publish_rewritten_baseline(tmp_path: Path, origin: Path, record: dict) -> str:
    """Force-publish a parentless copy of origin's tree stamped with ``record``."""
    publisher = tmp_path / "publisher"
    git("clone", "-q", "-b", "tickets", str(origin), str(publisher), cwd=tmp_path)
    git("config", "user.email", "p@t.co", cwd=publisher)
    git("config", "user.name", "p", cwd=publisher)
    git("checkout", "-q", "--orphan", "reclaimed", cwd=publisher)
    (publisher / compat.COMPAT_FILENAME).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    git("add", "-A", cwd=publisher)
    git("commit", "-q", "--no-verify", "-m", "reclaimed baseline", cwd=publisher)
    git("push", "-q", "-f", "origin", "HEAD:tickets", cwd=publisher)
    return git("rev-parse", "HEAD", cwd=publisher)


def mark_recent_read(tracker: Path) -> None:
    """Record a read-path reconverge as just run, as on a server that reads constantly.

    The read path reconverges at most once a minute; inside that window the write's own
    reads skip it, so nothing moves the clone before the write path sees it.
    """
    marker = reads.sync_marker_path(str(tracker.resolve()))
    Path(marker).write_text(str(int(time.time())), encoding="utf-8")


def local_record(tracker: Path) -> dict:
    return json.loads((tracker / compat.COMPAT_FILENAME).read_text(encoding="utf-8"))


def remote_tip(origin: Path) -> str:
    return git("rev-parse", "refs/heads/tickets", cwd=origin)


def supersede(tracker: Path, origin: Path, tmp_path: Path) -> str:
    """Publish a baseline that supersedes the clone's epoch, fetch it, and pin the throttle."""
    baseline = publish_rewritten_baseline(
        tmp_path, origin, compat.superseding_record(local_record(tracker), NEW_EPOCH)
    )
    git("fetch", "-q", "origin", "tickets", cwd=tracker)
    mark_recent_read(tracker)
    assert git_rc("merge-base", "HEAD", "origin/tickets", cwd=tracker) != 0, (
        "precondition: the fetched reclaimed baseline shares no commit with the clone"
    )
    return baseline


def publish_unrelated(tracker: Path, origin: Path, tmp_path: Path) -> str:
    """Publish a parentless baseline whose lineage does NOT name the clone's epoch."""
    record = {**local_record(tracker), "epoch": NEW_EPOCH, "epoch_lineage": ["another-epoch"]}
    sha = publish_rewritten_baseline(tmp_path, origin, record)
    git("fetch", "-q", "origin", "tickets", cwd=tracker)
    mark_recent_read(tracker)
    return sha


def added_events(origin: Path, baseline: str, suffix: str) -> list[str]:
    """Event paths the remote tip added on top of ``baseline`` (asserting it builds on it)."""
    tip = remote_tip(origin)
    assert git_rc("merge-base", "--is-ancestor", baseline, tip, cwd=origin) == 0, (
        "the write must be published on top of the reclaimed baseline"
    )
    added = git("diff", "--name-only", baseline, tip, cwd=origin).splitlines()
    return [path for path in added if path.endswith(suffix)]
