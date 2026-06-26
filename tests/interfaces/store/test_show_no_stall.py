"""A read's freshness reconverge must not stall on a held write lock (slim-fetch-ledge).

`rebar show` runs a throttled fetch+reconverge (`reads.ensure_fresh`) before
reading. The reconverge acquired the write lock with a 15s timeout, so while a
concurrent background push held that lock, `show` STALLED for many seconds — long
enough that a consumer piping `show` into a parser read an empty/incomplete buffer
(or timed out), the empty-stdout-exit-0 symptom. A read must prefer the local
snapshot promptly over a long stall, so the read-path reconverge now waits only
briefly for the lock and otherwise proceeds with local state.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import rebar
from rebar._engine_support import reads
from rebar._store import lock as _lock


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _rebar_cli(*args: str, repo: Path, push: str) -> subprocess.CompletedProcess:
    """Invoke the real `rebar` CLI in a subprocess (the consumer-facing path), with
    REBAR_SYNC_PUSH set so background pushes contend exactly as the bug describes."""
    env = dict(os.environ)
    env["REBAR_ROOT"] = str(repo)
    env["REBAR_SYNC_PUSH"] = push
    rebar_bin = shutil.which("rebar")
    cmd = [rebar_bin, *args] if rebar_bin else [sys.executable, "-m", "rebar", *args]
    return subprocess.run(cmd, cwd=str(repo), env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def repo_with_origin_tickets(tmp_path, monkeypatch):
    """A repo whose tracker has an `origin/tickets` upstream, so `ensure_fresh`
    actually reconverges (it early-returns when there's no remote branch). Yields
    (repo_path, tracker_path, ticket_id)."""
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "root", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    tid = rebar.create_ticket("task", "no-stall target", repo_root=str(repo))
    tracker = repo / ".tickets-tracker"
    _git("push", "-q", "origin", "tickets:tickets", cwd=tracker)
    return repo, tracker, tid


def _clear_sync_throttle(tracker: Path) -> None:
    tracker_abs = os.path.realpath(str(tracker))
    md5_12 = hashlib.md5(tracker_abs.encode()).hexdigest()[:12]
    marker = f"/tmp/.ticket-sync-{md5_12}"
    try:
        os.unlink(marker)
    except OSError:
        pass


def test_ensure_fresh_does_not_stall_on_held_write_lock(repo_with_origin_tickets):
    repo, tracker, tid = repo_with_origin_tickets
    _clear_sync_throttle(tracker)

    acquired = threading.Event()
    release = threading.Event()

    def _hold_lock():
        # Hold the write lock the whole time the read tries to reconverge — exactly
        # what a concurrent background push does during its commit window.
        handle = _lock.acquire(str(tracker), timeout=30, attempts=1)
        acquired.set()
        release.wait(timeout=30)
        handle.release()

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    try:
        assert acquired.wait(timeout=10), "could not pre-acquire the lock"
        t0 = time.monotonic()
        reads.ensure_fresh(str(tracker))  # the read-path freshness step
        elapsed = time.monotonic() - t0
        # Before the fix this blocked ~15s on the held lock; a read must not stall.
        assert elapsed < 8.0, f"ensure_fresh stalled on the held lock: {elapsed:.1f}s"
    finally:
        release.set()
        holder.join(timeout=30)

    # And the record reads back complete (a read is always consistent locally).
    state = reads.show_state(tid, str(tracker))
    assert state["title"] == "no-stall target"


def test_cli_show_complete_or_erroring_under_write_burst(repo_with_origin_tickets, monkeypatch):
    """AC regression (slim-fetch-ledge): the burst-of-writes-then-`show` pattern via
    the real CLI under REBAR_SYNC_PUSH=always (background pushes contend) — every
    `rebar show` must be COMPLETE-or-ERRORING: never empty stdout with a zero exit.
    Exercises the consumer-facing path the bug broke (pipe `show` into a parser)."""
    repo, tracker, tid = repo_with_origin_tickets
    monkeypatch.delenv("REBAR_SYNC_PUSH", raising=False)  # let the CLI helper set =always

    # A few more tickets so the burst is real.
    ids = [tid] + [
        rebar.create_ticket("task", f"burst target {i}", repo_root=str(repo)) for i in range(3)
    ]

    for round_no in range(6):
        # Burst of writes (each spawns a background push to origin under =always).
        for i, t in enumerate(ids):
            _rebar_cli(
                "edit", t, "--description", f"round {round_no} edit {i}", repo=repo, push="always"
            )
        # Immediately read each back through the CLI — the contention window.
        for t in ids:
            cp = _rebar_cli("show", t, repo=repo, push="always")
            empty_and_ok = cp.stdout.strip() == "" and cp.returncode == 0
            assert not empty_and_ok, (
                f"`rebar show {t}` returned EMPTY stdout with exit 0 "
                f"(round {round_no}); stderr={cp.stderr!r}"
            )
            if cp.returncode == 0:
                # A success exit must carry the complete record (parseable JSON).
                doc = json.loads(cp.stdout)
                assert doc.get("ticket_id"), f"incomplete show payload: {cp.stdout[:200]!r}"
