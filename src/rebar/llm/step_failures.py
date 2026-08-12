"""Run-scoped tally of LLM step calls that FAILED but did not fail the run (ticket
eclectic-industrial-argali).

**The gap this closes.** Most LLM sub-steps are deliberately non-fatal: the overlap judge
swallows a batch failure to all-abstain, the novelty / contradiction / completion sub-calls
degrade to "score nothing", and so on. Each one logs a warning and nothing else, so a run in
which EVERY overlap batch died emits a verdict byte-identical to a run in which nothing
overlapped. A caller consuming the gate's JSON cannot tell the difference, and repeated
silent degradation stays invisible unless someone scrapes the logs.

**The design.** Every LLM call in the system funnels through one except block —
:meth:`rebar.llm.runner.PydanticAIRunner.run`'s — which already knows the step's call label
and already writes a spend row. This module is the in-memory counterpart of that spend row:
a run-scoped sink the runner records into and the verdict assembler drains, so the count
rides out on ``coverage.llm_step_failures``.

It deliberately mirrors ``review_kernel/telemetry.py``'s contract-violation sink: a
:class:`contextvars.ContextVar` activated once per gate run, a ``record`` that is a silent
no-op outside an active scope, and a destructive ``drain``. It is ADDITIVE observability —
it never changes a verdict, an exit code, a signature, or a provenance stamp, and it never
alters what any swallow site does.

**Layering.** This module imports only the standard library, on purpose. The recording site
lives in the LOW ``rebar.llm.runner`` layer while the draining site lives in the HIGH
plan-review layer; a leaf sink lets both depend on it without the layering inversion
``build_drift.py`` documents (high pushes into low, never the reverse).

**"Non-fatal" needs no classification here.** A fatal failure aborts the run and returns a
degraded verdict that never reaches the drain, so every failure that appears in a produced
verdict is by construction one the run survived. The runner records unconditionally; what
makes the tally "non-fatal" is which runs live long enough to report it.

**Concurrency.** One sink object is shared by every call inside a run — including calls a
batch runner drives concurrently — so the counter increments are taken under a module-level
lock. The lock guards only two integer bumps on an in-memory dict (never I/O, never an LLM
call), so it cannot deadlock a caller and cannot become a contention point.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from collections.abc import Iterator
from typing import Any

_step_failures: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "rebar_llm_step_failures", default=None
)

#: Guards the read-modify-write in :func:`record`; see the module docstring's Concurrency note.
_lock = threading.Lock()


@contextlib.contextmanager
def collect_step_failures() -> Iterator[None]:
    """Activate a run-scoped sink for failed LLM step calls for the dynamic extent of one gate
    run. Nesting reuses the active sink (idempotent); the sink is dropped on exit so a count
    never leaks across runs/tickets."""
    if _step_failures.get() is not None:
        yield
        return
    token = _step_failures.set({})
    try:
        yield
    finally:
        _step_failures.reset(token)


def record(call_label: str) -> None:
    """Count ONE failed LLM step call under ``call_label`` if a sink is active; a no-op outside
    a :func:`collect_step_failures` scope, so unit-testing a runner in isolation neither raises
    nor leaks.

    ``call_label`` is the runner's own label, recorded verbatim so a key matches the
    ``llm call [<label>] ... FAILED`` line an operator would grep for. Never raises: this is
    reporting, and reporting must not be able to fail a run.
    """
    sink = _step_failures.get()
    if sink is None:
        return
    label = call_label or "?"
    with _lock:
        sink[label] = sink.get(label, 0) + 1


def drain() -> dict[str, Any]:
    """Return + clear the tally recorded in the active sink, shaped
    ``{"total": <int>, "by_step": {<label>: <int>}}``.

    Returns an EMPTY dict when nothing was recorded (or when no sink is active), which is what
    lets the caller add the key only when non-empty and keep a clean run's verdict coverage
    byte-identical to before this ticket.
    """
    sink = _step_failures.get()
    if not sink:
        return {}
    with _lock:
        by_step = dict(sink)
        sink.clear()
    return {"total": sum(by_step.values()), "by_step": by_step}
