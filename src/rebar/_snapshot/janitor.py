"""Snapshot-cache janitor — reclamation under disk pressure (epic ``raze-vet-ditch`` S2b).

Reclaiming the content-addressed snapshot cache WITHOUT corrupting concurrent readers is
the riskiest piece. A PID+heartbeat lease was spiked and REJECTED as unsound (N readers
per entry, PID reuse, crash-stale leases); mature systems (Gitaly, Sourcegraph gitserver,
Bazel, ccache) lean on kernel guarantees instead. This janitor does too — it relies on the
S2 cache's POSIX delete-on-last-close reads + touch-on-read ``mtime`` recency, and never
takes a per-reader lease. See ``docs/adr/0005-snapshot-cache-architecture.md``.

Design:
  * **Off the hot path.** A SINGLE background pass (Bazel moved GC to idle), never invoked
    from populate/read. :func:`start_background_janitor` runs :func:`run_gc` on an interval.
  * **Trigger.** Primary = a FREE-SPACE watermark (:func:`shutil.disk_usage`), backstopped
    by the incrementally-maintained byte total (no hot-path ``du``). Secondary = a max-age
    cold-trim of genuinely cold entries.
  * **Victim selection.** LRU by the cache's touch-on-read ``mtime`` (never ``atime``),
    skipping any entry within a short grace window.
  * **Eviction mechanism.** ``rename(<sha>, trash/<uuid>)`` (atomic disappearance from the
    canonical path — open fds survive) THEN ``rmtree`` the trash entry — NEVER an in-place
    recursive delete of a live entry.
  * **Cross-process interlock.** A single GC pass holds an exclusive ``flock`` on
    ``<root>/gc/lock``; population stays lock-free.
  * **Recovery.** Startup sweep clears ``tmp/*`` + ``trash/*`` and reconciles the byte total
    via one full walk; an interrupted rename→rmtree straggler is re-drained on a later pass.
  * **Self-heal.** A corrupt/truncated entry is detected (content-digest reverify) and
    discarded so the next acquire re-materializes it.

Tunables (free-space watermark, grace window, max-age, reverify period, background interval)
are configurable with documented defaults via :class:`JanitorConfig` — resolved
``REBAR_GATE_*`` env > ``[snapshot]`` config table > default.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from rebar._snapshot import cache as _cache
from rebar._snapshot.repo_snapshot import store_root, sweep_tmp

try:
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

# ── documented default tunables ───────────────────────────────────────────────────────
DEFAULT_FREE_WATERMARK_BYTES = 2 * 1024 * 1024 * 1024  # reclaim when free disk < 2 GiB
DEFAULT_GRACE_SECONDS = 120  # never evict an entry used within the last 2 minutes
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600  # cold-trim entries untouched for > 7 days
DEFAULT_REVERIFY_SECONDS = 0  # periodic integrity reverify: 0 = off (opt-in)
DEFAULT_INTERVAL_SECONDS = 300  # background pass cadence


def _read_snapshot_table(repo_root: str | None = None) -> dict:
    """The merged ``[snapshot]`` config table (user < project), or ``{}`` if unreadable —
    a broken core config degrades to env-only, never breaks the janitor."""
    try:
        from rebar import config as _root_config

        return _root_config.read_reserved_section("snapshot", repo_root)
    except Exception:  # noqa: BLE001 - degrade to env/defaults on any config error
        return {}


def _int_pref(table: dict, env_name: str, file_key: str, default: int) -> int:
    """Resolve an int tunable: ``REBAR_GATE_*`` env > ``[snapshot]`` file key > default."""
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            pass
    fv = table.get(file_key)
    if fv is not None and not isinstance(fv, bool):
        try:
            return int(fv)
        except (TypeError, ValueError):
            pass
    return default


@dataclass
class JanitorConfig:
    """Snapshot-cache janitor tunables (documented defaults; env/config overridable).

    ``max_age_seconds`` is expected to be MUCH larger than ``grace_seconds`` (cold-trim
    age ≫ recency-protection window); a contradictory ``max_age < grace`` would let the
    cold-trim override the grace protection."""

    free_watermark_bytes: int = DEFAULT_FREE_WATERMARK_BYTES
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
    reverify_seconds: int = DEFAULT_REVERIFY_SECONDS
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS

    @classmethod
    def from_env(cls, repo_root: str | None = None) -> JanitorConfig:
        t = _read_snapshot_table(repo_root)
        return cls(
            free_watermark_bytes=_int_pref(
                t,
                "REBAR_GATE_FREE_WATERMARK_BYTES",
                "free_watermark_bytes",
                DEFAULT_FREE_WATERMARK_BYTES,
            ),
            grace_seconds=_int_pref(
                t, "REBAR_GATE_GRACE_SECONDS", "grace_seconds", DEFAULT_GRACE_SECONDS
            ),
            max_age_seconds=_int_pref(
                t, "REBAR_GATE_MAX_AGE_SECONDS", "max_age_seconds", DEFAULT_MAX_AGE_SECONDS
            ),
            reverify_seconds=_int_pref(
                t, "REBAR_GATE_REVERIFY_SECONDS", "reverify_seconds", DEFAULT_REVERIFY_SECONDS
            ),
            interval_seconds=_int_pref(
                t,
                "REBAR_GATE_JANITOR_INTERVAL_SECONDS",
                "interval_seconds",
                DEFAULT_INTERVAL_SECONDS,
            ),
        )


# ── store layout helpers ────────────────────────────────────────────────────────────
def _trash_dir(root: Path) -> Path:
    d = root / "trash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gc_lock_path(root: Path) -> Path:
    d = root / "gc"
    d.mkdir(parents=True, exist_ok=True)
    return d / "lock"


def _is_entry(p: Path) -> bool:
    """A content-addressed snapshot entry: a hex-named directory (not tmp/trash/gc/locks)."""
    if not p.is_dir():
        return False
    name = p.name
    return name not in {"tmp", "trash", "gc", "locks"} and all(
        c in "0123456789abcdef" for c in name
    )


def _entries(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if _is_entry(p)]


def _remove_sidecars(root: Path, sha: str) -> None:
    for suffix in (".caveats.json", ".integrity"):
        try:
            (root / f"{sha}{suffix}").unlink()
        except OSError:
            pass


# ── eviction: rename-to-trash THEN rmtree (never in-place) ──────────────────────────
def _evict(root: Path, entry: Path) -> int:
    """Evict ONE entry: measure it, atomically rename it into trash (it disappears from the
    canonical path immediately; readers holding open fds keep reading), rmtree the trash
    copy, drop the byte total, and remove sidecars. Returns the bytes reclaimed."""
    size = _cache.entry_size(entry)
    sha = entry.name
    dest = _trash_dir(root) / f"{uuid.uuid4().hex}"
    try:
        os.rename(entry, dest)
    except OSError:
        return 0  # already gone (raced another evictor) — nothing reclaimed
    shutil.rmtree(dest, ignore_errors=True)
    _remove_sidecars(root, sha)
    _cache.add_bytes(-size, root)
    return size


def drain_trash(root: Path | None = None) -> int:
    """Re-drain any trash stragglers (an interrupted rename→rmtree from a prior pass).
    Returns the count of trash entries removed."""
    root = root or store_root()
    trash = root / "trash"
    if not trash.is_dir():
        return 0
    removed = 0
    for child in list(trash.iterdir()):
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(
            missing_ok=True
        )
        removed += 1
    return removed


# ── integrity reverify + self-heal ──────────────────────────────────────────────────
def _entry_digest(entry: Path) -> str:
    """A content digest over (relpath, size, blake2b(content)) for every file — detects
    truncation/corruption without git, and without the submodule/LFS false-positives a
    re-derived git tree-id would suffer (gitlinks absent by design)."""
    h = hashlib.blake2b(digest_size=32)
    for rel in sorted(
        os.path.relpath(os.path.join(dp, fn), entry)
        for dp, _dn, fns in os.walk(entry)
        for fn in fns
    ):
        fp = entry / rel
        try:
            data = fp.read_bytes()
        except OSError:
            data = b"<unreadable>"
        h.update(rel.encode())
        h.update(str(len(data)).encode())
        h.update(hashlib.blake2b(data, digest_size=16).digest())
    return h.hexdigest()


def reverify_entry(sha: str, root: Path | None = None) -> bool:
    """Verify a cache entry against its stored content digest; discard it if corrupt.

    Trust-on-first-use: the first call records the digest; later calls detect drift
    (truncation/bit-rot). On mismatch the entry is evicted (rename-to-trash) so the next
    acquire re-materializes it. The integrity sidecar's mtime doubles as the
    "last-reverified" timestamp (bumped on every clean check) so :func:`run_gc` can honor
    the configured reverify PERIOD. Returns ``True`` if found corrupt + discarded."""
    root = root or store_root()
    entry = root / sha
    if not entry.is_dir():
        return False
    digest_path = root / f"{sha}.integrity"
    current = _entry_digest(entry)
    try:
        stored = digest_path.read_text().strip()
    except OSError:
        stored = ""
    if not stored:
        digest_path.write_text(current)  # TOFU baseline (mtime = now)
        return False
    if current != stored:
        _evict(root, entry)
        return True
    # Clean: stamp the last-reverified time so the period is honored next pass.
    try:
        os.utime(digest_path, None)
    except OSError:  # pragma: no cover - best effort
        pass
    return False


def _last_reverified(root: Path, sha: str) -> float:
    try:
        return (root / f"{sha}.integrity").stat().st_mtime
    except OSError:
        return 0.0


# ── the GC pass ─────────────────────────────────────────────────────────────────────
@dataclass
class GcResult:
    skipped: str | None = None  # set if the pass did not run (e.g. "locked")
    evicted: int = 0
    reclaimed_bytes: int = 0
    skipped_grace: int = 0
    reverified: int = 0
    healed: int = 0


def startup_sweep(root: Path | None = None) -> int:
    """Crash recovery: clear ``tmp/*`` and ``trash/*`` and reconcile the byte total via one
    full walk (the authoritative count). Returns the reconciled byte total."""
    root = root or store_root()
    sweep_tmp(root)
    drain_trash(root)
    total = sum(_cache.entry_size(e) for e in _entries(root))
    # Authoritative reset (not an increment) — the walk IS ground truth.
    delta = total - _cache.byte_total(root)
    _cache.add_bytes(delta, root)
    return total


def run_gc(
    root: Path | None = None,
    *,
    config: JanitorConfig | None = None,
    now: float | None = None,
    free_bytes: int | None = None,
) -> GcResult:
    """Run ONE reclamation pass under the exclusive GC interlock.

    ``free_bytes`` overrides the measured free space (tests inject disk pressure). A
    concurrent pass in another process cannot run — the non-blocking ``flock`` returns
    ``GcResult(skipped="locked")``."""
    root = root or store_root()
    cfg = config or JanitorConfig.from_env()
    now = time.time() if now is None else now
    lock_path = _gc_lock_path(root)

    # Exclusive, NON-BLOCKING gc interlock (AC7). Without fcntl, fall back to an atomic
    # mkdir guard so two passes still cannot overlap.
    if fcntl is not None:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return GcResult(skipped="locked")
            return _gc_pass(root, cfg, now, free_bytes)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    else:  # pragma: no cover - non-POSIX fallback
        guard = lock_path.with_suffix(".d")
        try:
            os.mkdir(str(guard))
        except FileExistsError:
            return GcResult(skipped="locked")
        try:
            return _gc_pass(root, cfg, now, free_bytes)
        finally:
            try:
                os.rmdir(str(guard))
            except OSError:
                pass


def _gc_pass(root: Path, cfg: JanitorConfig, now: float, free_bytes: int | None) -> GcResult:
    res = GcResult()
    # Re-drain any straggler trash from an interrupted prior pass first (AC5).
    drain_trash(root)

    entries = sorted(_entries(root), key=lambda p: _cache.entry_mtime(p))  # LRU first
    grace_floor = now - cfg.grace_seconds
    max_age_floor = now - cfg.max_age_seconds

    free = shutil.disk_usage(str(root)).free if free_bytes is None else free_bytes

    for entry in entries:
        mtime = _cache.entry_mtime(entry)
        in_grace = mtime > grace_floor
        too_cold = mtime < max_age_floor
        need_space = free < cfg.free_watermark_bytes

        if in_grace and not too_cold:
            # Recently used and not yet max-age cold → protected by the grace window.
            if need_space:
                res.skipped_grace += 1
            continue
        if not need_space and not too_cold:
            continue  # enough free space and the entry is not cold — keep it

        reclaimed = _evict(root, entry)
        if reclaimed or not entry.exists():
            res.evicted += 1
            res.reclaimed_bytes += reclaimed
            free += reclaimed

    # Optional periodic integrity reverify (opt-in via reverify_seconds > 0). Honors the
    # PERIOD: an entry reverified within the window is skipped (its integrity sidecar's
    # mtime is the last-reverified stamp).
    if cfg.reverify_seconds > 0:
        reverify_floor = now - cfg.reverify_seconds
        for entry in _entries(root):
            if _last_reverified(root, entry.name) > reverify_floor:
                continue
            res.reverified += 1
            if reverify_entry(entry.name, root):
                res.healed += 1
    return res


# ── background driver (off the hot path) ────────────────────────────────────────────
def start_background_janitor(
    *,
    config: JanitorConfig | None = None,
    repo_root: str | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Start a daemon thread running :func:`run_gc` every ``interval_seconds``, OFF the hot
    path. Returns ``(thread, stop_event)``; set the event to stop. Runs a startup sweep
    once before the loop."""
    cfg = config or JanitorConfig.from_env(repo_root)
    stop = threading.Event()

    def _loop() -> None:
        startup_sweep()
        while not stop.is_set():
            try:
                run_gc(config=cfg)
            except Exception:  # noqa: BLE001 - a janitor pass must never crash the server
                pass
            stop.wait(cfg.interval_seconds)

    thread = threading.Thread(target=_loop, name="rebar-snapshot-janitor", daemon=True)
    thread.start()
    return thread, stop
