"""In-process singleflight de-duplication for long-running MCP gate ops (bug d80d).

THE PROBLEM. ``review_plan`` / ``verify_completion`` over the MCP server run a 15-20
minute billable LLM gate. The MCP *client* SDK abandons the request at 60s with an
opaque ``-32001``; the server keeps running and signs its attestation. The documented
agent reflex to ``-32001`` is to re-invoke — which, with no de-duplication, starts a
SECOND billable LLM pass while the first is still in flight, multiplying cost without
bound under a retry loop (bug d80d AC #2).

THE FIX (Phase 1, no wire change). A process-local singleflight registry
(golang ``singleflight`` semantics): a second concurrent caller for the same
``(gate, ticket, resolved-basis-SHA, variant, readonly)`` key ATTACHES to the same
in-flight computation and receives the SAME verdict instead of launching a second
run. The key is purged on completion, so a legitimate re-run after the prior one
finishes proceeds normally; a defensive max-age sweep evicts a wedged/crashed
leader's key so a LATER caller starts a fresh run rather than attaching to a dead
one. (A leader's ``finally`` releases already-attached followers on any exception;
only a hard process kill skips it, and that tears down the followers too — so a
follower cannot outlive its leader in a live process.) Default-on with the
kill-switch ``REBAR_MCP_DEDUP=0``.

WHY THREADS, NOT ASYNCIO. The MCP server runs every synchronous tool body on its own
anyio worker thread (``rebar._mcp_health.offload_sync_tools``), and the certified-tool
in-flight gauge + SIGTERM drain REQUIRE those bodies to stay ``def`` — making a
certified tool ``async def`` trips a fail-loud guard and blinds the drain
(``_mcp_health.instrument_certified_tools``). So the two overlapping gate calls are
concurrent *threads*, and this registry collapses them with a ``threading`` primitive
(a follower blocks on the leader's ``Event``). Keeping the tool bodies sync preserves
the gauge; the registry adds the de-dup underneath it. This is a leaf module (no
``rebar.*`` import at module load) so it never participates in an import cycle.
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

# Defensive ceiling (NOT required for correctness — purge-on-completion is). A changed
# ticket/base resolves to a different SHA => a different key, so a stale key can only
# ever come from a leader that crashed WITHOUT running its finally. 40 min is 2x the
# documented 15-20 min gate duration, so a live run is never swept.
_MAX_AGE_SECONDS: float = 40 * 60


def dedup_enabled() -> bool:
    """Is in-flight de-duplication active? Default-ON; ``REBAR_MCP_DEDUP=0`` disables it.

    The kill-switch is an env read (not a config gate) on purpose: it is a
    break-glass to turn the behaviour change off instantly without a config edit, and
    this leaf module must not import ``rebar.config`` (import-cycle hygiene)."""
    raw = os.environ.get("REBAR_MCP_DEDUP")  # read-via: subsystem-kill-switch
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def new_job_id() -> str:
    """A globally-unique, time-sortable job handle (``{ns-timestamp}-{uuid4hex}``).

    Mirrors ``rebar.llm.workflow.executor.new_run_id`` so the async surface (Phase 2)
    can key its git-ignored ``.rebar/gate_runs/<job_id>`` index the same way
    ``run_workflow`` keys ``.rebar/workflow_runs/<run_id>``."""
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
    """The job_id of the current in-flight run for ``dedup_key``, or ``None``.

    Used by the Phase-2 async surface so a ``*_start`` for an already-running key
    returns the existing handle instead of launching a second run."""
    with _lock:
        hit = _registry.get(dedup_key)
        return hit.job_id if hit is not None and not hit.done else None


def _sweep_locked() -> None:
    """Evict entries older than the max-age ceiling. Caller holds ``_lock``."""
    now = time.monotonic()
    stale = [k for k, v in _registry.items() if now - v.started_monotonic > _MAX_AGE_SECONDS]
    for k in stale:
        _registry.pop(k, None)


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
