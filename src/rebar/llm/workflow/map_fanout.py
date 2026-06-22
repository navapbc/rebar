"""Bounded-concurrent map fan-out — the ONE narrow relaxation of the Burr tripwire.

The executor + interpreter are deliberately synchronous and single-threaded (the
``test_executor_tripwire.py`` tripwire bans ``asyncio`` / ``concurrent.futures`` /
``threading`` / ``multiprocessing`` / retry libs in those two modules). This module
is the SOLE, DELIBERATE exception, confined to the ``map`` fan-out path.

RECORDED RATIONALE (why the relaxation is justified HERE and nowhere else):
  * Map iterations are I/O-bound LLM calls and are ORDER-INDEPENDENT BY CONSTRUCTION
    — each runs in its own iteration-keyed frame (``M#0/…``, ``M#1/…``), there is no
    cross-iteration ``needs`` edge, and each writes only its own frame keys. So
    running them concurrently changes nothing about WHAT is computed.
  * Replay determinism is preserved because correctness rests on the iteration-keyed
    idempotency markers, NOT on execution order: the reducer is last-writer-wins per
    frame key, so whatever order the commits land in, every clone converges to the
    same final per-key state (the dbc6 discipline).
  * Only the AGENT CALL overlaps. EVERY event commit and shared-run-state mutation is
    serialized through ``rc.lock`` (held in :func:`rebar.llm.workflow.interpreter`'s
    ``_run_leaf`` / ``_maybe_record_control`` around the recorder + ``rc.outputs`` /
    ``rc.statuses`` writes), so the append-only event log is written one event at a
    time exactly as in the serial path — the store never sees concurrent commits.
  * The concurrency is BOUNDED (``max_concurrency`` from the IR, default 1 = serial),
    so the fan-out cannot spawn an unbounded thread storm.

The async/threading ban stays in force everywhere else; this module is excluded from
the tripwire by name, and a test asserts it carries this rationale.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .interpreter import _map_iteration, _RunCtx


def run_concurrent_map(
    rc: _RunCtx,
    body: list,
    cur: str,
    sid: str,
    prefixes: tuple[str, ...],
    bindings: Any,
    as_name: Any,
    index_var: Any,
    collection: list,
    bound: int,
) -> None:
    """Run a map's iterations with BOUNDED concurrency. Parallel agent calls;
    serialized commits (every iteration's ``_run_leaf`` guards its recorder + state
    writes with ``rc.lock``, which this function installs for the fan-out). Mutates
    ``rc`` in place (outputs/statuses/markers) exactly as the serial loop would; the
    caller records the map's own completion marker once all iterations finish.

    A failure in any iteration sets ``rc.failed`` (checked by ``_execute_frame`` at
    each step), so already-queued iterations that have not started early-return;
    in-flight iterations are allowed to finish (their markers are durable) before this
    returns, leaving the event log in a consistent, replay-able state.
    """
    # Install a single shared mutex for the duration of the fan-out (reused by a
    # nested map so commits stay globally serialized). ``threading.Lock`` is a context
    # manager, which is exactly what interpreter._guard expects.
    created = rc.lock is None
    if created:
        rc.lock = threading.Lock()
    try:
        with ThreadPoolExecutor(max_workers=bound, thread_name_prefix=f"rebar-map-{sid}") as pool:
            futures = [
                pool.submit(
                    _map_iteration,
                    rc,
                    body,
                    cur,
                    sid,
                    prefixes,
                    bindings,
                    as_name,
                    index_var,
                    j,
                    item,
                )
                for j, item in enumerate(collection)
            ]
            # Wait for ALL iterations (never cancel mid-flight — a started iteration's
            # effect + marker must complete so recovery is clean). A normal step failure
            # is captured as DATA on ``rc`` inside _execute_frame (it sets rc.failed,
            # which makes queued-but-unstarted iterations early-return at the top of
            # _execute_frame), not raised. An UNEXPECTED exception (a real bug in a step
            # body) is surfaced by re-raising the first one here — but note it does NOT
            # set rc.failed, so sibling iterations already submitted run to completion
            # before it propagates. Either way the store is left consistent + replayable.
            for f in as_completed(futures):
                f.result()
    finally:
        if created:
            rc.lock = None
