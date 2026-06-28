"""S2b — snapshot-cache janitor: reclamation under disk pressure (epic raze-vet-ditch).

Covers ``rebar._snapshot.janitor``: free-space-watermark LRU eviction (mtime, grace
window, off the hot path), max-age cold-trim, startup sweep + byte reconcile, byte-total
consistency under concurrent populate-vs-evict, trash-straggler re-drain, rename-to-trash
(never in-place delete), the exclusive gc/lock interlock, corrupt-entry self-heal, the
configurable tunables, and the architecture ADR.
"""

from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rebar._snapshot import cache, janitor
from rebar._snapshot import repo_snapshot as rs

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


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
    return rs.store_root()


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path / "repo")


def _populate(repo: Path, store: Path, name: str, body: str, *, mtime: float | None = None):
    sha = _commit(repo, name, body)
    cache.acquire(sha, repo_root=str(repo), fetch=False)
    entry = rs.entry_path(sha, store)
    if mtime is not None:
        os.utime(entry, (mtime, mtime))
    return sha, entry


# --------------------------------------------------------------------------------------
# AC1 — watermark LRU eviction by mtime, skipping the grace window
# --------------------------------------------------------------------------------------
def test_watermark_evicts_lru_skips_grace(store, repo):
    now = time.time()
    cfg = janitor.JanitorConfig(
        free_watermark_bytes=2 * 1024**3, grace_seconds=100, max_age_seconds=10**9
    )
    sha_old, old = _populate(repo, store, "a.txt", "x" * 50, mtime=now - 5000)
    sha_recent, recent = _populate(repo, store, "b.txt", "y" * 50, mtime=now - 1)

    # Inject disk pressure (free=0 < watermark): the LRU/old entry is evicted, the
    # recently-touched one is protected by the grace window.
    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=0)
    assert not old.exists()
    assert recent.exists()
    assert res.evicted == 1
    assert res.skipped_grace >= 1


def test_evicts_stale_pinned_ticket_store_entry(store):
    """The pinned ticket-store entries (``tickets-<sha>``, PR #67) must be GC'd under
    pressure like code-snapshot ``<sha>`` entries — else they leak unboundedly as the
    tickets branch changes. The ``tickets-`` prefix is the only difference from a code
    entry, and it must not hide the entry from the janitor's eviction + byte accounting."""
    now = time.time()
    cfg = janitor.JanitorConfig(
        free_watermark_bytes=2 * 1024**3, grace_seconds=100, max_age_seconds=10**9
    )
    entry = store / ("tickets-" + "a" * 40)
    (entry / ".tickets-tracker").mkdir(parents=True)
    (entry / ".tickets-tracker" / "events.jsonl").write_text("x" * 50)
    old = now - 5000
    os.utime(entry, (old, old))

    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=0)
    # Eviction half:
    assert not entry.exists(), "stale tickets-<sha> entry should be evicted under pressure"
    assert res.evicted >= 1
    # Accounting half: the entry's bytes are measured and flow through the byte total via
    # _evict -> _cache.add_bytes(-size). This only happens because _is_entry now recognizes
    # the tickets- prefix — without the fix the entry is invisible to _entries(), so it is
    # neither evicted NOR counted and reclaimed_bytes stays 0.
    assert res.reclaimed_bytes > 0, "the tickets-<sha> entry's bytes must be reclaimed/accounted"


def test_no_eviction_when_space_ample_and_not_cold(store, repo):
    now = time.time()
    cfg = janitor.JanitorConfig(free_watermark_bytes=1, grace_seconds=100, max_age_seconds=10**9)
    _sha, entry = _populate(repo, store, "a.txt", "x" * 50, mtime=now - 5000)
    # free far above watermark and entry not max-age cold => keep it.
    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=10**12)
    assert entry.exists()
    assert res.evicted == 0


# --------------------------------------------------------------------------------------
# AC2 — secondary max-age cold-trim (independent of disk pressure)
# --------------------------------------------------------------------------------------
def test_max_age_cold_trim(store, repo):
    now = time.time()
    cfg = janitor.JanitorConfig(free_watermark_bytes=1, grace_seconds=10, max_age_seconds=50)
    _sha_cold, cold = _populate(repo, store, "a.txt", "x" * 50, mtime=now - 1000)
    _sha_warm, warm = _populate(repo, store, "b.txt", "y" * 50, mtime=now - 5)
    # Ample free space, but the cold entry (age 1000 > max_age 50) is trimmed anyway.
    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=10**12)
    assert not cold.exists()
    assert warm.exists()
    assert res.evicted == 1


# --------------------------------------------------------------------------------------
# AC3 — startup sweep clears tmp/* + trash/* and reconciles byte total via a full walk
# --------------------------------------------------------------------------------------
def test_startup_sweep_clears_and_reconciles(store, repo):
    sha, entry = _populate(repo, store, "a.txt", "x" * 123)
    # Plant crash debris + corrupt the byte total.
    (store / "tmp" / "leftover").mkdir(parents=True)
    (store / "trash" / "straggler").mkdir(parents=True)
    cache.add_bytes(999999, store)  # bogus inflation

    total = janitor.startup_sweep(store)
    assert not (store / "tmp" / "leftover").exists()
    assert not (store / "trash" / "straggler").exists()
    walk = cache.entry_size(entry)
    assert total == walk
    assert cache.byte_total(store) == walk


# --------------------------------------------------------------------------------------
# AC4 — byte total stays consistent under concurrent populate-vs-evict (no TOCTOU drift)
# --------------------------------------------------------------------------------------
def test_byte_total_consistent_under_concurrent_populate_evict(store, repo):
    cfg = janitor.JanitorConfig(free_watermark_bytes=2 * 1024**3, grace_seconds=0)
    shas = [_commit(repo, f"f{i}.txt", str(i) * (50 + i)) for i in range(6)]

    def populate(_):
        for s in shas:
            cache.acquire(s, repo_root=str(repo), fetch=False)

    def evict(_):
        for _ in range(5):
            janitor.run_gc(store, config=cfg, free_bytes=0)  # aggressive eviction

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(populate, i) for i in range(4)] + [ex.submit(evict, i) for i in range(2)]
        for f in futs:
            f.result()

    # Settle: re-acquire all, then the running total must equal a fresh full walk.
    for s in shas:
        cache.acquire(s, repo_root=str(repo), fetch=False)
    walk = sum(cache.entry_size(e) for e in store.iterdir() if janitor._is_entry(e))
    assert cache.byte_total(store) == walk


# --------------------------------------------------------------------------------------
# AC5 — interrupted rename-to-trash straggler re-drained on a later pass
# --------------------------------------------------------------------------------------
def test_trash_straggler_redrained(store, repo):
    _populate(repo, store, "a.txt", "x" * 50)
    straggler = store / "trash" / "interrupted-rmtree"
    straggler.mkdir(parents=True)
    (straggler / "junk").write_text("half-deleted\n")
    # A later janitor pass drains it (run_gc drains trash up front).
    janitor.run_gc(store, config=janitor.JanitorConfig(free_watermark_bytes=1), free_bytes=10**12)
    assert not straggler.exists()


# --------------------------------------------------------------------------------------
# AC6 — eviction is rename-to-trash THEN rmtree; an open reader survives (no in-place del)
# --------------------------------------------------------------------------------------
def test_eviction_rename_to_trash_open_reader_survives(store, repo):
    now = time.time()
    sha, entry = _populate(repo, store, "f.txt", "content\n", mtime=now - 5000)
    fh = open(entry / "f.txt", "rb")
    try:
        cfg = janitor.JanitorConfig(
            free_watermark_bytes=2 * 1024**3, grace_seconds=10, max_age_seconds=10**9
        )
        janitor.run_gc(store, config=cfg, now=now, free_bytes=0)
        assert not entry.exists()  # gone from the canonical path
        assert fh.read() == b"content\n"  # but the held fd still reads it
    finally:
        fh.close()


# --------------------------------------------------------------------------------------
# AC7 — a second GC pass cannot run concurrently (exclusive gc/lock)
# --------------------------------------------------------------------------------------
@pytest.mark.skipif(fcntl is None, reason="POSIX flock required")
def test_gc_lock_is_exclusive(store, repo):
    _populate(repo, store, "a.txt", "x")
    lock_path = store / "gc" / "lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        res = janitor.run_gc(store, free_bytes=0)
        assert res.skipped == "locked"
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)


# --------------------------------------------------------------------------------------
# AC8 — corrupt/truncated entry detected and self-healed by re-materialization
# --------------------------------------------------------------------------------------
def test_corrupt_entry_detected_and_self_heals(store, repo):
    sha, entry = _populate(repo, store, "f.txt", "good\n")
    assert janitor.reverify_entry(sha, store) is False  # TOFU baseline
    # Corrupt the materialized content (bit-rot / truncation).
    (entry / "f.txt").write_text("TAMPERED")
    assert janitor.reverify_entry(sha, store) is True  # detected + discarded
    assert not entry.exists()
    # Self-heal: the next acquire re-materializes faithful content.
    h = cache.acquire(sha, repo_root=str(repo), fetch=False)
    assert (h.path / "f.txt").read_text() == "good\n"


def test_reverify_passes_when_unchanged(store, repo):
    sha, _entry = _populate(repo, store, "f.txt", "stable\n")
    assert janitor.reverify_entry(sha, store) is False
    assert janitor.reverify_entry(sha, store) is False  # still consistent


# --------------------------------------------------------------------------------------
# AC9 — tunables configurable with documented defaults
# --------------------------------------------------------------------------------------
def test_janitor_config_defaults():
    cfg = janitor.JanitorConfig()
    assert cfg.free_watermark_bytes == janitor.DEFAULT_FREE_WATERMARK_BYTES
    assert cfg.grace_seconds == janitor.DEFAULT_GRACE_SECONDS
    assert cfg.max_age_seconds == janitor.DEFAULT_MAX_AGE_SECONDS
    assert cfg.reverify_seconds == janitor.DEFAULT_REVERIFY_SECONDS
    assert cfg.interval_seconds == janitor.DEFAULT_INTERVAL_SECONDS


def test_janitor_config_env_overrides(monkeypatch):
    monkeypatch.setenv("REBAR_GATE_FREE_WATERMARK_BYTES", "123")
    monkeypatch.setenv("REBAR_GATE_GRACE_SECONDS", "7")
    monkeypatch.setenv("REBAR_GATE_MAX_AGE_SECONDS", "88")
    monkeypatch.setenv("REBAR_GATE_REVERIFY_SECONDS", "9")
    monkeypatch.setenv("REBAR_GATE_JANITOR_INTERVAL_SECONDS", "42")
    cfg = janitor.JanitorConfig.from_env()
    assert cfg.free_watermark_bytes == 123
    assert cfg.grace_seconds == 7
    assert cfg.max_age_seconds == 88
    assert cfg.reverify_seconds == 9
    assert cfg.interval_seconds == 42


def test_janitor_config_reads_snapshot_toml_table(tmp_path, monkeypatch):
    # [snapshot] in the project config resolves (env > file > default); env unset here.
    monkeypatch.delenv("REBAR_GATE_GRACE_SECONDS", raising=False)
    repo = _init_repo(tmp_path / "repo")
    (repo / "rebar.toml").write_text("[snapshot]\ngrace_seconds = 33\n")
    cfg = janitor.JanitorConfig.from_env(repo_root=str(repo))
    assert cfg.grace_seconds == 33


def test_reverify_period_skips_recently_verified(store, repo, monkeypatch):
    # With a long reverify period, an entry verified this pass is not re-walked next pass.
    sha, entry = _populate(repo, store, "f.txt", "v\n")
    cfg = janitor.JanitorConfig(
        free_watermark_bytes=1, grace_seconds=1, max_age_seconds=10**9, reverify_seconds=10**6
    )
    r1 = janitor.run_gc(store, config=cfg, free_bytes=10**12)
    assert r1.reverified == 1  # first pass baselines it
    r2 = janitor.run_gc(store, config=cfg, free_bytes=10**12)
    assert r2.reverified == 0  # within the period -> skipped


# --------------------------------------------------------------------------------------
# AC10 — ADR records the architecture + the rejected PID+heartbeat lease
# --------------------------------------------------------------------------------------
def test_adr_records_architecture_and_rejected_pid_lease():
    adr = (
        Path(__file__).resolve().parents[2] / "docs" / "adr" / "0005-snapshot-cache-architecture.md"
    )
    assert adr.is_file()
    text = adr.read_text().lower()
    assert "delete-on-last-close" in text
    assert "flock" in text and "gc" in text
    assert "pid" in text and "heartbeat" in text and "reject" in text


# --------------------------------------------------------------------------------------
# Background driver runs off the hot path (single pass invoked on an interval)
# --------------------------------------------------------------------------------------
def test_background_janitor_runs_and_stops(store, repo, monkeypatch):
    _populate(repo, store, "a.txt", "x" * 50)
    calls: list[int] = []
    monkeypatch.setattr(janitor, "run_gc", lambda **kw: calls.append(1))
    monkeypatch.setattr(janitor, "startup_sweep", lambda *a, **k: 0)
    cfg = janitor.JanitorConfig(interval_seconds=1)
    thread, stop = janitor.start_background_janitor(config=cfg)
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(calls) >= 1
