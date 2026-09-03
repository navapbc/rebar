"""Peak-RSS measurement for a gate run (bug 9ea3-7d07-ea55-4496).

MEASUREMENT ONLY. On 2026-09-02 the mcp container was OOM-killed roughly three minutes
into a plan-review gate run on an 8 GiB box it shares with Gerrit, the review-bot and
opcert. Gate work runs IN-PROCESS on a daemon thread inside the MCP server
(:func:`rebar._mcp_llm._spawn_gate_daemon`), so gate memory *is* server memory — and
nobody has ever measured what one gate run actually costs. Every candidate remedy
(a container ``mem_limit``, a larger instance, a narrower LLM fan-out) needs that number
first, so this module produces it and stops there. It sets no limit and enforces nothing.

WHY A LOG MARKER AND NOT A METRIC. The host probe (``infra/scripts/observability.sh``)
is the only thing on the box with AWS credentials: the containers deliberately do not get
them, because the IMDS hop limit constrains in-container metadata access — the same reason
``VOTER_ERROR`` / ``MERGE_CHANGE_ERROR`` are journald markers that the host probe greps
and republishes. A marker therefore needs no new credential path, no boto3 dependency in
the gate hot path, and no CI provider; it works identically under the CLI on a laptop,
where CloudWatch does not exist at all. The convention is copied exactly from
:func:`rebar.review_bot.voter_merge.merge_change_error`: ONE line-start
``GATE_PEAK_RSS {json}`` print to stderr (the single emission a ``^GATE_PEAK_RSS \\{``
anchor would count), plus a logger copy of the JSON body WITHOUT the token, so configured
application logging can never double a future count (bug f829-152a-b415-44a4).

``ru_maxrss`` UNITS DIFFER BY PLATFORM and the difference is a factor of 1024, which is
exactly large enough to look like a plausible reading either way: Linux (and the box)
reports KiB, while macOS/BSD reports BYTES. :func:`ru_maxrss_to_bytes` is the whole of
that decision, kept as a pure function so both branches are directly testable without a
platform to run them on.

The measurement is HIGH-WATER, PROCESS-WIDE and MONOTONIC: ``RUSAGE_SELF`` reports the
peak for the whole process since it started, not for this gate alone. In the CLI that is
effectively the run's own cost; in the long-lived MCP server it is "the largest this
server has ever been", so ``peak_delta_bytes`` (the rise across the gate) is reported
alongside it and is the number that attributes cost to THIS run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import time
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

#: The line-start journald marker token. An observability probe that later counts these
#: must anchor on ``^GATE_PEAK_RSS \{`` (bug 8c2f-8377-5044-4650): prose naming the token
#: — including an LLM review of this very file — must never be counted as an emission.
MARKER = "GATE_PEAK_RSS"

_BYTES_PER_KIB = 1024


def ru_maxrss_to_bytes(ru_maxrss: int, platform: str) -> int:
    """Convert a raw ``ru_maxrss`` to bytes for ``platform`` (a ``sys.platform`` string).

    macOS and the BSDs report ``ru_maxrss`` in BYTES; Linux reports it in KIBIBYTES. Getting
    this wrong is a silent 1024x error in either direction, so the platform test is explicit
    and lives in one pure function rather than at the call site."""
    if platform == "darwin" or platform.startswith(("freebsd", "openbsd", "netbsd", "dragonfly")):
        return int(ru_maxrss)
    return int(ru_maxrss) * _BYTES_PER_KIB


def peak_rss_bytes() -> int | None:
    """Process-wide peak RSS in bytes, or ``None`` where ``resource`` is unavailable.

    ``resource`` is a Unix-only stdlib module (absent on Windows), and this is
    instrumentation: it must degrade to "no measurement" rather than break a gate run."""
    try:
        import resource
    except ImportError:  # pragma: no cover — Unix-only module; absent on Windows
        return None
    return ru_maxrss_to_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, sys.platform)


def emit_gate_peak_rss(gate: str, ticket_id: str, record: dict[str, Any]) -> None:
    """Write one ``GATE_PEAK_RSS`` marker. Best-effort: never raises into the gate."""
    try:
        body = json.dumps({"event": MARKER, "gate": gate, "ticket_id": ticket_id, **record})
        logger.info(body)
        print(MARKER + " " + body, file=sys.stderr, flush=True)  # noqa: T201 — journald marker
    except Exception:  # instrumentation must never fail the run it measures
        logger.debug("peak-rss marker not emitted", exc_info=True)


@contextlib.contextmanager
def gate_peak_rss(gate: str, ticket_id: str) -> Iterator[None]:
    """Emit a ``GATE_PEAK_RSS`` marker when the wrapped gate run completes.

    Emitted from a ``finally`` so a gate that RAISES — which includes the OOM-adjacent
    failures this instrumentation exists for — still reports what it had grown to. Wrapping
    the library entry points (``review_plan`` / ``verify_completion``) rather than the MCP
    daemon and the CLI separately covers both call paths at one seam, since both reach the
    gate through these functions."""
    started_ns = time.monotonic_ns()
    before = peak_rss_bytes()
    try:
        yield
    finally:
        after = peak_rss_bytes()
        emit_gate_peak_rss(
            gate,
            ticket_id,
            {
                "peak_rss_bytes": after,
                # The rise ACROSS this run. ru_maxrss is a process-wide high-water mark, so
                # in the long-lived MCP server ``peak_rss_bytes`` alone attributes every
                # previous run's cost to this one; the delta is what this run added.
                "peak_delta_bytes": (None if after is None or before is None else after - before),
                "elapsed_ms": (time.monotonic_ns() - started_ns) // 1_000_000,
                "timestamp": time.time(),
            },
        )
