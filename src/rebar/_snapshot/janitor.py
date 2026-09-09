"""Reclaim snapshot-cache disk and metadata safely off the read/populate path.

Readers use touch-on-read ``mtime`` and POSIX delete-on-last-close, not unsound
PID/heartbeat leases. One GC pass holds ``<root>/gc/lock`` and evicts LRU entries by
atomically renaming them to trash before recursive deletion, so open descriptors survive.

Reclamation combines four pressures: the larger of absolute and volume-relative free-space
watermarks, an optional running-byte cap, an on-by-default entry-count cap for filesystem
metadata, and independent cold-age trimming. The first three honor a grace window and fixed
hysteresis margin. Startup removes temporary/trash remnants and reconciles byte accounting;
digest reverification discards corruption for rematerialization. :class:`JanitorConfig`
resolves documented defaults through ``REBAR_GATE_*`` environment values, then
``[snapshot]`` configuration.
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
from rebar._store.fsutil import atomic_write

try:
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

# ── documented default tunables ───────────────────────────────────────────────────────
DEFAULT_FREE_WATERMARK_BYTES = 2 * 1024 * 1024 * 1024  # reclaim when free disk < 2 GiB
DEFAULT_FREE_WATERMARK_PCT = 0  # volume-relative headroom %, 0 = off (absolute floor only)
DEFAULT_GRACE_SECONDS = 120  # never evict an entry used within the last 2 minutes
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600  # cold-trim entries untouched for > 7 days
DEFAULT_MAX_BYTES = 0  # store-size cap in bytes: 0 = off (opt-in)
# The default entry cap exceeds normal working sets but bounds metadata pressure far below
# the incident's 13,056 entries. Zero is the operator opt-out.
DEFAULT_MAX_ENTRIES = 2000
DEFAULT_REVERIFY_SECONDS = 0  # periodic integrity reverify: 0 = off (opt-in)
DEFAULT_INTERVAL_SECONDS = 300  # background pass cadence
DEFAULT_MIN_FREE_GIB = 2  # hard pre-clone admission floor

# Fixed algorithmic hysteresis, deliberately not configurable. An armed pass raises free
# space by this margin or lowers byte/count totals by its mirror, avoiding threshold thrash.
RECLAIM_TARGET_MARGIN_PCT = 5

# Clamp requested free headroom so inverted expectations or values near 100 cannot evict the
# whole cache. Clamp rather than reject because a janitor tunable must not fail a gate.
MAX_FREE_WATERMARK_PCT = 50


@dataclass(frozen=True)
class VolumeFreeSpace:
    """Free-space measurement for the exact target path that would receive writes."""

    path: Path
    free_bytes: int
    total_bytes: int
    min_free_bytes: int


class SnapshotLowDiskError(RuntimeError):
    """Raised before starting a snapshot/review clone when the target volume is too full."""

    def __init__(self, space: VolumeFreeSpace) -> None:
        self.space = space
        super().__init__(
            f"low disk on {space.path}: free={space.free_bytes} bytes, "
            f"floor={space.min_free_bytes} bytes"
        )


def min_free_bytes(repo_root: str | os.PathLike[str] | None = None) -> int:
    from rebar._config_resolvers import resolve_gate_min_free_bytes

    return resolve_gate_min_free_bytes(DEFAULT_MIN_FREE_GIB, repo_root)


def volume_free_space(
    path: str | os.PathLike[str], *, repo_root: str | os.PathLike[str] | None = None
) -> VolumeFreeSpace:
    usage = shutil.disk_usage(os.fspath(path))
    return VolumeFreeSpace(
        path=Path(path),
        free_bytes=int(usage.free),
        total_bytes=int(usage.total),
        min_free_bytes=min_free_bytes(repo_root),
    )


def has_min_free_space(
    path: str | os.PathLike[str], *, repo_root: str | os.PathLike[str] | None = None
) -> bool:
    try:
        return volume_free_space(path, repo_root=repo_root).free_bytes >= min_free_bytes(repo_root)
    except OSError:
        return True


def ensure_min_free_space(
    path: str | os.PathLike[str], *, repo_root: str | os.PathLike[str] | None = None
) -> None:
    try:
        space = volume_free_space(path, repo_root=repo_root)
    except OSError:
        return
    if space.free_bytes < space.min_free_bytes:
        raise SnapshotLowDiskError(space)


def _default_tunables() -> dict[str, int]:
    """The documented janitor defaults, keyed by :class:`JanitorConfig` field — passed to
    the owned resolver so the ``[snapshot]``/env cutover keeps the defaults defined here."""
    return {
        "free_watermark_bytes": DEFAULT_FREE_WATERMARK_BYTES,
        "free_watermark_pct": DEFAULT_FREE_WATERMARK_PCT,
        "grace_seconds": DEFAULT_GRACE_SECONDS,
        "max_age_seconds": DEFAULT_MAX_AGE_SECONDS,
        "max_bytes": DEFAULT_MAX_BYTES,
        "max_entries": DEFAULT_MAX_ENTRIES,
        "reverify_seconds": DEFAULT_REVERIFY_SECONDS,
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
    }


@dataclass
class JanitorConfig:
    """Integer janitor settings with documented environment/config overrides.

    Reclamation starts at the larger absolute free-byte floor or whole-percentage headroom
    (zero disables the percentage), then continues through a fixed hysteresis margin.
    ``max_bytes`` independently caps the incrementally maintained store total and defaults
    off. ``max_entries`` bounds metadata regardless of disk size, defaults on, and uses zero
    as an explicit opt-out; both caps share the margin. ``max_age_seconds`` should greatly
    exceed ``grace_seconds`` so cold trimming does not defeat recency protection."""

    free_watermark_bytes: int = DEFAULT_FREE_WATERMARK_BYTES
    free_watermark_pct: int = DEFAULT_FREE_WATERMARK_PCT
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
    max_bytes: int = DEFAULT_MAX_BYTES
    max_entries: int = DEFAULT_MAX_ENTRIES
    reverify_seconds: int = DEFAULT_REVERIFY_SECONDS
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS

    @classmethod
    def from_env(cls, repo_root: str | None = None) -> JanitorConfig:
        """Resolve the tunables through the owned config seam
        (:func:`rebar.config.resolve_janitor_tunables`): ``REBAR_GATE_*`` env >
        ``[snapshot]`` config table > documented default. This dataclass RECEIVES the
        resolved values rather than reading ``os.environ`` / the config file itself."""
        from rebar import config

        return cls(**config.resolve_janitor_tunables(_default_tunables(), repo_root))


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
    """Identify a hex code entry or ``tickets-<sha>`` entry, excluding sidecar dirs.

    Recognizing the ticket prefix keeps those advancing entries visible to GC and accounting."""
    if not p.is_dir():
        return False
    name = p.name
    if name in {"tmp", "trash", "gc", "locks"}:
        return False
    core = name[len("tickets-") :] if name.startswith("tickets-") else name
    return bool(core) and all(c in "0123456789abcdef" for c in core)


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
    """Rename one entry to trash, delete it, remove sidecars, and return freed bytes.

    Measure :func:`exclusive_size` before rename: shared hardlinks free nothing, while the
    result drives both free-space progress and the running-total decrement. Open readers
    retain their descriptors after the canonical path disappears."""
    size = _cache.exclusive_size(entry)
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
    """TOFU-check an entry digest, evicting corruption for rematerialization.

    The integrity sidecar's ``mtime`` records the last clean check for periodic GC
    revalidation. Return ``True`` only when corruption was discarded."""
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
        # Publish the TOFU baseline through a unique same-dir temp and os.replace; a plain
        # write or shared temp could expose a truncated stamp and evict a healthy entry.
        atomic_write(digest_path, current)
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
    # Charge each inode ONCE: summing per-entry sizes would count a blob shared by k entries
    # k times, and the incremental path (exclusive_size) counts it once.
    total = _cache.distinct_bytes(_entries(root))
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


def _space_thresholds(total: int, cfg: JanitorConfig) -> tuple[int, int]:
    """Return free-byte ``(trigger, target)`` thresholds for a volume.

    Each is the larger absolute or clamped percentage term. Zero percentage preserves the
    absolute-only behavior with no margin; otherwise the higher target provides hysteresis
    without allowing an out-of-range value to evict the whole store."""
    if cfg.free_watermark_pct <= 0:
        return cfg.free_watermark_bytes, cfg.free_watermark_bytes
    pct = min(cfg.free_watermark_pct, MAX_FREE_WATERMARK_PCT)
    trigger = max(cfg.free_watermark_bytes, total * pct // 100)
    target = max(cfg.free_watermark_bytes, total * (pct + RECLAIM_TARGET_MARGIN_PCT) // 100)
    return trigger, target


def _byte_thresholds(root: Path, cfg: JanitorConfig) -> tuple[int, int]:
    """Return current and hysteretic target bytes for the store cap.

    A nonpositive cap avoids even the locked total read; an enabled cap targets below its
    trigger so later passes do not immediately re-fire."""
    if cfg.max_bytes <= 0:
        return 0, 0
    return _cache.byte_total(root), cfg.max_bytes * (100 - RECLAIM_TARGET_MARGIN_PCT) // 100


def _count_thresholds(entries: list[Path], cfg: JanitorConfig) -> tuple[int, int]:
    """Return live and hysteretic target counts for the entry cap.

    A nonpositive cap returns ``(0, 0)``. Counting is free after the GC pass's LRU
    enumeration; the operation-linked trigger still performs only one stamp ``stat``.
    Integer rounding may collapse the margin for tiny caps, correctly targeting the cap."""
    if cfg.max_entries <= 0:
        return 0, 0
    return len(entries), cfg.max_entries * (100 - RECLAIM_TARGET_MARGIN_PCT) // 100


def _reclaim_loop(
    root: Path,
    cfg: JanitorConfig,
    entries: list[Path],
    now: float,
    res: GcResult,
    *,
    free: int,
    space_trigger: int,
    space_target: int,
) -> None:
    """Evict ``entries`` LRU-first and accumulate results in ``res``.

    Free-space, byte-cap, entry-cap, and cold-age pressure compose independently. Grace
    protects recent entries from the first three; cold trim intentionally overrides it.
    Bounded terms disarm only at hysteretic targets. Track bytes in-loop from each eviction
    instead of taking a locked total read per entry."""
    grace_floor = now - cfg.grace_seconds
    max_age_floor = now - cfg.max_age_seconds
    used, byte_target = _byte_thresholds(root, cfg)
    live, count_target = _count_thresholds(entries, cfg)

    need_space = free < space_trigger
    over_budget = cfg.max_bytes > 0 and used > cfg.max_bytes
    over_count = cfg.max_entries > 0 and live > cfg.max_entries

    for entry in entries:
        mtime = _cache.entry_mtime(entry)
        in_grace = mtime > grace_floor
        too_cold = mtime < max_age_floor
        wants_room = need_space or over_budget or over_count

        if in_grace and not too_cold:
            # Recently used and not yet max-age cold → protected by the grace window.
            if wants_room:
                res.skipped_grace += 1
            continue
        if not wants_room and not too_cold:
            continue  # nothing is asking for room and the entry is not cold — keep it

        reclaimed = _evict(root, entry)
        if reclaimed or not entry.exists():
            res.evicted += 1
            res.reclaimed_bytes += reclaimed
            free += reclaimed
            used -= reclaimed
            live -= 1
            need_space = need_space and free < space_target
            over_budget = over_budget and used > byte_target
            over_count = over_count and live > count_target


def _reverify_pass(root: Path, cfg: JanitorConfig, now: float, res: GcResult) -> None:
    """Optional periodic integrity reverify (opt-in via ``reverify_seconds > 0``). Honors the
    PERIOD: an entry reverified within the window is skipped (its integrity sidecar's mtime is
    the last-reverified stamp). Accumulates into ``res`` in place."""
    if cfg.reverify_seconds <= 0:
        return
    reverify_floor = now - cfg.reverify_seconds
    for entry in _entries(root):
        if _last_reverified(root, entry.name) > reverify_floor:
            continue
        res.reverified += 1
        if reverify_entry(entry.name, root):
            res.healed += 1


def _gc_pass(root: Path, cfg: JanitorConfig, now: float, free_bytes: int | None) -> GcResult:
    res = GcResult()
    # Re-drain any straggler trash from an interrupted prior pass first (AC5).
    drain_trash(root)

    entries = sorted(_entries(root), key=_cache.entry_mtime)  # LRU first

    # ``total`` ALWAYS comes from the real volume (the percentage term is meaningless without
    # it); only ``free`` is overridable, so a test can inject pressure on a real filesystem.
    usage = shutil.disk_usage(str(root))
    free = usage.free if free_bytes is None else free_bytes
    trigger, target = _space_thresholds(usage.total, cfg)
    _reclaim_loop(
        root,
        cfg,
        entries,
        now,
        res,
        free=free,
        space_trigger=trigger,
        space_target=target,
    )

    _reverify_pass(root, cfg, now, res)
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
