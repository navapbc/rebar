"""Local git lock contention on the tickets store is absorbed automatically (bug 9305).

Operator-ratified contract (2026-08-04, recorded on the ticket): concurrent rebar sessions
contending on LOCAL git locks (``index.lock`` / ref locks) must NOT surface errors to the
agent running the command. The store's git execution seam (``rebar._store.gitutil``) provides

  * a per-store advisory lock serializing rebar's own index-mutating git ops, plus
  * bounded, JITTERED retry/backoff on git's lock-conflict signatures,

so a transient conflict self-heals silently (at most a debug/warning log) and only a
genuinely STUCK lock — the bounded budget (tens of seconds by default) exhausted — surfaces
one actionable error naming the lock file with holder guidance.

These tests use REAL git repos in tmp dirs and a real competing lock holder — behavioral,
not change-detectors. The budget constants are shrunk via monkeypatch where a test would
otherwise wait out the full default budget; the default budget itself is pinned by a
separate contract test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from rebar._store import gitutil


def _fresh_repo(tmp_path: Path, name: str) -> str:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "seed"], cwd=repo, check=True)
    return str(repo)


def _index_lock_path(repo: str) -> Path:
    p = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--git-path", "index.lock"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(p) if os.path.isabs(p) else Path(repo) / p


# ---------------------------------------------------------------------------
# (a) transient contention: a lock held briefly by ANOTHER PROCESS self-heals
# ---------------------------------------------------------------------------


def test_transient_index_lock_from_competing_process_self_heals(tmp_path: Path) -> None:
    """An index-mutating store git op that hits an ``index.lock`` held ~3s by a competing
    process succeeds with NO error surfaced. 3s exceeds the pre-9305 total retry budget
    (~2s), so this pins the raised tens-of-seconds budget, not the old one."""
    repo = _fresh_repo(tmp_path, "transient")
    (Path(repo) / "f.txt").write_text("x\n")
    lock = _index_lock_path(repo)

    # A competing PROCESS creates the lock, holds it ~3s, then releases it — the shape of
    # a live peer mid-write. (The young lock must NOT be reclaimed out from under it; it
    # must be ridden out and the op retried after the holder releases.)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time, os\n"
            "open(sys.argv[1], 'w').close()\n"
            "time.sleep(3.0)\n"
            "os.remove(sys.argv[1])\n",
            str(lock),
        ]
    )
    try:
        time.sleep(0.3)  # let the holder plant the lock
        assert lock.exists()
        result = gitutil.run_git_write(repo, "add", "--", "f.txt")
    finally:
        holder.wait(timeout=30)

    assert result.returncode == 0, (
        f"transient contention must self-heal, got rc={result.returncode}: {result.stderr}"
    )


def test_transient_ref_lock_signature_is_retried(tmp_path: Path) -> None:
    """git's ref-lock conflict signature (``cannot lock ref``) is retried like index.lock —
    a conflict that clears after two attempts self-heals with no error surfaced."""
    repo = _fresh_repo(tmp_path, "reflock")
    calls = {"n": 0}

    def run_once() -> subprocess.CompletedProcess:
        calls["n"] += 1
        if calls["n"] < 3:
            return subprocess.CompletedProcess(
                ["git"],
                128,
                "",
                "error: cannot lock ref 'refs/heads/tickets': Unable to create "
                f"'{repo}/.git/refs/heads/tickets.lock': File exists.",
            )
        return subprocess.CompletedProcess(["git"], 0, "", "")

    result = gitutil._with_index_lock_retry(repo, run_once)
    assert result.returncode == 0
    assert calls["n"] == 3, "the ref-lock signature must be retried until it clears"


# ---------------------------------------------------------------------------
# (b) a genuinely stuck lock: ONE actionable error after the bounded budget
# ---------------------------------------------------------------------------


def test_stuck_lock_surfaces_one_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permanently held lock exhausts the bounded budget and surfaces ONE actionable
    error naming the contended lock file and the remedy (holder guidance) — not raw git
    stderr alone. Budget shrunk for test speed; the default budget is pinned separately."""
    repo = _fresh_repo(tmp_path, "stuck")
    (Path(repo) / "f.txt").write_text("x\n")
    lock = _index_lock_path(repo)
    lock.write_text("")  # never released: a genuinely stuck lock

    monkeypatch.setattr(gitutil, "_INDEX_LOCK_ATTEMPTS", 3)
    monkeypatch.setattr(gitutil, "_INDEX_LOCK_BACKOFF_S", 0.05)

    result = gitutil.run_git_write(repo, "add", "--", "f.txt")

    assert result.returncode != 0
    stderr = result.stderr or ""
    assert "index.lock" in stderr, "the error must name the contended lock file"
    assert "rebar:" in stderr, "rebar must add its own actionable guidance"
    assert "remove" in stderr.lower(), "the error must state the remedy (remove a stale lock)"
    assert "git process" in stderr.lower(), "the error must give holder guidance"
    assert lock.exists(), "a young lock outside the write lock must never be reclaimed"


def test_default_retry_budget_is_tens_of_seconds() -> None:
    """The DEFAULT bounded retry budget is on the order of tens of seconds (the
    operator-ratified default), not the pre-9305 ~2s and not unbounded."""
    budget = gitutil._lock_retry_budget_s()
    assert 10.0 <= budget <= 60.0, f"default lock-retry budget {budget}s not in [10s, 60s]"


def test_nonlock_failure_still_surfaces_immediately(tmp_path: Path) -> None:
    """A NON-lock git failure is returned on the first attempt — the contention policy
    must not mask or delay a genuine error."""
    calls = {"n": 0}

    def run_once() -> subprocess.CompletedProcess:
        calls["n"] += 1
        return subprocess.CompletedProcess(["git"], 1, "", "fatal: pathspec 'x' did not match")

    result = gitutil._with_index_lock_retry(str(tmp_path), run_once)
    assert result.returncode == 1
    assert calls["n"] == 1
    assert "rebar:" not in (result.stderr or ""), "no lock guidance on a non-lock failure"


# ---------------------------------------------------------------------------
# (c) uncontended ops: no added latency (no sleep on the happy path)
# ---------------------------------------------------------------------------


def test_uncontended_write_never_sleeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An uncontended index-mutating op through the seam performs ZERO sleeps: the
    advisory lock's fast path is non-blocking and no retry backoff fires."""
    repo = _fresh_repo(tmp_path, "fast")
    (Path(repo) / "f.txt").write_text("x\n")
    sleeps: list[float] = []
    real_sleep = time.sleep

    def counting_sleep(s: float) -> None:
        sleeps.append(s)
        real_sleep(s)

    monkeypatch.setattr(gitutil.time, "sleep", counting_sleep)
    result = gitutil.run_git_write(repo, "add", "--", "f.txt")
    assert result.returncode == 0
    assert sleeps == [], f"uncontended happy path must not sleep, slept: {sleeps}"


# ---------------------------------------------------------------------------
# per-store advisory lock: serializes rebar's own git ops across processes
# ---------------------------------------------------------------------------


def test_advisory_lock_serializes_against_competing_holder(tmp_path: Path) -> None:
    """While a competing PROCESS holds the per-store advisory git-op lock, a write through
    the seam waits (bounded) and then succeeds once the holder releases — the write is
    serialized behind the peer instead of colliding with it."""
    repo = _fresh_repo(tmp_path, "advisory")
    (Path(repo) / "f.txt").write_text("x\n")

    # A competing process takes the advisory lock for ~1.5s.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time, fcntl, os\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "from rebar._store import gitutil\n"
            "fd = os.open(gitutil._store_git_lock_path(sys.argv[1]), os.O_CREAT | os.O_RDWR)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(1.5)\n",
            repo,
            str(Path(__file__).resolve().parents[3] / "src"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        t0 = time.monotonic()
        result = gitutil.run_git_write(repo, "add", "--", "f.txt")
        waited = time.monotonic() - t0
    finally:
        holder.wait(timeout=30)

    assert result.returncode == 0, f"write must succeed after the peer releases: {result.stderr}"
    assert waited >= 0.5, "the write should have been serialized behind the advisory holder"


def test_concurrent_processes_through_seam_both_succeed(tmp_path: Path) -> None:
    """Two processes committing through the seam against the SAME repo both succeed —
    contention among rebar's own writers is absorbed, never surfaced."""
    repo = _fresh_repo(tmp_path, "both")
    src = str(Path(__file__).resolve().parents[3] / "src")

    def worker(name: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys\n"
                "sys.path.insert(0, sys.argv[3])\n"
                "from rebar._store import gitutil\n"
                "repo, name = sys.argv[1], sys.argv[2]\n"
                "open(f'{repo}/{name}.txt', 'w').write(name)\n"
                "r = gitutil.run_git_write(repo, 'add', '--', f'{name}.txt')\n"
                "assert r.returncode == 0, r.stderr\n"
                "r = gitutil.run_git_write(repo, 'commit', '-q', '--no-verify', '-m', name)\n"
                "assert r.returncode == 0, r.stderr\n",
                repo,
                name,
                src,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

    results: dict[str, subprocess.CompletedProcess] = {}
    threads = [
        threading.Thread(target=lambda n=n: results.__setitem__(n, worker(n)))
        for n in ("alpha", "beta")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for name, cp in results.items():
        assert cp.returncode == 0, f"writer {name} failed: {cp.stderr}\n{cp.stdout}"
    log = subprocess.run(
        ["git", "-C", repo, "log", "--oneline"], capture_output=True, text=True, check=True
    ).stdout
    assert "alpha" in log and "beta" in log, f"both commits must land, log:\n{log}"


def test_backoff_is_jittered() -> None:
    """The retry backoff is jittered (git lockfile.c's uniform 0.75x-1.25x shape) so
    competing processes de-synchronize instead of thundering in lockstep."""
    base = 1.0
    samples = {gitutil._jitter(base) for _ in range(64)}
    assert all(0.75 <= s <= 1.25 for s in samples), f"jitter out of range: {sorted(samples)[:5]}"
    assert len(samples) > 1, "jitter must actually vary"
