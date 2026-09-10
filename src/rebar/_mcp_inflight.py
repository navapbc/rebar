"""Process-local singleflight for long-running, billable MCP gates.

Concurrent calls sharing gate, canonical ticket, resolved basis SHA, variant, and
readonly mode attach to one result. Completion purges the key; a max-age sweep frees
crashed leaders. The default-on ``REBAR_MCP_DEDUP=0`` kill switch bypasses attachment.
Threading events match FastMCP's synchronous worker bodies and preserve certified-tool
gauge instrumentation without module-level rebar imports.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

# Reclaim only leaders that missed completion cleanup; 40 minutes is twice the normal
# gate window, and changed bases naturally produce different keys.
_MAX_AGE_SECONDS: float = 40 * 60


def dedup_enabled() -> bool:
    """Return whether default-on dedup is active; false-like env values disable it.

    The environment kill switch changes behavior immediately without importing config.
    """
    raw = os.environ.get("REBAR_MCP_DEDUP")  # read-via: subsystem-kill-switch
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def new_job_id() -> str:
    """Return a unique, time-sortable ``{ns-timestamp}-{uuid4hex}`` gate handle."""
    return f"{time.time_ns()}-{uuid.uuid4().hex}"


def canonical_ticket_id(ticket_id: str) -> str:
    """Best-effort resolve an alias / short-id to the full canonical id for keying.

    So ``REB-310``, an alias, and the full id map to ONE de-dup key. Best-effort by
    design: any resolution failure (no store, ambiguous id) returns the input
    unchanged rather than failing the gate call the caller actually asked for."""
    try:
        from rebar._engine_support.reads import resolve_ticket_id, tracker_dir

        tdir = tracker_dir()
        if not os.path.isdir(tdir):
            return ticket_id
        return resolve_ticket_id(ticket_id, tdir, quiet=True) or ticket_id
    except Exception:  # noqa: BLE001 — canonicalisation must never fail the op
        return ticket_id


def resolve_basis_sha(ref: str | None, source: str | None, repo_root: str | None = None) -> str:
    """Resolve the effective review ref to a 40-hex commit SHA — the SAME anchor the
    gate binds into its attestation — so the key tracks the reviewed *snapshot*, not a
    moving symbolic ref.

    Default ``origin/main`` (``source='local'`` => ``HEAD``), matching the gate tools'
    defaults. An unresolvable ref yields a stable ``unresolved:<ref>`` sentinel rather
    than raising: a bad ref is the gate's error to report, and two concurrent calls
    with the same bad ref still de-dup on the identical sentinel."""
    effective = ref or ("HEAD" if source == "local" else "origin/main")
    try:
        from rebar.llm.workflow.snapshot import resolve_sha

        return resolve_sha(effective, repo_root)
    except Exception:  # noqa: BLE001 — a bad ref is the gate's error to raise, not ours
        return f"unresolved:{effective}"


def compute_key(
    gate_type: str,
    ticket_id: str,
    basis_sha: str,
    variant: str,
    readonly: bool,
) -> str:
    """The de-dup key: ``sha256`` over the NUL-joined dimensions that make two calls
    the SAME logical gate op. ``ticket_id`` is canonicalised by the caller (or here is
    hashed as given for a pre-canonicalised id)."""
    parts = [gate_type, ticket_id, basis_sha, variant, "1" if readonly else "0"]
    return sha256("\0".join(parts).encode("utf-8")).hexdigest()


@dataclass
class _Inflight:
    """One in-flight logical gate op. ``event`` releases followers; the leader fills
    ``result``/``error`` before setting it."""

    job_id: str
    started_monotonic: float
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    done: bool = False


_registry: dict[str, _Inflight] = {}
_lock = threading.Lock()

# Forced and dedup-disabled jobs use NUL-prefixed private keys that cannot collide with
# hex content keys, yet remain visible to activity checks and stale sweeping.
_STANDALONE_KEY_PREFIX = "standalone\x00"


def reset_registry() -> None:
    """Drop all registry state (test seam; also a hard reset for a kill-switch flip)."""
    with _lock:
        _registry.clear()


def seed_stale_entry(dedup_key: str) -> None:
    """Insert a never-completing in-flight entry aged past the sweep ceiling (test seam)
    to prove the defensive max-age sweep reclaims a wedged leader's key."""
    with _lock:
        _registry[dedup_key] = _Inflight(
            job_id=new_job_id(),
            started_monotonic=time.monotonic() - (_MAX_AGE_SECONDS + 1.0),
        )


def active_job_id(dedup_key: str) -> str | None:
    """Return the live job for a key so duplicate async starts share its handle."""
    with _lock:
        hit = _registry.get(dedup_key)
        return hit.job_id if hit is not None and not hit.done else None


def is_job_active(job_id: str) -> bool:
    """Whether this process still owns an in-flight ``job_id``.

    Pollers treat an inactive job whose durable index says running as a crashed leader.
    """
    with _lock:
        return any(e.job_id == job_id and not e.done for e in _registry.values())


def _sweep_locked() -> None:
    """Evict entries older than the max-age ceiling. Caller holds ``_lock``."""
    now = time.monotonic()
    stale = [k for k, v in _registry.items() if now - v.started_monotonic > _MAX_AGE_SECONDS]
    for k in stale:
        _registry.pop(k, None)


def _register_standalone(job_id: str) -> tuple[_Inflight, str]:
    """Register a private but observable forced or dedup-disabled run.

    Its fresh job-key prevents attachment, while activity polling and stale cleanup still
    cover the daemon's lifetime.
    """
    with _lock:
        entry = _Inflight(job_id=job_id, started_monotonic=time.monotonic())
        _registry[f"{_STANDALONE_KEY_PREFIX}{job_id}"] = entry
        return entry, f"{_STANDALONE_KEY_PREFIX}{job_id}"


def _attach_or_create(dedup_key: str, job_id_factory: Callable[[], str]) -> tuple[_Inflight, bool]:
    """Under the lock: attach to a live entry (``leader=False``) or create one
    (``leader=True``). The sweep runs first so a wedged key can never wrongly block."""
    with _lock:
        _sweep_locked()
        hit = _registry.get(dedup_key)
        if hit is not None and not hit.done:
            return hit, False
        entry = _Inflight(job_id=job_id_factory(), started_monotonic=time.monotonic())
        _registry[dedup_key] = entry
        return entry, True


def _drop(dedup_key: str, entry: _Inflight) -> None:
    """Purge ``entry`` on completion (identity-checked so a re-created key survives)."""
    with _lock:
        if _registry.get(dedup_key) is entry:
            _registry.pop(dedup_key, None)


def _finish(entry: _Inflight, dedup_key: str) -> None:
    """Mark the entry terminal, release followers, and purge the key."""
    entry.done = True
    entry.event.set()
    _drop(dedup_key, entry)


def _await_follower(entry: _Inflight) -> tuple[str, Any]:
    """A follower blocks on the leader's event, then re-raises its error or shares its result."""
    entry.event.wait()
    if entry.error is not None:
        raise entry.error
    return entry.job_id, entry.result


def _run_leader(dedup_key: str, entry: _Inflight, work: Callable[[], Any]) -> tuple[str, Any]:
    """The leader runs ``work`` once; ``finally`` releases followers + purges even on error."""
    try:
        entry.result = work()
    except BaseException as exc:
        entry.error = exc
        raise
    finally:
        _finish(entry, dedup_key)
    return entry.job_id, entry.result


def run_singleflight(
    dedup_key: str,
    job_id_factory: Callable[[], str],
    work: Callable[[], Any],
    *,
    bypass: bool = False,
) -> tuple[str, Any]:
    """Run ``work`` under singleflight de-dup, returning ``(job_id, result)``.

    The FIRST caller for ``dedup_key`` (the leader) runs ``work``; concurrent callers
    for the same key (followers) block until it completes and receive the SAME result
    (or the SAME exception). The key is purged on completion, so a call after the run
    finished re-invokes. ``bypass=True`` (or the ``REBAR_MCP_DEDUP=0`` kill-switch)
    runs ``work`` directly with a fresh job_id and never touches the registry."""
    if bypass or not dedup_enabled():
        return job_id_factory(), work()
    entry, leader = _attach_or_create(dedup_key, job_id_factory)
    if not leader:
        return _await_follower(entry)
    return _run_leader(dedup_key, entry, work)


def run_gate_singleflight(
    gate_type: str,
    ticket_id: str,
    *,
    ref: str | None,
    source: str | None,
    variant: str,
    readonly: bool,
    force: bool,
    work: Callable[[], Any],
    repo_root: str | None = None,
) -> Any:
    """Phase-1 convenience: derive the key from the gate args and run ``work`` under
    singleflight, returning just the verdict (the sync tool contract is unchanged).

    ``force=True`` bypasses de-dup entirely (mirrors ``review_plan(force=True)``
    bypassing the attestation short-circuit): a human forcing a fresh review must not
    attach to an in-flight one."""
    basis = resolve_basis_sha(ref, source, repo_root)
    key = compute_key(gate_type, canonical_ticket_id(ticket_id), basis, variant, readonly)
    _job_id, result = run_singleflight(key, new_job_id, work, bypass=force)
    return result


@dataclass
class GateJobHandle:
    """A reserved (or attached-to) singleflight slot for the Phase-2 ``*_start`` tools.

    ``is_new`` is True for the LEADER — the caller that must spawn the background daemon
    and, when it settles, call :meth:`complete` to release any attached followers. When
    ``is_new`` is False the caller ATTACHED to a run already in flight (``job_id`` is that
    run's id) and must NOT start a second billable pass — it just returns the shared
    handle to be polled."""

    job_id: str
    is_new: bool
    _dedup_key: str | None = None
    _entry: _Inflight | None = None

    def complete(self, result: Any = None, error: BaseException | None = None) -> None:
        """Leader-only: publish the verdict to attached followers and purge the key.

        A no-op for a follower handle or a bypassed (forced / kill-switch) job, which
        own no registry entry. Idempotent — safe to call once from the daemon's
        ``finally``."""
        if self._entry is None or self._dedup_key is None:
            return
        self._entry.result = result
        self._entry.error = error
        _finish(self._entry, self._dedup_key)


def begin_gate_job(
    gate_type: str,
    ticket_id: str,
    *,
    ref: str | None = None,
    source: str | None = None,
    variant: str = "",
    readonly: bool = False,
    force: bool = False,
    repo_root: str | None = None,
) -> GateJobHandle:
    """Reserve or attach to a slot without running work in the caller.

    Only a handle with ``is_new`` spawns the daemon; duplicate starts receive the live
    job ID. Forced or dedup-disabled starts get private, unattachable jobs that remain
    observable for their full lifetime.
    """
    if force or not dedup_enabled():
        job_id = new_job_id()
        entry, key = _register_standalone(job_id)
        return GateJobHandle(job_id, True, key, entry)
    basis = resolve_basis_sha(ref, source, repo_root)
    key = compute_key(gate_type, canonical_ticket_id(ticket_id), basis, variant, readonly)
    entry, leader = _attach_or_create(key, new_job_id)
    if leader:
        return GateJobHandle(entry.job_id, True, key, entry)
    return GateJobHandle(entry.job_id, False)
