"""Read the canonical bridge ``preview`` / ``sync`` route's stream contract.

WHY THIS MODULE EXISTS. The reconciler's ``OK: …`` summary lines on stdout are printed
**only** for the compatibility ``--mode`` routes: ``__main__.py`` guards that block with
``route not in {"preview", "sync"}``. The canonical routes suppress it deliberately and
report through a single stderr disposition line instead — ``docs/exit-codes.md``
§"Bridge routes: canonical 0/1/2 and retained compatibility sentinels" pins
``BRIDGE_STATE: converged`` as the contract, and ADR 0092 records the adapters.

When commit ``c87afedba8`` re-pointed the cells in ``test_reconcile_pass.py`` at
``sync``, their ``"OK:" in stdout`` assertions became assertions on an output the invoked
route does not emit, and the next live DC run went red against a reconciler that was
behaving correctly.

THE TRAP TO AVOID WHEN FIXING THAT. ``BRIDGE_STATE: converged`` is **not** by itself a
zero-write signal. ``__main__.py`` classifies a pass that applied N mutations as
``CONVERGED`` as well — the disposition means "the pass reached a settled end state", not
"the pass wrote nothing". Swapping the idempotence cell's assertion one-for-one onto that
line would keep it green while a pass thrashed the remote on every run, which is the exact
failure it was written to catch.

The zero-write evidence a canonical ``sync`` run does emit is the reconciler's ``RECON:``
telemetry. Those are bare ``print(..., file=sys.stderr)`` calls, not ``logging`` calls, so
they appear at any log level with no ``REBAR_LOG_LEVEL`` cooperation:

* ``RECON: outbound_differ total=N create=… update=… delete=…`` — ``run_differs.py``,
  emitted unconditionally once the outbound differ completes.
* ``RECON: inbound_differ total=N with_fields=… …`` — ``run_differs.py``, likewise, and
  already net of bidirectional suppression (suppressed pairs are returned separately and
  are not counted in the total).
* ``RECON: batch_outcome action=… key=… error=…`` — ``applier.py``, emitted once **per
  applied mutation**, so zero occurrences is direct evidence that nothing was written.

Every function here takes captured text and returns a verdict. No subprocess, no network,
no Jira, no ``rebar`` import — so ``tests/unit/test_bridge_output_parsing.py`` can prove
this parsing without the live DC harness, which only CI's external lane can run.
"""

from __future__ import annotations

import re

# The canonical benign-state line for a settled pass (docs/exit-codes.md).
CONVERGED_LINE = "BRIDGE_STATE: converged"

# Deliberately unanchored. Interleaved writers on a shared stderr can prepend text to a
# line, and a false red in a 40-minute live job is far more expensive than the tiny risk
# of matching this distinctive token mid-line.
_OUTBOUND_TOTAL = re.compile(r"RECON: outbound_differ total=(\d+)")
_INBOUND_TOTAL = re.compile(r"RECON: inbound_differ total=(\d+)")
_BATCH_OUTCOME = re.compile(r"RECON: batch_outcome\b")


def converged(stderr: str) -> bool:
    """True when the pass reported the canonical settled disposition."""
    return CONVERGED_LINE in stderr


def differ_totals(stderr: str) -> tuple[list[int], list[int]]:
    """Every outbound and inbound differ total the pass reported, in order.

    Lists rather than scalars: a pass normally reports each total once, but returning all
    occurrences means a second reported round cannot hide a non-zero behind a zero.
    """
    outbound = [int(m.group(1)) for m in _OUTBOUND_TOTAL.finditer(stderr)]
    inbound = [int(m.group(1)) for m in _INBOUND_TOTAL.finditer(stderr)]
    return outbound, inbound


def applied_mutation_count(stderr: str) -> int:
    """Number of mutations the applier reported writing (one line each)."""
    return len(_BATCH_OUTCOME.findall(stderr))


def converged_pass_problem(stdout: str, stderr: str) -> str | None:
    """Describe why a pass does not look converged, or None when it does.

    This is the canonical-route replacement for the legacy ``"OK:" in stdout`` check. It
    asserts the pass settled; it deliberately says nothing about how much was written,
    because the cells that use it exercise passes that are SUPPOSED to write.
    """
    if converged(stderr):
        return None
    if "OK:" in stdout:
        return (
            "the pass reported the LEGACY route's OK: line on stdout but not the canonical "
            f"{CONVERGED_LINE!r} on stderr — was it invoked with --mode instead of the "
            "preview/sync route?"
        )
    return f"no {CONVERGED_LINE!r} line on stderr"


def wrote_nothing_problem(stdout: str, stderr: str) -> str | None:
    """Describe why a pass does not look like a no-op, or None when it does.

    Convergence alone is not enough (see the module docstring): this additionally requires
    both differ totals to be reported and zero, and zero applied-mutation lines.
    """
    problem = converged_pass_problem(stdout, stderr)
    if problem is not None:
        return problem

    applied = applied_mutation_count(stderr)
    if applied:
        return f"the pass applied {applied} mutation(s) — it wrote to the remote"

    outbound, inbound = differ_totals(stderr)
    if not outbound or not inbound:
        missing = "outbound" if not outbound else "inbound"
        return (
            f"no 'RECON: {missing}_differ total=' line on stderr — the pass did not reach "
            "the differ stage, so its zero-write claim cannot be confirmed"
        )
    if any(outbound) or any(inbound):
        return (
            f"the differ computed work: outbound totals {outbound}, inbound totals {inbound} "
            "— a converged repeat pass must compute nothing"
        )
    return None
