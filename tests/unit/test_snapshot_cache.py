"""S2 — content-addressed snapshot cache: single-flight + reader-safety (epic raze-vet-ditch).

Covers ``rebar._snapshot.cache``: in-process + cross-process single-flight, POSIX
delete-on-last-close reader safety, ENOENT/read-error self-heal, touch-on-read ``mtime``
recency (never ``atime`` / no PID lease), and atomic byte accounting on populate.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rebar._snapshot import cache
from rebar._snapshot import repo_snapshot as rs


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def store(monkeypatch, tmp_path):
    base = tmp_path / "gate-tmpdir"
    base.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(base))
    # The real store root is <base>/rebar-gate-snapshots (store_root() appends it).
    return rs.store_root()


@pytest.fixture
def repo(tmp_path):
    r = _init_repo(tmp_path / "repo")
    return r


# --------------------------------------------------------------------------------------
# AC1 — in-process single-flight: concurrent same-SHA => exactly ONE materialization
# --------------------------------------------------------------------------------------
def test_in_process_single_flight(store, repo, monkeypatch):
    sha = _commit(repo, "f.txt", "v1\n")
    builds: list[str] = []
    real = rs._materialize_tree

    def counting(repo_root, s, dest_tmp):
        builds.append(s)
        time.sleep(0.05)  # widen the race window
        return real(repo_root, s, dest_tmp)

    monkeypatch.setattr(rs, "_materialize_tree", counting)

    def go(_):
        h = cache.acquire(sha, repo_root=str(repo), fetch=False)
        return (h.path / "f.txt").read_text()

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(go, range(4)))

    assert results == ["v1\n"] * 4
    assert len(builds) == 1, f"expected exactly one materialization, got {len(builds)}"


def test_in_process_lock_collapses_without_flock(store, repo, monkeypatch):
    """Pin the IN-PROCESS single-flight specifically: with the cross-process flock
    disabled, the per-SHA threading lock alone must still collapse to one build."""
    sha = _commit(repo, "f.txt", "v1\n")
    monkeypatch.setattr(cache, "_interprocess_lock", lambda _p: contextlib.nullcontext())
    builds: list[str] = []
    real = rs._materialize_tree

    def counting(repo_root, s, dest_tmp):
        builds.append(s)
        time.sleep(0.05)
        return real(repo_root, s, dest_tmp)

    monkeypatch.setattr(rs, "_materialize_tree", counting)
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(lambda _: cache.acquire(sha, repo_root=str(repo), fetch=False), range(4)))
    assert len(builds) == 1


# --------------------------------------------------------------------------------------
# AC2 — cross-process single-flight: racing PROCESSES collapse to one materialization
# --------------------------------------------------------------------------------------
_CHILD = """
import os, sys, time
from rebar._snapshot import cache
from rebar._snapshot import repo_snapshot as rs

sha, repo, marker = sys.argv[1], sys.argv[2], sys.argv[3]
real = rs._materialize_tree
def counting(repo_root, s, dest_tmp):
    # Record one build + slow it so peers pile up on the flock.
    with open(marker, "a") as fh:
        fh.write("build\\n")
    time.sleep(0.4)
    return real(repo_root, s, dest_tmp)
rs._materialize_tree = counting
cache.acquire(sha, repo_root=repo, fetch=False)
"""


def test_cross_process_single_flight(store, repo, tmp_path):
    sha = _commit(repo, "f.txt", "v1\n")
    marker = tmp_path / "builds.log"
    # store == <base>/rebar-gate-snapshots; children re-append, so pass the BASE.
    env = {**os.environ, "REBAR_GATE_TMPDIR": str(store.parent)}
    procs = [
        subprocess.Popen([sys.executable, "-c", _CHILD, sha, str(repo), str(marker)], env=env)
        for _ in range(3)
    ]
    for p in procs:
        assert p.wait(timeout=30) == 0
    builds = marker.read_text().count("build") if marker.exists() else 0
    assert builds == 1, f"cross-process flock should collapse to ONE build, got {builds}"
    assert (rs.entry_path(sha, store) / "f.txt").read_text() == "v1\n"


# --------------------------------------------------------------------------------------
# AC3 — delete-on-last-close: an open reader survives eviction; a new lookup re-heals
# --------------------------------------------------------------------------------------
def test_open_reader_survives_eviction_then_relookup_misses(store, repo):
    sha = _commit(repo, "f.txt", "content\n")
    h = cache.acquire(sha, repo_root=str(repo), fetch=False)
    dest = rs.entry_path(sha, store)

    # Reader opens the file up front (holds an fd).
    fh = cache.open_in_snapshot(h, "f.txt", "rb")
    try:
        # Janitor-style eviction: rename away (atomic disappearance) THEN rmtree — never
        # an in-place delete of a live entry.
        trash = store / "trash-x"
        os.rename(dest, trash)
        shutil.rmtree(trash)
        # The open fd still reads the evicted content (POSIX delete-on-last-close).
        assert fh.read() == b"content\n"
    finally:
        fh.close()

    # The entry is gone from the canonical path; a new lookup cleanly misses + re-heals.
    assert not dest.exists()
    h2 = cache.acquire(sha, repo_root=str(repo), fetch=False)
    assert (h2.path / "f.txt").read_text() == "content\n"


def test_open_in_snapshot_raises_cache_miss_on_enoent(store, repo):
    sha = _commit(repo, "f.txt", "x\n")
    h = cache.acquire(sha, repo_root=str(repo), fetch=False)
    with pytest.raises(cache.CacheMiss):
        cache.open_in_snapshot(h, "does-not-exist.txt", "rb")


# --------------------------------------------------------------------------------------
# AC4 — ENOENT/read-error on an entry is a miss, self-healed by re-materialization
# --------------------------------------------------------------------------------------
def test_deleted_entry_self_heals(store, repo):
    sha = _commit(repo, "f.txt", "v\n")
    cache.acquire(sha, repo_root=str(repo), fetch=False)
    dest = rs.entry_path(sha, store)
    shutil.rmtree(dest)
    assert not dest.exists()
    h = cache.acquire(sha, repo_root=str(repo), fetch=False)
    assert dest.exists()
    assert (h.path / "f.txt").read_text() == "v\n"


# --------------------------------------------------------------------------------------
# AC5 — recency by touch-on-read mtime (not atime); no PID/heartbeat lease
# --------------------------------------------------------------------------------------
def test_cache_hit_touches_mtime(store, repo):
    sha = _commit(repo, "f.txt", "v\n")
    cache.acquire(sha, repo_root=str(repo), fetch=False)
    dest = rs.entry_path(sha, store)
    old = cache.entry_mtime(dest)
    # Force the recorded mtime into the past, then a cache hit must bump it forward.
    past = old - 1000
    os.utime(dest, (past, past))
    assert cache.entry_mtime(dest) == pytest.approx(past, abs=1)
    cache.acquire(sha, repo_root=str(repo), fetch=False)  # hit
    assert cache.entry_mtime(dest) > past + 100


def test_no_pid_or_heartbeat_lease_in_module():
    # No PID-based lease: the recency/liveness model is touch-on-read mtime + POSIX
    # delete-on-last-close, never a process-id heartbeat. Assert no getpid() CALL exists
    # (the prose may mention the rejected lease; an actual call would be the smell).
    src = Path(cache.__file__).read_text()
    assert "getpid(" not in src and "kill(" not in src


# --------------------------------------------------------------------------------------
# AC6 — byte total increments atomically on populate
# --------------------------------------------------------------------------------------
def test_byte_total_increments_on_populate(store, repo):
    assert cache.byte_total(store) == 0
    sha1 = _commit(repo, "a.txt", "x" * 100)
    cache.acquire(sha1, repo_root=str(repo), fetch=False)
    after1 = cache.byte_total(store)
    assert after1 == cache.entry_size(rs.entry_path(sha1, store))
    assert after1 > 0

    sha2 = _commit(repo, "b.txt", "y" * 500)
    cache.acquire(sha2, repo_root=str(repo), fetch=False)
    after2 = cache.byte_total(store)
    # Second populate adds the second entry's bytes (monotone increment per populate).
    assert after2 == after1 + cache.entry_size(rs.entry_path(sha2, store))


def test_byte_total_not_double_counted_on_cache_hit(store, repo):
    sha = _commit(repo, "f.txt", "v\n")
    cache.acquire(sha, repo_root=str(repo), fetch=False)
    once = cache.byte_total(store)
    cache.acquire(sha, repo_root=str(repo), fetch=False)  # hit, must NOT re-add
    assert cache.byte_total(store) == once


def test_add_bytes_atomic_floor_at_zero(store):
    cache.add_bytes(50, store)
    assert cache.byte_total(store) == 50
    cache.add_bytes(-200, store)  # over-decrement clamps at 0 (janitor reconciles)
    assert cache.byte_total(store) == 0


def test_add_bytes_atomic_under_concurrency(store):
    """AC6 'atomically': N concurrent +1s must not lose an update (flock-serialized
    read-modify-write, no TOCTOU drift)."""
    n = 50
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: cache.add_bytes(1, store), range(n)))
    assert cache.byte_total(store) == n
