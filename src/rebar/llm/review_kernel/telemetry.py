"""Shared, run-scoped review telemetry (ticket c2c5): the contract-violation sink and the
``*_decide``-boundary ``outcome_counts`` tally, extracted so plan-review and code-review consume
ONE implementation instead of forking it.

**The sink.** Pass-2's verifier -> Pass-3 reshape (the ``*_decide`` ops) detects contract
violations (malformed / duplicate / out-of-range verification indices) via the shared
:func:`.verify.reshape_verifications` seam. Under the expand-contract posture (epic
drag-gripe-brake) those NEVER change the verdict — they are surfaced as ADDITIVE observability:
an ERROR log + a ``verification_contract_violations`` entry on the verdict coverage, present
only when non-empty (so a clean run's verdict stays byte-identical). ``decide`` and the terminal
coach/verdict step run as SEPARATE workflow steps, so the report is carried between them by a
run-scoped ``ContextVar``, activated once per gate run around the whole workflow execution.

**``decide_outcome_counts``.** :func:`.verify.verify_findings` tallies a PER-CHUNK
``outcome_counts`` (``clean``/``recovered``/``empty_outcomes``/``unrecoverable``) — but neither
production pipeline calls it (both run Pass-2 as a declarative workflow prompt step, not a
procedural ``run_chunk`` loop), so chunk boundaries are gone by the time a ``*_decide`` op sees
its input: only the merged, flat ``verifications`` list and the already-computed
:class:`.verify.VerificationReshape` are available. This helper derives the same four-bucket
vocabulary as a SINGLE-RUN (not per-chunk) tally over that boundary. ``unrecoverable`` is always
``0`` here: a ``StructuredOutputError`` is a per-chunk execution signal that does not survive the
merge into the flat list (deferred follow-on work, not this ticket's scope)."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from typing import Any

from .verify import VerificationReshape

_contract_violations: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "rebar_review_kernel_contract_violations", default=None
)


@contextlib.contextmanager
def collect_contract_violations() -> Iterator[None]:
    """Activate a run-scoped sink for verification contract violations for the dynamic extent of
    one review-gate run. Nesting reuses the active sink (idempotent); the sink is dropped on exit
    so it never leaks across runs/tickets."""
    if _contract_violations.get() is not None:
        yield
        return
    token = _contract_violations.set([])
    try:
        yield
    finally:
        _contract_violations.reset(token)


def record_contract_violation(summary: dict[str, Any]) -> None:
    """Record one NON-EMPTY contract-violation summary if a sink is active; a no-op outside a
    :func:`collect_contract_violations` scope (so unit-testing a ``*_decide`` op in isolation
    never raises and never leaks)."""
    sink = _contract_violations.get()
    if sink is not None and summary:
        sink.append(dict(summary))


def drain_contract_violations() -> list[dict[str, Any]]:
    """Return + clear the violations recorded in the active sink (empty list when none recorded,
    or when no sink is active)."""
    sink = _contract_violations.get()
    if not sink:
        return []
    drained = list(sink)
    sink.clear()
    return drained


def decide_outcome_counts(
    raw_verifs: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    reshape: VerificationReshape,
) -> dict[str, int]:
    """The single-run ``{clean, recovered, empty_outcomes, unrecoverable}`` tally available at a
    ``*_decide`` step boundary — the whole (already-merged) verify output treated as ONE unit,
    not per-chunk. ``unrecoverable`` is always ``0`` (see module docstring). No findings means
    there was nothing to verify: every count is ``0``."""
    counts = {"clean": 0, "recovered": 0, "empty_outcomes": 0, "unrecoverable": 0}
    if not findings:
        return counts
    if not raw_verifs:
        counts["empty_outcomes"] = 1
    elif reshape.has_violations:
        counts["recovered"] = 1
    else:
        counts["clean"] = 1
    return counts
