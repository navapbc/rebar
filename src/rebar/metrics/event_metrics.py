"""Agent-process metrics derived from the raw ticket event store (ticket 18e6).

These are *read-only* derivations over the rebar ticket event store. Unlike
:func:`rebar.reduce_ticket` — which collapses a ticket's event log into a single
terminal-state dict — the agent-process metrics need to see the *sequence* of
raw events (repeated claim sessions, reopen/close edges, revert records). There
is no ordered-event API, so these functions read the on-disk event files
directly: the tracker dir is ``rebar.config.tracker_dir(repo_root)``, each ticket
is a subdirectory named by ticket id, and event files are named
``<ts_ns>-<uuid>-<EVENT_TYPE>.json`` carrying the envelope
``{"event_type","timestamp","uuid","env_id","author","data"}``. Compaction may
retire a folded event to a ``*.json.retired`` tombstone; where a metric must see
the full history (claim sessions, status edges) we include the retired files too.

The four tested derivations:

- :func:`attempts_per_ticket` — distinct ``open -> in_progress`` claim sessions.
- :func:`rework_within_days` — tickets reopened then re-closed within ``n`` days.
- :func:`revert_recovery` — tickets with a substantive (STATUS/COMMITS) revert.
- :func:`reopen_recovery` / :func:`first_pass_rate` — small companion signals.

Each is registered into the c085 :data:`~rebar.metrics.registry.REGISTRY` via a
single-arg *context adapter* (c085's ``MetricSpec.compute`` is
``Callable[[context], value | None]``): the adapter pulls ``repo_root`` / range
off the context object and calls the multi-arg derivation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import rebar
from rebar.config import tracker_dir
from rebar.metrics.registry import REGISTRY, MetricSpec
from rebar.reducer._cache import is_active_event
from rebar.reducer._sort import event_sort_key

# ---------------------------------------------------------------------------
# On-disk event-store access helpers.
# ---------------------------------------------------------------------------

_NS_PER_DAY = 86_400 * 1_000_000_000


def _ticket_dirs(repo_root: Any) -> list[str]:
    """Absolute paths of every ticket subdirectory under the tracker dir.

    Returns an empty list when the tracker dir does not exist yet.
    """

    root = str(tracker_dir(repo_root))
    if not os.path.isdir(root):
        return []
    out: list[str] = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isdir(path):
            out.append(path)
    return out


def _event_files(
    ticket_dir: str,
    event_type: str,
    *,
    include_retired: bool = True,
) -> list[str]:
    """Chronologically sorted event-file paths of one type within a ticket dir.

    Matches ``*-<EVENT_TYPE>.json`` and (when ``include_retired``) the folded
    ``*-<EVENT_TYPE>.json.retired`` tombstones; ``*-SNAPSHOT.json`` is skipped.
    Ordering reuses :func:`rebar.reducer._sort.event_sort_key`, whose integer
    ns-timestamp prefix dominates and gives a stable chronological order across
    both active and retired files.
    """

    active = f"-{event_type}.json"
    retired = f"-{event_type}.json.retired"
    paths: list[str] = []
    for name in os.listdir(ticket_dir):
        if name.endswith(active):
            paths.append(os.path.join(ticket_dir, name))
        elif include_retired and name.endswith(retired) and not is_active_event(name):
            paths.append(os.path.join(ticket_dir, name))
    paths.sort(key=event_sort_key)
    return paths


def _load(path: str) -> dict[str, Any]:
    """Parse one event envelope from disk."""

    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _has_any_event(ticket_dir: str) -> bool:
    """True when the ticket dir holds at least one non-snapshot event file."""

    for name in os.listdir(ticket_dir):
        if name.endswith("-SNAPSHOT.json"):
            continue
        if name.endswith(".json") or name.endswith(".json.retired"):
            return True
    return False


# ---------------------------------------------------------------------------
# Range parsing (ISO date/datetime bounds over ns-epoch timestamps).
# ---------------------------------------------------------------------------


def _to_ns(dt: datetime) -> int:
    """Whole-second epoch ns for a datetime (naive treated as UTC)."""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1_000_000_000 + dt.microsecond * 1000


def _bounds(since: str | None, until: str | None) -> tuple[int | None, int | None]:
    """Resolve ``since``/``until`` ISO strings to (lower_inclusive, upper_exclusive) ns.

    Date-only bounds are treated as day bounds: ``since`` is the start of its
    day (inclusive) and ``until`` spans the whole day (exclusive upper = start
    of the following day). A bound carrying a time component is taken literally,
    with the upper bound made inclusive to the microsecond.
    """

    lo: int | None = None
    hi: int | None = None
    if since is not None:
        lo = _to_ns(datetime.fromisoformat(since))
    if until is not None:
        dt = datetime.fromisoformat(until)
        if "T" not in until and ":" not in until:
            hi = _to_ns(dt + timedelta(days=1))
        else:
            hi = _to_ns(dt) + 1
    return lo, hi


def _in_range(ts: Any, lo: int | None, hi: int | None) -> bool:
    """True when ns timestamp ``ts`` lies within [lo, hi); ``None`` bounds are open."""

    if not isinstance(ts, int):
        return False
    if lo is not None and ts < lo:
        return False
    if hi is not None and ts >= hi:
        return False
    return True


# ---------------------------------------------------------------------------
# Derivations (the oracle's direct targets).
# ---------------------------------------------------------------------------


def attempts_per_ticket(
    repo_root: Any,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, int]:
    """Per-ticket count of DISTINCT claim sessions.

    For each ticket, scans raw ``*-STATUS.json`` files (including retired ones),
    keeps only the ``open -> in_progress`` claim edge
    (``data.current_status == "open" and data.status == "in_progress"``) whose
    event timestamp falls within the range, and counts the distinct
    ``data.session`` values seen. Tickets with no such edge are omitted.
    """

    lo, hi = _bounds(since, until)
    result: dict[str, int] = {}
    for ticket_dir in _ticket_dirs(repo_root):
        sessions: set[Any] = set()
        for path in _event_files(ticket_dir, "STATUS"):
            event = _load(path)
            data = event.get("data") or {}
            if data.get("current_status") == "open" and data.get("status") == "in_progress":
                if _in_range(event.get("timestamp"), lo, hi):
                    sessions.add(data.get("session"))
        if sessions:
            result[os.path.basename(ticket_dir)] = len(sessions)
    return result


def rework_within_days(
    repo_root: Any,
    n: int,
    since: str | None = None,
    until: str | None = None,
) -> int | None:
    """Count tickets reopened (``closed -> open``) then re-closed within ``n`` days.

    Measured from the raw STATUS event ``timestamp`` fields (ns-epoch ints): a
    ticket counts when a ``closed -> open`` edge is followed by a later
    ``-> closed`` edge no more than ``n`` days afterwards. Returns ``None`` when
    there are NO tickets in the store/range at all (Unavailable); returns ``0``
    when tickets exist but none were reworked (a measured zero).
    """

    lo, hi = _bounds(since, until)
    window_ns = n * _NS_PER_DAY
    present = False
    reworked = 0
    for ticket_dir in _ticket_dirs(repo_root):
        edges: list[tuple[int, dict[str, Any]]] = []
        ticket_present = False
        for path in _event_files(ticket_dir, "STATUS"):
            event = _load(path)
            ts = event.get("timestamp")
            if not isinstance(ts, int) or not _in_range(ts, lo, hi):
                continue
            ticket_present = True
            edges.append((ts, event.get("data") or {}))
        # A ticket with any in-range event at all counts as "present"; STATUS
        # events dominate but a ticket may exist via other event types too.
        if ticket_present or (_has_any_event(ticket_dir) and lo is None and hi is None):
            present = True
        if _ticket_reworked(edges, window_ns):
            reworked += 1
    if not present:
        return None
    return reworked


def _ticket_reworked(edges: list[tuple[int, dict[str, Any]]], window_ns: int) -> bool:
    """True when a ``closed -> open`` edge is followed by a ``-> closed`` within the window."""

    for i, (reopen_ts, data) in enumerate(edges):
        if data.get("current_status") == "closed" and data.get("status") == "open":
            for close_ts, later in edges[i + 1 :]:
                if later.get("status") == "closed" and 0 <= close_ts - reopen_ts <= window_ns:
                    return True
    return False


def revert_recovery(
    repo_root: Any,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """Count tickets with at least one SUBSTANTIVE revert (STATUS or COMMITS target).

    Reads each ticket's reduced ``state["reverts"]`` via
    :func:`rebar.reduce_ticket` and counts a ticket when any of its reverts
    targets a ``STATUS`` or ``COMMITS`` event — i.e. a revert of substance,
    never a revert of a COMMENT/LINK. When a range is supplied, reverts are
    filtered by their own event ``timestamp``.
    """

    lo, hi = _bounds(since, until)
    count = 0
    for ticket_dir in _ticket_dirs(repo_root):
        state = rebar.reduce_ticket(ticket_dir, include_retired=True)
        if not state:
            continue
        for revert in state.get("reverts") or []:
            if since is not None or until is not None:
                if not _in_range(revert.get("timestamp"), lo, hi):
                    continue
            if revert.get("target_event_type") in ("STATUS", "COMMITS"):
                count += 1
                break
    return count


def reopen_recovery(
    repo_root: Any,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """Count tickets that carry at least one ``closed -> open`` reopen edge."""

    lo, hi = _bounds(since, until)
    count = 0
    for ticket_dir in _ticket_dirs(repo_root):
        for path in _event_files(ticket_dir, "STATUS"):
            event = _load(path)
            data = event.get("data") or {}
            if data.get("current_status") == "closed" and data.get("status") == "open":
                if _in_range(event.get("timestamp"), lo, hi):
                    count += 1
                    break
    return count


def first_pass_rate(
    repo_root: Any,
    since: str | None = None,
    until: str | None = None,
) -> float | None:
    """Fraction of tickets claimed exactly once (no re-claim attempts).

    Returns ``None`` when no ticket has any claim in the range.
    """

    attempts = attempts_per_ticket(repo_root, since, until)
    if not attempts:
        return None
    single = sum(1 for c in attempts.values() if c == 1)
    return single / len(attempts)


# ---------------------------------------------------------------------------
# c085 registry integration — single-arg context adapters.
# ---------------------------------------------------------------------------

_ACCRUING_SINCE = "2026-01-01T00:00:00+00:00"


def _ctx_repo_root(ctx: Any) -> Any:
    return getattr(ctx, "repo_root", None)


def _ctx_since(ctx: Any) -> Any:
    return getattr(ctx, "since", None)


def _ctx_until(ctx: Any) -> Any:
    return getattr(ctx, "until", None)


def _spec(metric_id: str, fn: Any, *, n: int | None = None) -> MetricSpec:
    """Build a MetricSpec whose single-arg ``compute`` adapts to the c085 context."""

    if n is None:

        def compute(ctx: Any) -> Any:
            if ctx is None:
                return None
            return fn(_ctx_repo_root(ctx), _ctx_since(ctx), _ctx_until(ctx))

    else:

        def compute(ctx: Any) -> Any:
            if ctx is None:
                return None
            return fn(_ctx_repo_root(ctx), n, _ctx_since(ctx), _ctx_until(ctx))

    return MetricSpec(
        id=metric_id,
        lens="agent_process",
        source="structural",
        confidence="high",
        compute=compute,
        accruing_since=_ACCRUING_SINCE,
    )


def register() -> None:
    """Append this module's specs to the c085 REGISTRY (idempotent on id)."""

    existing = {spec.id for spec in REGISTRY}
    specs = [
        _spec("attempts_per_ticket", attempts_per_ticket),
        _spec("rework_within_7_days", rework_within_days, n=7),
        _spec("revert_recovery", revert_recovery),
        _spec("reopen_recovery", reopen_recovery),
        _spec("first_pass_rate", first_pass_rate),
    ]
    for spec in specs:
        if spec.id not in existing:
            REGISTRY.append(spec)
            existing.add(spec.id)


register()
