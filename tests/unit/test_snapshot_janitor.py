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
    _sha_old, old = _populate(repo, store, "a.txt", "x" * 50, mtime=now - 5000)
    _sha_recent, recent = _populate(repo, store, "b.txt", "y" * 50, mtime=now - 1)

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
    _sha, entry = _populate(repo, store, "a.txt", "x" * 123)
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

    # Settle: re-acquire all, then the running total must equal a fresh full walk. The
    # entries are adjacent SHAs, so they hardlink-share unchanged blobs (task 5b25) — the
    # authoritative walk therefore charges each distinct inode once (a per-entry
    # ``entry_size`` sum would double-count every shared blob).
    for s in shas:
        cache.acquire(s, repo_root=str(repo), fetch=False)
    assert cache.byte_total(store) == _real_store_bytes(store)


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
    _sha, entry = _populate(repo, store, "f.txt", "content\n", mtime=now - 5000)
    with open(entry / "f.txt", "rb") as fh:
        cfg = janitor.JanitorConfig(
            free_watermark_bytes=2 * 1024**3, grace_seconds=10, max_age_seconds=10**9
        )
        janitor.run_gc(store, config=cfg, now=now, free_bytes=0)
        assert not entry.exists()  # gone from the canonical path
        assert fh.read() == b"content\n"  # but the held fd still reads it


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
    _sha, _entry = _populate(repo, store, "f.txt", "v\n")
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


# --------------------------------------------------------------------------------------
# Bug 3a52 (masonic-abeyant-stagbeetle) — the free-space watermark must scale with the
# VOLUME, so it can never sit ABOVE the operator's disk-pressure alarm floor.
#
# On the review-bot host the root volume is 30 GiB and `rebar-root-disk-pressure`
# (infra/terraform/monitoring_autodeploy.tf) alarms above 85% used = 4.5 GiB free. The
# absolute 2-GiB DEFAULT_FREE_WATERMARK_BYTES is only crossed at 93.3% used, so the
# janitor — the only thing that bounds /tmp/rebar-gate-snapshots — provably cannot engage
# until after the alarm has already breached. These cases pin the ordering the runbook
# states (infra/runbooks/review-bot-ops.md: reclaim, then alarm as the backstop).
# --------------------------------------------------------------------------------------
_GIB = 1024**3
_ROOT_TOTAL = 30 * _GIB  # the review-bot host's root volume
_ALARM_FLOOR_FREE = int(_ROOT_TOTAL * 0.15)  # 85% used — the CloudWatch threshold
_OBSERVED_PEAK_FREE = int(_ROOT_TOTAL * 0.10)  # 90% used — reached on 2026-08-17


@pytest.fixture
def volume_30gib(monkeypatch):
    """Make the janitor see a 30-GiB volume, matching the review-bot host's root disk."""
    import shutil as _shutil

    real = _shutil.disk_usage

    def fake(path):
        usage = real(path)
        return type(usage)(_ROOT_TOTAL, _ROOT_TOTAL - usage.free, usage.free)

    monkeypatch.setattr(janitor.shutil, "disk_usage", fake)
    return _ROOT_TOTAL


def _sparse_entry(store: Path, name: str, size: int, mtime: float) -> Path:
    """An entry whose accounted size is ``size`` without writing ``size`` real bytes
    (``cache.entry_size`` sums ``st_size``, so a sparse file is measured at full size)."""
    entry = store / name
    entry.mkdir(parents=True, exist_ok=True)
    with open(entry / "blob", "wb") as fh:
        fh.truncate(size)
    os.utime(entry, (mtime, mtime))
    return entry


def test_watermark_evicts_at_the_alarm_floor_on_a_30gib_volume(store, repo, volume_30gib):
    """At 85% used — the exact point the operator's alarm fires — a cold entry MUST be
    reclaimed. Today the 2-GiB absolute watermark leaves 4.5 GiB of free space looking
    healthy, so the pass evicts nothing and the disk keeps climbing."""
    now = time.time()
    cfg = janitor.JanitorConfig(free_watermark_pct=20, grace_seconds=100, max_age_seconds=10**9)
    _sha, entry = _populate(repo, store, "a.txt", "x" * 50, mtime=now - 5000)

    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=_ALARM_FLOOR_FREE)

    assert res.evicted >= 1, (
        "at 85% used on a 30-GiB volume the janitor must reclaim BEFORE the "
        "rebar-root-disk-pressure alarm fires, not after"
    )
    assert not entry.exists()


def test_watermark_evicts_at_the_observed_incident_peak(store, repo, volume_30gib):
    """90% used is the peak the 2026-08-17 incident actually reached. The janitor ran
    every 300s throughout and evicted nothing."""
    now = time.time()
    cfg = janitor.JanitorConfig(free_watermark_pct=20, grace_seconds=100, max_age_seconds=10**9)
    _sha, entry = _populate(repo, store, "a.txt", "x" * 50, mtime=now - 5000)

    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=_OBSERVED_PEAK_FREE)

    assert res.evicted >= 1
    assert not entry.exists()


def test_reclaim_continues_past_the_trigger_to_the_hysteresis_target(store, volume_30gib):
    """Hysteresis: a triggered pass must reclaim past the trigger up to the target, so it
    does not sit on the threshold and re-fire on every 300s pass.

    30-GiB volume, ``free_watermark_pct=20`` -> trigger 6.0 GiB free, target 7.5 GiB free
    (trigger + RECLAIM_TARGET_MARGIN_PCT). Starting at 5.5 GiB free with three 1-GiB
    entries: evicting one reaches 6.5 GiB, which already clears the TRIGGER, so a
    trigger-only implementation stops at 1. Reaching the TARGET needs a second eviction.
    """
    now = time.time()
    cfg = janitor.JanitorConfig(free_watermark_pct=20, grace_seconds=100, max_age_seconds=10**9)
    for i in range(3):
        _sparse_entry(store, f"{i:040x}", _GIB, now - 5000 - i)

    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=int(5.5 * _GIB))

    assert res.evicted == 2, (
        "a triggered pass must keep reclaiming until the hysteresis target is reached "
        f"(expected 2 evictions to go 5.5 GiB -> 7.5 GiB free, got {res.evicted})"
    )


def test_volume_relative_watermark_still_honours_the_grace_window(store, volume_30gib):
    """The new trigger must not override the recency protection: an entry touched inside
    grace_seconds stays, even at 90% used."""
    now = time.time()
    cfg = janitor.JanitorConfig(free_watermark_pct=20, grace_seconds=100, max_age_seconds=10**9)
    recent = _sparse_entry(store, "b" * 40, 4096, now - 1)

    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=_OBSERVED_PEAK_FREE)

    assert recent.exists(), "an in-grace entry must survive the volume-relative trigger"
    assert res.skipped_grace >= 1


def test_watermark_pct_zero_preserves_the_absolute_only_behaviour(store, repo, volume_30gib):
    """The relative term is off by default: with free_watermark_pct=0 the trigger is the
    absolute watermark alone, so 90% used on a 30-GiB volume still evicts nothing."""
    now = time.time()
    cfg = janitor.JanitorConfig(free_watermark_pct=0, grace_seconds=100, max_age_seconds=10**9)
    _sha, entry = _populate(repo, store, "a.txt", "x" * 50, mtime=now - 5000)

    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=_OBSERVED_PEAK_FREE)

    assert res.evicted == 0
    assert entry.exists()


def test_free_watermark_pct_tunable_survives_int_resolution(monkeypatch, tmp_path):
    """The knob resolves through the existing int seam. A float-typed knob would be
    truncated to 0 by _snapshot_int and silently disable the whole fix."""
    monkeypatch.setenv("REBAR_GATE_FREE_WATERMARK_PCT", "20")
    cfg = janitor.JanitorConfig.from_env(str(tmp_path))
    assert cfg.free_watermark_pct == 20


def test_free_watermark_pct_defaults_to_off(monkeypatch, tmp_path):
    monkeypatch.delenv("REBAR_GATE_FREE_WATERMARK_PCT", raising=False)
    cfg = janitor.JanitorConfig.from_env(str(tmp_path))
    assert cfg.free_watermark_pct == janitor.DEFAULT_FREE_WATERMARK_PCT == 0


def test_watermark_pct_is_clamped_so_it_cannot_demand_the_whole_volume(store, repo, volume_30gib):
    """An out-of-range percentage must not turn the janitor into a permanent shredder.

    ``free_watermark_pct`` is headroom to KEEP FREE, but "80" reads naturally as "reclaim at
    80% used" — the inverse. Unclamped, that asks for 24 GiB free on a 30-GiB volume and
    ``free < trigger`` is then true at every plausible free-space level, so every pass evicts
    the entire store and every gate re-materializes its snapshot from scratch. At >=100 the
    trigger exceeds the volume outright. The clamp keeps the knob monotonic and bounded.
    """
    now = time.time()
    cfg = janitor.JanitorConfig(free_watermark_pct=100, grace_seconds=100, max_age_seconds=10**9)
    trigger, target = janitor._space_thresholds(_ROOT_TOTAL, cfg)
    assert trigger < _ROOT_TOTAL, "the trigger must never demand the whole volume be free"
    assert target < _ROOT_TOTAL, "the hysteresis target must never demand the whole volume"

    # 20 GiB free on a 30-GiB volume is 33% used — nothing should be reclaimed there.
    _sha, entry = _populate(repo, store, "a.txt", "x" * 50, mtime=now - 5000)
    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=20 * _GIB)
    assert res.evicted == 0, "a healthy 33%-used volume must not be reclaimed"
    assert entry.exists()


def test_absolute_floor_still_governs_when_it_is_the_larger_term(store, repo, monkeypatch):
    """The two watermark terms combine with ``max``, so the volume-relative term must never
    WEAKEN the absolute floor — the direction the incident case does not exercise.

    On a 10-GiB volume ``free_watermark_pct=10`` is only 1 GiB, below the 2-GiB absolute
    floor, so the floor governs and 1.5 GiB free (85% used) still reclaims.
    """
    import shutil as _shutil

    real = _shutil.disk_usage
    total = 10 * _GIB
    monkeypatch.setattr(
        janitor.shutil,
        "disk_usage",
        lambda p: type(real(p))(total, total - real(p).free, real(p).free),
    )
    now = time.time()
    cfg = janitor.JanitorConfig(
        free_watermark_bytes=2 * _GIB,
        free_watermark_pct=10,
        grace_seconds=100,
        max_age_seconds=10**9,
    )
    trigger, _target = janitor._space_thresholds(total, cfg)
    assert trigger == 2 * _GIB, "the larger (absolute) term must win"

    _sha, entry = _populate(repo, store, "a.txt", "x" * 50, mtime=now - 5000)
    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=int(1.5 * _GIB))
    assert res.evicted >= 1
    assert not entry.exists()


# --------------------------------------------------------------------------------------
# Bug 3907 — the ADR-promised byte-total backstop + accounting for the tickets- entries
#
# ADR 0005 D5 ("backstopped by the byte total") and the janitor's own module contract both
# promise a THIRD reclamation trigger driven by the incrementally-maintained byte total.
# _gc_pass read only free space and mtime, so the promise was inert; and materialize_tickets
# populated the store's LARGEST entries (~861 MiB each in the wild) without ever calling
# add_bytes, so the total those triggers would read under-counted them to zero.
# --------------------------------------------------------------------------------------
def test_max_bytes_cap_evicts_when_over_budget(store, repo):
    """The byte-total backstop is the ONLY term left armed here: free space is abundant
    (``free_bytes`` far above the watermark), the cold-trim is disarmed (``max_age`` huge) and
    every entry is outside the grace window. An eviction can therefore only come from the cap."""
    now = time.time()
    # DISTINCT mtimes, oldest first: LRU order must be well-defined, or "which entry goes
    # first" would fall through to filesystem iteration order and the assertion below would
    # be an order-dependent flake rather than a statement about LRU order. ONE path with
    # DISTINCT content per commit keeps the entries fully disjoint (no hardlink sharing,
    # task 5b25), so "evict LRU until under the cap, newest survives" is well-defined here;
    # the cap's interaction with SHARED entries is pinned by the bug-8386 tests below.
    entries = [
        _populate(repo, store, "f.txt", str(i) * 4096, mtime=now - 10_000 + i * 100)[1]
        for i in range(4)
    ]
    total = cache.byte_total(store)
    assert total > 0
    cfg = janitor.JanitorConfig(
        free_watermark_bytes=1,  # free-space term disarmed
        free_watermark_pct=0,
        grace_seconds=120,
        max_age_seconds=10**9,  # cold-trim disarmed
        max_bytes=total // 2,  # ... leaving only the cap
    )
    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=10**15)

    assert res.skipped is None, "the pass must actually run (not lose the gc lock)"
    assert res.evicted >= 1, "byte_total over the cap must reclaim"
    # The algorithmic property, not a wall-clock or disk-size assertion: reclaim continues
    # until the running total is back under the cap, and it stops there rather than draining
    # the whole store.
    assert cache.byte_total(store) <= cfg.max_bytes
    assert entries[-1].exists(), "a cap is not a purge — the most recent entry survives"
    # LRU order: the oldest entry goes first.
    assert not entries[0].exists()


def test_max_bytes_cap_off_by_default_changes_nothing(store, repo):
    """``max_bytes`` defaults to 0 = off (the ``free_watermark_pct`` precedent), so an
    existing deployment that sets no cap keeps exactly today's behaviour."""
    now = time.time()
    assert janitor.JanitorConfig().max_bytes == janitor.DEFAULT_MAX_BYTES == 0
    _sha, entry = _populate(repo, store, "a.txt", "x" * 4096, mtime=now - 10_000)
    cfg = janitor.JanitorConfig(
        free_watermark_bytes=1, grace_seconds=120, max_age_seconds=10**9, max_bytes=0
    )
    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=10**15)
    assert res.evicted == 0
    assert entry.exists()


def test_materialize_tickets_accounts_its_bytes(store, repo):
    """``materialize_tickets`` is a SECOND populate path alongside ``cache.acquire``; ADR 0005
    D2 requires the byte total to be maintained INCREMENTALLY, so it owes the same
    ``add_bytes`` its sibling pays. Without it the entries that dominate the store count zero
    and every size-driven trigger reads a total that is arbitrarily wrong."""
    _commit(repo, "seed.txt", "seed")
    _git(repo, "checkout", "--quiet", "-b", "tickets")
    _commit(repo, "t.json", "T" * 200_000)
    _git(repo, "checkout", "--quiet", "-")

    before = cache.byte_total(store)
    dest = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))
    real = cache.entry_size(dest)

    assert real > 0, "the entry must actually hold bytes"
    assert cache.byte_total(store) == before + real, "populated bytes must be accounted once"

    # Idempotent: a cache HIT re-uses the entry and must not double-count it.
    rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False)
    assert cache.byte_total(store) == before + real

    # And the incremental total agrees with the authoritative walk (what startup_sweep does).
    assert cache.byte_total(store) == sum(
        cache.entry_size(e) for e in store.iterdir() if janitor._is_entry(e)
    )


def test_cap_reclaims_a_tickets_entry_end_to_end(store, repo):
    """The two halves compose: an accounted ``tickets-<sha>`` entry is what pushes the store
    over the cap, and the cap is what reclaims it — the trigger fires end-to-end on the entry
    kind that actually dominates the store."""
    now = time.time()
    _commit(repo, "seed.txt", "seed")
    _git(repo, "checkout", "--quiet", "-b", "tickets")
    _commit(repo, "t.json", "T" * 200_000)
    _git(repo, "checkout", "--quiet", "-")
    dest = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))
    os.utime(dest, (now - 10_000, now - 10_000))  # outside the grace window

    total = cache.byte_total(store)
    assert total >= cache.entry_size(dest) > 0
    cfg = janitor.JanitorConfig(
        free_watermark_bytes=1,
        grace_seconds=120,
        max_age_seconds=10**9,
        max_bytes=total // 2,
    )
    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=10**15)
    assert res.evicted >= 1
    assert not dest.exists(), "the tickets- entry must be reclaimable by the cap"
    assert cache.byte_total(store) <= cfg.max_bytes


def test_max_bytes_resolved_from_env_and_snapshot_table(tmp_path, monkeypatch):
    """The knob rides the SAME owned config seam as every other janitor tunable:
    ``REBAR_GATE_MAX_BYTES`` env > ``[snapshot] max_bytes`` > documented default."""
    monkeypatch.setenv("REBAR_GATE_MAX_BYTES", "12345")
    assert janitor.JanitorConfig.from_env().max_bytes == 12345

    monkeypatch.delenv("REBAR_GATE_MAX_BYTES")
    proj = _init_repo(tmp_path / "proj")
    (proj / "rebar.toml").write_text("[snapshot]\nmax_bytes = 6789\n")
    assert janitor.JanitorConfig.from_env(repo_root=str(proj)).max_bytes == 6789


# --------------------------------------------------------------------------------------
# Bug 8386 (review finding) — hardlink sharing invalidates the janitor's size assumptions
# --------------------------------------------------------------------------------------
def _real_store_bytes(root: Path) -> int:
    """Bytes the store's ENTRIES actually occupy, charging every distinct inode once.

    Deliberately a SEPARATE implementation from ``cache.distinct_bytes`` rather than a call
    to it: an accounting test whose expected value comes from the code under test measures
    only that the code agrees with itself. This walk is written from the definition of "bytes
    on disk", so the two sides of every assertion below can disagree.

    Scoped to entries because that is what the byte total accounts for — the store also holds
    ``bytes.total`` itself, ``locks/`` and the per-entry sidecars, which it deliberately does
    not track."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for entry in janitor._entries(root):
        for dirpath, _dirnames, filenames in os.walk(entry):
            for name in filenames:
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                except OSError:  # pragma: no cover - racing eviction
                    continue
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += st.st_size
    return total


def _three_sharing_ticket_entries(repo: Path, store: Path) -> list[Path]:
    """Three ``tickets-<sha>`` entries built from one another by hardlink delta."""
    _commit(repo, "seed.txt", "seed")
    _git(repo, "checkout", "--quiet", "-b", "tickets")
    for i in range(8):
        (repo / f"t{i}.json").write_text("T" * 20_000)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "tickets base")
    entries = []
    for i in range(3):
        (repo / f"t{i}.json").write_text("T" * 19_999 + "X")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", f"delta {i}")
        entries.append(Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False)))
    _git(repo, "checkout", "--quiet", "-")
    return entries


def test_evicting_a_shared_entry_keeps_the_byte_total_honest(store, repo):
    """The running byte total must keep describing real disk once entries share inodes.

    Hardlink sharing (bug 8386) broke an assumption the janitor's accounting rested on. An
    apportioned per-entry size (``st_size // st_nlink``) is a reasonable REPORTING figure, but
    it is wrong as an incremental decrement: removing one of ``k`` links frees nothing until
    the last link goes, so subtracting a 1/k share drives the running total away from the
    bytes actually on disk. The ``max_bytes`` cap then evicts against bytes that do not
    exist. Measured on this fixture pre-fix: evicting one entry credited 69,997 bytes while
    freeing 20,000.
    """
    entries = _three_sharing_ticket_entries(repo, store)
    assert len(entries) == 3

    janitor.startup_sweep(store)  # authoritative walk -> the total starts exactly right
    assert cache.byte_total(store) == _real_store_bytes(store), (
        "the reconciling walk must agree exactly with the bytes on disk"
    )

    before = _real_store_bytes(store)
    janitor._evict(store, entries[0])
    after = _real_store_bytes(store)
    assert after < before, "the victim held some blobs exclusively; those should be gone"
    assert after > before // 2, "evicting one of three sharers must not free a whole tree"

    assert cache.byte_total(store) == after, (
        f"byte_total={cache.byte_total(store)} but the store really holds {after} bytes"
    )


def test_populating_shared_entries_tracks_real_disk_without_a_sweep(store, repo):
    """The POPULATE side must be honest on its own, with no reconciling walk to rescue it.

    The eviction tests below start with ``startup_sweep``, which resets the total from an
    authoritative walk — and in doing so would launder an over-credit made at populate time.
    This one never sweeps, so the only thing keeping ``byte_total`` in step with the disk is
    what each materialization added. Charging a shared entry its full size here inflates the
    total by every blob it reused, and the ``max_bytes`` cap then evicts against bytes that
    were never consumed — defeating the sharing this ticket exists to introduce.
    """
    entries = _three_sharing_ticket_entries(repo, store)
    assert len(entries) == 3

    on_disk = _real_store_bytes(store)
    assert cache.byte_total(store) == on_disk, (
        f"after three sharing materializations byte_total={cache.byte_total(store)} "
        f"but the entries really occupy {on_disk} bytes"
    )
    # Sharing is real here, so a per-entry full-size sum is strictly larger — i.e. the
    # assertion above is discriminating, not satisfied by every possible accounting.
    assert sum(cache.entry_size(e) for e in entries) > on_disk

    # And the reconciling walk agrees, so the incremental and authoritative paths converge
    # on the same number rather than each being self-consistently wrong.
    janitor.startup_sweep(store)
    assert cache.byte_total(store) == on_disk


def test_reclaim_credits_only_bytes_actually_freed(store, repo):
    """``_evict`` must report the disk it really freed, not an apportioned share.

    The free-space loop credits ``free += reclaimed`` and stops once the watermark is met, so
    an over-credit makes it believe it recovered space it did not and stop reclaiming while
    the volume is still under the watermark.
    """
    entries = _three_sharing_ticket_entries(repo, store)

    before = _real_store_bytes(store)
    freed = janitor._evict(store, entries[0])
    actually_freed = before - _real_store_bytes(store)
    assert freed == actually_freed, (
        f"_evict credited {freed} bytes but only {actually_freed} left the disk"
    )

    # The deferred bytes are not lost: the final holder of a shared blob does free it.
    janitor._evict(store, entries[1])
    mid = _real_store_bytes(store)
    last = janitor._evict(store, entries[2])
    assert last == mid - _real_store_bytes(store)
    assert last > 0, "evicting the final holder must credit the bytes it really released"
    assert _real_store_bytes(store) == 0


def test_the_three_size_questions_on_a_known_inode_layout(tmp_path):
    """Sharing splits "how big is this entry" into three questions with three answers.

    Every expectation below is a LITERAL read off the layout this test builds by hand —
    nothing is derived from the functions under test, so a change in any of them shows up
    as a mismatch instead of moving both sides of the comparison together.
    """
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "sub").mkdir(parents=True)
    right.mkdir()
    (left / "shared.bin").write_bytes(b"S" * 1000)
    (left / "sub" / "only-left.bin").write_bytes(b"A" * 300)
    (right / "only-right.bin").write_bytes(b"B" * 70)
    os.link(left / "shared.bin", right / "shared.bin")  # ONE inode, TWO links

    # REPORTING: full size, sharing ignored — and so it double-counts across entries.
    assert cache.entry_size(left) == 1300
    assert cache.entry_size(right) == 1070
    assert cache.entry_size(left) + cache.entry_size(right) == 2370  # 1000 counted twice

    # ACCOUNTING: the bytes this entry alone holds — what populating it added, and what
    # evicting it would free. The 1000 shared bytes belong to neither exclusively.
    assert cache.exclusive_size(left) == 300
    assert cache.exclusive_size(right) == 70

    # GROUND TRUTH: every inode once. 1000 + 300 + 70.
    assert cache.distinct_bytes([left, right]) == 1370

    # Dropping one of two links frees nothing and hands the bytes to the survivor, which
    # is exactly why "subtract exclusive_size on evict" balances: the inode is subtracted
    # once, when its LAST link goes.
    (left / "shared.bin").unlink()
    assert cache.exclusive_size(right) == 1070
    assert cache.distinct_bytes([left, right]) == 1370  # unchanged: the bytes never left


# --------------------------------------------------------------------------------------
# Bug 797c-a4ea — a materialize_tickets cache hit must bump the entry's recency (mtime)
# --------------------------------------------------------------------------------------
def _tickets_branch(repo: Path, body: str) -> None:
    _commit(repo, "seed.txt", "seed")
    _git(repo, "checkout", "--quiet", "-b", "tickets")
    _commit(repo, "t.json", body)
    _git(repo, "checkout", "--quiet", "-")


def test_materialize_tickets_bumps_mtime_on_cache_hit(store, repo):
    """ADR 0005 D4: the janitor evicts LRU by ``mtime``, which the cache bumps explicitly on
    EVERY hit. ``materialize_tickets`` is a second read path into the same store, so its
    cache-hit branch owes the same ``touch_entry`` that ``cache.acquire`` pays — without it a
    hot ``tickets-<sha>`` entry's recency is frozen at creation time."""
    _tickets_branch(repo, "T")
    dest = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))

    stale = time.time() - 10_000
    os.utime(dest, (stale, stale))
    rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False)  # cache hit

    assert cache.entry_mtime(dest) > stale + 1, (
        "a materialize_tickets cache hit must bump the entry mtime (ADR 0005 D4)"
    )


def test_hot_tickets_entry_is_not_first_lru_victim(store, repo):
    """The consequence pinned: a repeatedly-read ``tickets-`` entry must NOT be the first LRU
    victim ahead of a genuinely older-unread entry. With the hit un-recorded, the oldest-created
    (but hottest) entry sorts FIRST in the janitor's LRU order and is evicted preferentially."""
    now = time.time()
    _tickets_branch(repo, "T" * 200_000)

    # The tickets entry is created FIRST (oldest creation time = frozen mtime pre-fix) ...
    hot = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))
    os.utime(hot, (now - 50_000, now - 50_000))
    # ... a code entry is created later but never read again after this.
    _sha, cold = _populate(repo, store, "cold.txt", "C" * 100_000, mtime=now - 20_000)
    # The tickets entry keeps being read (cache hits).
    rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False)

    total = cache.byte_total(store)
    assert total > 0
    cfg = janitor.JanitorConfig(
        free_watermark_bytes=1,
        grace_seconds=120,
        max_age_seconds=10**9,
        max_bytes=total - 1,  # must reclaim at least one entry; the LRU-first one goes
    )
    res = janitor.run_gc(store, config=cfg, now=now, free_bytes=10**15)

    assert res.evicted >= 1
    assert hot.exists(), "the hot tickets- entry must not be the first LRU victim"
    assert not cold.exists(), "the genuinely cold entry is the correct victim"
