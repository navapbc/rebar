"""Advisory lock for the reconciler — the self-healing refs/reconciler/* CAS lock.

Provides:
  ReconcileLockError  — raised on fail-CLOSED conditions / "could not acquire"
  ReconcileLockLost   — raised mid-pass when the heartbeat detects a lost/stolen lease
  check_pass_lock     — True if the pass lock is currently held (fail-CLOSED on read error)
  acquire_pass_lock   — create-only CAS on refs/reconciler/lock; returns the oid
  renew_pass_lock     — heartbeat: CAS-renew the lease, returns the new oid
  release_pass_lock   — observed-oid CAS delete (idempotent)
  check_phase_gate    — True if advancement is blocked by the refs/reconciler/gate blob

The pass-lock/phase-gate live on ``refs/reconciler/*`` (a ref → blob, never in the
tickets working tree, never union-merged) via the C1/C2 primitive in :mod:`_ref_lock`.
The legacy tickets-branch file backend, its ``b859-8fa1`` retry loop, and the
``.reconciler-* merge=ours`` carve-out were retired in epic dust-troth-naval / C4
(ADR 0031); ``_cas_once`` / ``_is_cas_mismatch`` remain here as the single shared CAS
discriminator that :mod:`_ref_lock` imports.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# By-path sibling loader (this module is exec'd standalone in tests, so import
# ``lazy_load`` normally when package context exists, else bootstrap by path).
# ---------------------------------------------------------------------------

try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)  # type: ignore[union-attr]
    lazy_load = sys.modules[_loader_key].lazy_load


def _load_ref_lock():
    """Load the C1/C2 ref-lock primitive."""
    return lazy_load("rebar_reconciler__ref_lock_advisory", "_ref_lock.py")


# ---------------------------------------------------------------------------
# Config (the ref lock is authoritative on the sync remote). Reads fail SAFE.
# ---------------------------------------------------------------------------


def _reconciler_config():
    from rebar.config import ConfigError, load_config

    try:
        return load_config().reconciler
    except ConfigError:
        return None


def _lock_lease_secs() -> int:
    cfg = _reconciler_config()
    return int(getattr(cfg, "lock_lease_secs", 120)) if cfg is not None else 120


def _lock_remote(repo_root: Path) -> str | None:
    """The sync remote the ref lock is authoritative on, or None if it is not a
    configured remote of *repo_root* (single-clone / test → pure-local CAS)."""
    from rebar.config import ConfigError, load_config
    from rebar_reconciler import git_adapter

    try:
        remote = load_config().sync.remote or "origin"
    except ConfigError:
        remote = "origin"
    check = git_adapter.remote_get_url(repo_root, remote)
    return remote if check.returncode == 0 else None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ReconcileLockError(RuntimeError):
    """Raised on fail-CLOSED conditions, or when the pass lock cannot be acquired
    (someone holds it). Fail-CLOSED: when we cannot determine lock state confidently
    we block the orchestrator rather than silently disabling concurrency protection."""


class ReconcileLockLost(RuntimeError):
    """Raised mid-pass when the ref-backend heartbeat detects the lease was lost or
    stolen. Distinct from :class:`ReconcileLockError` ("could not get the lock"):
    this means "held it, then lost it" — the pass aborts and the ``finally`` release
    no-ops (the ref already moved), so a re-run is safe/idempotent."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_pass_lock(repo_root: Path) -> bool:
    """Return True if the reconciler pass lock (refs/reconciler/lock) is held.

    False when the ref is absent (no active lock). Fail-CLOSED: a corrupt/unreadable
    blob or a read timeout raises :class:`ReconcileLockError` rather than reporting
    "free". This is an advisory preflight — acquire's create-only CAS is the real
    single-winner guard.
    """
    ref_lock = _load_ref_lock()
    try:
        state = ref_lock.read(repo_root, ref_lock.LOCK_REF, remote=_lock_remote(repo_root))
    except (ref_lock.RefLockCorruptError, ref_lock.RefLockTimeoutError) as exc:
        raise ReconcileLockError(f"cannot determine pass-lock state (fail-CLOSED): {exc}") from exc
    return state is not None


def acquire_pass_lock(pass_id: str, repo_root: Path) -> str | None:
    """Acquire the reconciler pass lock via a create-only CAS on refs/reconciler/lock.

    Returns the acquired ref oid — thread it to :func:`renew_pass_lock` /
    :func:`release_pass_lock`.

    Raises:
        ReconcileLockError — if the lock is already held (create-only CAS rejected).
    """
    ref_lock = _load_ref_lock()
    try:
        return ref_lock.acquire(
            repo_root,
            ref_lock.LOCK_REF,
            holder=pass_id,
            lease_secs=_lock_lease_secs(),
            remote=_lock_remote(repo_root),
        )
    except ref_lock.RefLockHeldError as exc:
        raise ReconcileLockError(
            f"pass lock {ref_lock.LOCK_REF} already held (pass_id={pass_id!r}): {exc}"
        ) from exc


def steal_pass_lock(pass_id: str, repo_root: Path, *, sleep_fn=time.sleep) -> str | None:
    """Attempt to steal an EXPIRED pass lock, returning the new oid on success.

    Delegates to :func:`_ref_lock.steal` — the sanctioned skew-proof expiry
    primitive: it reads the ref, sleeps one ``lease_secs``, and re-checks whether
    the holder made ``(oid, fence)`` progress. ``_ref_lock`` deliberately does NOT
    trust ``heartbeat_ns`` wall-clock for expiry, so there is no ``pass_lock_is_expired``
    helper; ``steal()`` IS the expiry test.

    Returns:
        - a NEW oid — the lease was stale and we stole it (holder made no progress
          over one lease window); thread it into the heartbeat like ``acquire``'s oid.
        - ``None`` — the ref is free (holder released during our sleep) OR the holder
          is live (made progress); the caller re-reads to discriminate.

    Fail-CLOSED: any error from ``steal()`` (git transport/permission) is caught,
    logged, and reported as ``None`` (not stolen) rather than crashing the pass — the
    caller then falls through to exit-3.

    ``sleep_fn`` is injected in tests to avoid the real one-lease-length sleep.
    """
    ref_lock = _load_ref_lock()
    try:
        return ref_lock.steal(
            repo_root,
            ref_lock.LOCK_REF,
            holder=pass_id,
            remote=_lock_remote(repo_root),
            sleep_fn=sleep_fn,
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed: treat a steal error as "not stolen"
        logging.warning("steal_pass_lock: steal attempt failed (%s) — treating as not stolen", exc)
        return None


def renew_pass_lock(pass_id: str, repo_root: Path, oid: str) -> str:
    """Heartbeat: renew the pass lease, returning the new oid.

    Raises :class:`_ref_lock.LeaseLostError` if the lease was lost/stolen.
    """
    ref_lock = _load_ref_lock()
    return ref_lock.renew(repo_root, ref_lock.LOCK_REF, oid=oid, remote=_lock_remote(repo_root))


def release_pass_lock(pass_id: str, repo_root: Path, oid: str | None = None) -> None:
    """Release the reconciler pass lock (idempotent).

    Observed-oid CAS delete of ``refs/reconciler/lock`` against *oid* (the latest oid
    from acquire/renew); a stale/absent ref is a benign no-op.
    """
    ref_lock = _load_ref_lock()
    ref_lock.release(
        repo_root,
        ref_lock.LOCK_REF,
        oid=oid or ("0" * 40),
        remote=_lock_remote(repo_root),
    )


def check_phase_gate(target_mode, repo_root: Path) -> bool:
    """Return True if *target_mode* is blocked by the phase gate (refs/reconciler/gate).

    The gate blob carries the MODE name at or below which advancement is permitted;
    a strictly higher *target_mode* is blocked (``target_mode > gated_mode``, via
    Mode's ``@functools.total_ordering``). Absent gate ref = not blocked. A
    corrupt/unreadable gate blob fails closed (treated as gated).
    """
    ref_lock = _load_ref_lock()
    gated_mode_str = ref_lock.read_gate(repo_root, remote=_lock_remote(repo_root))
    return _mode_blocks(target_mode, gated_mode_str)


def _mode_blocks(target_mode, gated_mode_str: str | None) -> bool:
    """Return True iff *target_mode* is blocked by *gated_mode_str* (None = open).

    An unrecognised mode string is treated as no gate.
    """
    if not gated_mode_str:
        return False

    # Load mode.py under the SAME canonical dotted key that __main__.py uses so
    # tests (which pre-seed sys.modules under that key) and production code share a
    # single Mode class object (isinstance checks across module boundaries).
    mode_mod = lazy_load("rebar_reconciler.mode", "mode.py")

    try:
        gated_mode = mode_mod.Mode.from_str(gated_mode_str)
    except ValueError:
        logger.warning(
            "check_phase_gate: unrecognised mode %r in gate; treating as no gate",
            gated_mode_str,
        )
        return False

    return target_mode > gated_mode


# ---------------------------------------------------------------------------
# Shared CAS discriminator + single-shot seam (imported by :mod:`_ref_lock`).
# ---------------------------------------------------------------------------


def _is_cas_mismatch(
    exc: subprocess.CalledProcessError, ref_name: str = "refs/heads/tickets"
) -> bool:
    """Return True iff *exc* is an ``update-ref`` compare-and-swap old-sha mismatch.

    ``git update-ref <ref> <new> <old>`` (create-only or advance) reports a CAS
    old-sha mismatch as **exit 128**; the delete form ``git update-ref -d <ref>
    <old>`` reports it as **exit 1**. Both carry ``cannot lock ref '<ref>'`` in
    stderr, so we accept exit 128 OR an exit-1 ``cannot lock ref`` — a strict superset
    that never misclassifies an unrelated failure. We discriminate on the command
    shape (an ``update-ref`` invocation naming *ref_name*) so an unrelated exit-128
    from some other git command is not treated as a retryable race.
    """
    args = exc.cmd or []
    is_update_ref = "update-ref" in args and ref_name in args
    if not is_update_ref:
        return False
    if exc.returncode == 128:
        return True
    stderr = getattr(exc, "stderr", "") or ""
    return "cannot lock ref" in stderr


def _cas_once(mutate_and_advance, ref_name: str = "refs/heads/tickets") -> bool:
    """Run *mutate_and_advance* exactly once and classify the CAS outcome.

    Returns ``True`` when the callable completed (CAS succeeded), ``False`` when it
    failed with a CAS old-sha mismatch on *ref_name* (:func:`_is_cas_mismatch`). Any
    other ``CalledProcessError`` (or other exception) propagates immediately
    (fail-CLOSED). This is the single-shot seam :mod:`_ref_lock` builds acquire /
    release / steal / renew on — each interprets the ``False`` return itself (acquire
    → "already held"; release → idempotent success; steal → "lost the race").
    """
    try:
        mutate_and_advance()
        return True
    except subprocess.CalledProcessError as exc:
        if _is_cas_mismatch(exc, ref_name):
            return False
        # Not a CAS race — propagate (fail-CLOSED).
        raise
