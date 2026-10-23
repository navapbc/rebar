"""Fast-fail shared concurrency admission for plan-review and completion gates.

ADR 0112 bounds live snapshot bytes and gate RSS before store materialization.
Both CLI and MCP entry points share flocked slots in the snapshot lock directory;
kernel release on close/crash avoids stale leases. A full set raises retryable
``GateCongestedError`` (CLI exit 11), not a verdict, without queueing request
threads. Missing fcntl or an unusable individual slot admits with a
``GATE_ADMISSION_DISARMED`` marker. An unreachable or declared-but-unmounted
scratch volume fails closed before store access; its root directory alone
cannot prove the volume is mounted."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from rebar.llm.errors import GateCongestedError, GateScratchUnavailableError

try:
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent (absent on Windows)
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: The module's public surface: the context manager, the resolved cap and its default, and
#: the two marker tokens an observability probe anchors on. Everything else — the slot files,
#: the ``flock`` wrapper, the marker emitters — is underscore-prefixed and PRIVATE
#: (``docs/api-stability.md``): it is mechanism, and binding to it would make a future change
#: to how slots are counted a compatibility break.
__all__ = [
    "DEFAULT_MAX_CONCURRENT_GATES",
    "DISARMED_MARKER",
    "GATE_MIN_AVAILABLE_MIB",
    "MARKER",
    "available_memory_mib",
    "gate_admission",
    "max_concurrent_gates",
    "scratch_unavailable_detail",
]

#: The line-start journald marker token for a refused admission (AC7). Same convention as
#: ``GATE_PEAK_RSS`` / ``VOTER_ERROR`` / ``MERGE_CHANGE_ERROR``: the containers hold no AWS
#: credentials, so the host probe greps journald and republishes. It is also the portable
#: choice — identical behaviour under the CLI on a laptop with no CloudWatch and no CI
#: provider. A probe counting these must anchor on ``^GATE_CONGESTED \{``, so prose naming
#: the token (including an LLM review of this file) is never counted as an emission.
MARKER = "GATE_CONGESTED"

#: Marker emitted when admission ADMITS because its own plumbing is unusable. A cap that
#: disarms silently is how the incident recurs: the volume fills, and nothing in the record
#: says the bound was not in force. Distinct from :data:`MARKER` so an operator can tell
#: "the host was busy" from "the bound was not running at all". The operator's own ``0``
#: off-switch is NOT a disarm and emits nothing — it is a choice, not a fault.
DISARMED_MARKER = "GATE_ADMISSION_DISARMED"

#: Default to 4 concurrent gates: measured plan-review peak ~748 MB and
#: completion peak ~462 MB; 4 plan reviews use ~2.99 GB of the roughly 3 GB
#: available on the shared 8 GiB host after steady load and Gerrit reserve.
#: A fifth would exceed that budget (ADR 0112).
DEFAULT_MAX_CONCURRENT_GATES = 4

#: The floor, in MiB, of ``MemAvailable`` below which a gate is refused rather
#: than started. It matches ``MCP_MEM_MIN_MB`` in ``infra/scripts/autodeploy.sh``:
#: both shed load before starting a heavyweight overlap on this host.
GATE_MIN_AVAILABLE_MIB = 1024

_MEMINFO = Path("/proc/meminfo")

_SLOT_PREFIX = "gate-slot-"

#: Declaration lives beside the scratch mount on root; proof lives inside
#: the volume. Declaration without proof means an unmounted dedicated volume
#: and must fail closed before mkdir can recreate the store on root. Other
#: marker combinations retain current behavior for unprovisioned hosts.
#: Files make provisioning the source of truth (ADR 0112; story aa40).
_SCRATCH_REQUIRED_MARKER = ".gate-scratch-required"
_SCRATCH_MOUNTED_MARKER = ".gate-scratch-mounted"
_CANONICAL_GATE_SCRATCH_MOUNT = Path("/var/lib/rebar/gate-scratch")


def _test_slot_namespace() -> str | None:
    from rebar._config_resolvers import resolve_pytest_xdist_worker

    worker = resolve_pytest_xdist_worker()
    if not worker:
        return None
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in worker)
    return f"pytest-{safe}"


def max_concurrent_gates(repo_root: str | os.PathLike[str] | None = None) -> int:
    """The configured concurrent-gate cap: ``[snapshot].max_concurrent_gates`` > default.

    ``0`` disables the bound entirely — the off-switch idiom ``max_bytes`` / ``max_entries``
    already use. A malformed or negative value falls back to the default rather than
    crashing every gate on the host.
    """
    from rebar._config_resolvers import resolve_gate_max_concurrent

    return resolve_gate_max_concurrent(DEFAULT_MAX_CONCURRENT_GATES, repo_root)


def scratch_unavailable_detail() -> str | None:
    """Return the shared scratch-volume failure detail, or None.

    The gate and review bot use this side-effect-free two-marker check before
    writing snapshots or clones; observability reads the same proof. The root-side
    declaration survives unmount, while the volume-side proof disappears. Guard
    the declared host regardless of the caller's temp path. With neither a
    declaration nor canonical dedicated path, ordinary laptops and CI stay
    unaffected (ADR 0112; story aa40)."""
    from rebar._snapshot.repo_snapshot import peek_store_root

    base = peek_store_root().parent
    declared = (base.parent / _SCRATCH_REQUIRED_MARKER).is_file()
    configured_dedicated_path = base == _CANONICAL_GATE_SCRATCH_MOUNT
    if not (declared or configured_dedicated_path):
        return None
    if (base / _SCRATCH_MOUNTED_MARKER).is_file():
        return None
    if configured_dedicated_path and not declared:
        return (
            f"{base} is configured as the dedicated scratch volume but "
            f"{_SCRATCH_REQUIRED_MARKER} and {_SCRATCH_MOUNTED_MARKER} are absent, so the "
            "volume was not mounted or marked"
        )
    return (
        f"{base} declares a dedicated scratch volume ({base.parent / _SCRATCH_REQUIRED_MARKER}) "
        f"but {_SCRATCH_MOUNTED_MARKER} is absent, so the volume is not mounted"
    )


def available_memory_mib() -> int | None:
    """Host ``MemAvailable`` in MiB, or ``None`` when the platform has no ``/proc/meminfo``.

    Missing ``/proc/meminfo`` is inapplicable (for example macOS); an existing but
    unparseable file is a broken Linux memory probe and is reported by the caller.
    """
    try:
        text = _MEMINFO.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1]) // 1024
            break
    raise ValueError("/proc/meminfo exposes no parseable MemAvailable field")


def _emit_memory_low(
    gate: str,
    ticket_id: str,
    available_mib: int,
    repo_root: str | os.PathLike[str] | None,
) -> None:
    """Announce a memory refusal on the existing congestion token."""
    _emit(
        MARKER,
        gate,
        ticket_id,
        limit=max_concurrent_gates(repo_root),
        reason="memory_low",
        available_mib=available_mib,
        floor_mib=GATE_MIN_AVAILABLE_MIB,
    )


def _require_available_memory(
    gate: str,
    ticket_id: str,
    repo_root: str | os.PathLike[str] | None,
) -> None:
    """Refuse when the host cannot spare memory for another gate.

    This is a precondition, not a reservation: it composes with the slot counter by
    rejecting the case where the host is already in the low-memory band before the
    next gate allocates its snapshot and review-clone working set.
    """
    try:
        available = available_memory_mib()
    except (OSError, ValueError) as exc:
        _emit_admission_disarmed(gate, ticket_id, f"MemAvailable unreadable: {type(exc).__name__}")
        return
    if available is None:
        return
    if available < GATE_MIN_AVAILABLE_MIB:
        _emit_memory_low(gate, ticket_id, available, repo_root)
        raise GateCongestedError(
            gate,
            max_concurrent_gates(repo_root),
            available_mib=available,
            floor_mib=GATE_MIN_AVAILABLE_MIB,
        )


def _require_scratch_volume(gate: str) -> None:
    """Raise :class:`GateScratchUnavailableError` when the shared predicate says the declared
    scratch volume is not mounted.

    Runs BEFORE :func:`_slot_dir`. That ordering is the whole point: the creating
    :func:`store_root` would materialise the store on the root filesystem on its way to the
    refusal, so checking afterwards would report the fault having already caused it.
    """
    detail = scratch_unavailable_detail()
    if detail is not None:
        raise GateScratchUnavailableError(gate, detail)


def _slot_dir() -> Path:
    """Directory holding the admission slot files — the snapshot store's ``locks/``.

    Reusing the store's own lock directory (rather than a new tree) is what makes the
    counter follow ``REBAR_GATE_TMPDIR`` onto a dedicated gate-scratch volume for free.
    """
    from rebar._snapshot.repo_snapshot import store_root

    d = store_root() / "locks"
    namespace = _test_slot_namespace()
    if namespace is not None:
        d = d / namespace
    d.mkdir(parents=True, exist_ok=True)
    return d


def _open_slot(path: Path) -> int | None:
    """Open ``path`` and take its exclusive non-blocking ``flock``, or return ``None``.

    ``None`` means "another holder has it" — the whole point is that this NEVER waits. An
    ``OSError`` from the open itself is deliberately NOT swallowed into ``None``: an
    unwritable store is a plumbing failure that must fail OPEN at the caller, not a full
    slot that must refuse a gate.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _acquire_slot(limit: int, directory: Path) -> int | None:
    """Take the first free slot below ``limit`` and return its fd, else ``None``.

    Scanning from 0 packs holders into the low slots, so the file set a host ever creates
    is bounded by the highest cap it has run with rather than by its total gate count.
    Raises ``OSError`` if a slot file cannot be opened at all; the caller supplies the
    directory, having already decided what an unreachable store root means.
    """
    for index in range(limit):
        fd = _open_slot(directory / f"{_SLOT_PREFIX}{index}")
        if fd is not None:
            return fd
    return None


def _release_slot(fd: int) -> None:
    """Drop a held slot. Closing the fd alone releases the ``flock``; the explicit unlock
    is kept so the release is visible at the call site and does not depend on close order."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - the close below still releases it
        logger.debug("gate admission slot unlock failed", exc_info=True)
    with contextlib.suppress(OSError):
        os.close(fd)


def _emit(token: str, gate: str, ticket_id: str, **fields: object) -> None:
    """Write one line-start ``<token> {json}`` marker. Best-effort: never raises.

    One emitter for both markers so they cannot drift apart in shape — a probe anchors on
    the token at line start and parses the rest as JSON either way.
    """
    try:
        body = json.dumps(
            {
                "event": token,
                "gate": gate,
                "ticket_id": ticket_id,
                "timestamp": time.time(),
                **fields,
            }
        )
        logger.info(body)
        print(token + " " + body, file=sys.stderr, flush=True)  # noqa: T201 — journald marker
    except Exception:  # instrumentation must never fail the decision it reports
        logger.debug("admission marker %s not emitted", token, exc_info=True)


def _emit_gate_congested(gate: str, ticket_id: str, limit: int) -> None:
    """Announce a refusal (AC7). Congestion visible only as a client-side error is
    congestion nobody can see in aggregate."""
    _emit(MARKER, gate, ticket_id, limit=limit)


def _emit_admission_disarmed(gate: str, ticket_id: str, reason: str) -> None:
    """Announce that the cap was NOT in force for this run, and why (AC8)."""
    _emit(DISARMED_MARKER, gate, ticket_id, reason=reason)


@contextlib.contextmanager
def gate_admission(
    gate: str, ticket_id: str, repo_root: str | os.PathLike[str] | None = None
) -> Iterator[None]:
    """Hold one concurrency slot for the wrapped gate run, or FAST-FAIL immediately.

    Raises :class:`GateCongestedError` without waiting when every slot is taken. The slot is
    released from a ``finally``, so a gate that RAISES — the OOM- and ENOSPC-adjacent
    failures this bound exists for — gives its slot back; a leak on the error path would
    degrade the cap into a deadlock that only a reboot clears.
    """
    # BEFORE every other branch, including the operator's `0` off-switch and the fcntl
    # disarm: those disarm the CONCURRENCY bound, which is a different question from whether
    # gate bytes are about to land on the root filesystem. A host that turned the counter off
    # did not thereby consent to losing its scratch volume silently.
    _require_scratch_volume(gate)
    # Also before the counter's ``0`` off-switch: disabling the concurrency bound is a
    # different decision from starting gates into an exhausted host.
    _require_available_memory(gate, ticket_id, repo_root)
    limit = max_concurrent_gates(repo_root)
    if limit <= 0:
        yield  # the operator's explicit off switch — a choice, not a fault: no marker
        return
    if fcntl is None:
        _emit_admission_disarmed(gate, ticket_id, "advisory locking (fcntl) unavailable")
        yield
        return
    try:
        directory = _slot_dir()
    except OSError as exc:
        # Fail CLOSED (ADR 0112): an unreachable scratch store must not silently send gate
        # bytes back to the root filesystem. No marker — a raised refusal already reaches
        # the caller and its log; markers exist for outcomes that would leave no trace.
        raise GateScratchUnavailableError(gate, f"{type(exc).__name__}: {exc}") from exc
    try:
        fd = _acquire_slot(limit, directory)
    except OSError as exc:
        # Fail OPEN — but say so. The store root is reachable; one slot file is not, which
        # is a local fault, not the volume failure the ADR fails closed on.
        _emit_admission_disarmed(gate, ticket_id, f"slot file unusable: {type(exc).__name__}")
        yield
        return
    if fd is None:
        _emit_gate_congested(gate, ticket_id, limit)
        raise GateCongestedError(gate, limit)
    try:
        yield
    finally:
        _release_slot(fd)
