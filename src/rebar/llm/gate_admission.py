"""Bounded gate concurrency with FAST-FAIL admission (story 09da-343c-1ee9-480c).

ADR 0112 decision 5. The snapshot janitor reclaims by high-water mark and relies on POSIX
delete-on-last-close: it can ``unlink`` an in-flight snapshot, but the blocks are not
returned until the last reader closes. So at peak concurrency the bytes a running gate
holds are **LIVE, not garbage**, and a disk cap set below peak concurrent hold cannot be
honoured by reclamation — the janitor runs, evicts what it may, and the volume still fills.
Bounding how many holders there can be is therefore a PRECONDITION for the per-accumulator
caps, not an optimization. The same counter also bounds concurrent gate RSS, which is the
other budget the review host runs out of (~748 MB peak per plan-review on an 8 GiB box).

WHY THIS SEAM. ``rebar.llm.peak_rss.gate_peak_rss`` already wraps exactly this scope and
states the reason: wrapping the library entry points ``review_plan`` / ``verify_completion``
"covers both call paths at one seam, since both reach the gate through these functions".
Admission is that sibling — entered OUTSIDE the snapshot handle resolution, because it is
the snapshot materialization (and the review-bot's per-review clone) that spends the bytes.
One wrapper bounds the in-process MCP daemon path and separate-process CLI runs against ONE
counter shared by both gates; two counters of N each would admit 2N holders, which is
precisely the bound that was needed.

WHY THE REFUSAL IS AN ``LLMError``. :class:`~rebar.llm.errors.GateCongestedError` subclasses
``LLMError`` and carries a retryable ``LLMOutcome``, so the ``except LLMError`` arms the MCP
tool bodies and the CLI gate handlers ALREADY have route it — a structured
``retryable: true`` payload over MCP, exit 11 ("transient — retry") on the CLI — with no new
except clause anywhere. It is never a verdict VALUE, so congestion cannot be read as
INDETERMINATE, BLOCK or FAIL.

WHY FAST-FAIL AND NOT A QUEUE. A queued gate still holds its thread and its resident memory
while it waits, so a queue converts disk pressure into memory pressure — and it holds the
MCP client's request open past its ~60 s deadline, producing the ``-32001`` ambiguity the
async ``*_start`` + poll surface exists to avoid. A queue is also itself unbounded storage
of the thing being bounded. A refusal is legible: it lets a client back off.

WHY FLOCK'D SLOT FILES AND NOT A LEASE. ADR 0005 and ``rebar._snapshot.janitor`` record
that a PID+heartbeat lease was spiked and REJECTED as unsound (N readers per entry, PID
reuse, crash-stale leases). A ``flock`` needs no heartbeat: the kernel drops it when the
holder dies or its fd closes, so a crashed gate cannot permanently consume a slot. It also
serialises correctly between threads of ONE process, because a ``flock`` is held by the open
file description and each acquisition opens its own. The slots live inside the snapshot
store's existing ``locks/`` directory, so they inherit ``REBAR_GATE_TMPDIR`` and follow gate
scratch onto ADR 0112 decision 3's dedicated volume without further plumbing.

DEGRADATION IS SPLIT, and the split is the ADR's, not a preference. A missing ``fcntl`` or a
single unusable slot file inside an otherwise-healthy ``locks/`` directory ADMITS and emits a
``GATE_ADMISSION_DISARMED`` marker: bricking every gate on the host over a platform gap is
worse than the pressure the bound relieves, and the free-space floor still applies — but a
cap that disarms SILENTLY is how the incident recurs, so it is never silent. An unreachable
STORE ROOT is the opposite case and fails CLOSED, because ADR 0112 says so in as many words:
admission must treat a scratch volume that is unmounted "not as an empty cache to repopulate
onto the root filesystem — otherwise the volume's failure mode is silently reverting to the
state this ADR exists to prevent". A malformed knob is handled one layer down, in the
resolver, by falling back to the measured default rather than disarming at all.
"""

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
    "MARKER",
    "gate_admission",
    "max_concurrent_gates",
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

#: Concurrent gate executions admitted by default. DERIVED FROM MEASUREMENT, not guessed.
#: A complete plan-review peaks at ~748 MB resident and a completion-verifier at ~462 MB
#: (``GATE_PEAK_RSS`` markers; ADR 0112 cites ~739 MB over ~4.7 min, and a degraded run at
#: 501 MB, so the figure varies materially run to run). The review host is a ``t4g.large``
#: (8 GiB) shared with Gerrit, and ADR 0112 decision 7 forbids growing it: steady state is
#: ~2.17 GB and Gerrit reserves ~3 GiB by config (``heapLimit 2g`` + ``packedGitLimit 1g``),
#: leaving roughly 3 GiB for gate work. 4 x 748 MB = ~2.99 GB fits inside that; a fifth
#: concurrent run (~3.74 GB) does not. It is also decisively below the operator's observed
#: ~10 concurrent plan reviews — which is the point, since a cap at or above observed peak
#: sheds no load and bounds nothing.
DEFAULT_MAX_CONCURRENT_GATES = 4

_SLOT_PREFIX = "gate-slot-"


def max_concurrent_gates(repo_root: str | os.PathLike[str] | None = None) -> int:
    """The configured concurrent-gate cap: ``[snapshot].max_concurrent_gates`` > default.

    ``0`` disables the bound entirely — the off-switch idiom ``max_bytes`` / ``max_entries``
    already use. A malformed or negative value falls back to the default rather than
    crashing every gate on the host.
    """
    from rebar._config_resolvers import resolve_gate_max_concurrent

    return resolve_gate_max_concurrent(DEFAULT_MAX_CONCURRENT_GATES, repo_root)


def _slot_dir() -> Path:
    """Directory holding the admission slot files — the snapshot store's ``locks/``.

    Reusing the store's own lock directory (rather than a new tree) is what makes the
    counter follow ``REBAR_GATE_TMPDIR`` onto a dedicated gate-scratch volume for free.
    """
    from rebar._snapshot.repo_snapshot import store_root

    d = store_root() / "locks"
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
