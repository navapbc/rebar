"""Count LLM calls that failed in a run that still produced a verdict.

A run-scoped context sink lets the low-level runner record failures and the
plan-review assembler drain them into coverage.llm_step_failures. Recording
outside an active run is a no-op. This tally adds observability without changing
the verdict or a swallowed error. Fatal runs never reach the drain. Concurrent
calls share a sink whose integer updates are protected by a short lock. This
stdlib-only leaf keeps the runner independent of the higher review layer."""

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
