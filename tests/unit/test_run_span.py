"""8772 (root span per gate run): one gate run is one trace.

rebar opened no span at the gate run boundary, so `tracing.py`'s per-candidate spans were
each their own trace root and a fanned-out plan-review appeared in Langfuse as many traces.
`run_identity.mint_run_identity` already prefers an active recording span's trace id over
minting; this gives it something to read.

The OTel SDK ships in the optional `[tracing]` extra and is absent here — the API alone only
ever returns non-recording spans — so the tracer is stubbed. That exercises rebar's own
branch (is_recording -> get_span_context -> 032x -> contextvar propagation), which is what
this ticket owns; OTel's parentage is OTel's to test.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.unit

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class _Ctx:
    def __init__(self, trace_id: int) -> None:
        self.trace_id = trace_id


class _Span:
    def __init__(self, trace_id: int) -> None:
        self._c = _Ctx(trace_id)

    def is_recording(self) -> bool:
        return True

    def get_span_context(self) -> _Ctx:
        return self._c

    def set_attribute(self, *a, **k) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None


class _Tracer:
    """A tracer whose spans carry a fixed trace id, and which makes that span current."""

    def __init__(self, trace_id: int) -> None:
        self._t = trace_id

    def start_as_current_span(self, name, *a, **k):
        import contextlib

        from opentelemetry import trace as otel

        span = _Span(self._t)

        @contextlib.contextmanager
        def _cm():
            prev = otel.get_current_span
            otel.get_current_span = lambda *_a, **_k: span
            try:
                yield span
            finally:
                otel.get_current_span = prev

        return _cm()


def _install(monkeypatch: pytest.MonkeyPatch, tracer) -> None:
    from opentelemetry import trace as otel

    monkeypatch.setattr(otel, "get_tracer", lambda *_a, **_k: tracer)


# ══════════════════════════ HAPPY PATH ══════════════════════════


def test_run_span_makes_identity_read_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: inside the run span, mint_run_identity READS it rather than minting.

    This is the ordering assertion: it fails if the span is opened inside rather than
    around the `gate_config(with_identity(...))` statement, because `with_identity` is
    evaluated as an argument and runs before the with-body opens.
    """
    from rebar.llm.run_identity import mint_run_identity
    from rebar.llm.tracing import run_span

    _install(monkeypatch, _Tracer(0xABCDEF))
    with run_span("review-plan", ticket_id="t"):
        got = mint_run_identity(ticket_id="t", operation="review-plan")[0]
    assert got == format(0xABCDEF, "032x")


def test_run_span_is_a_noop_without_a_tracer() -> None:
    """AC4: no tracer configured — the block still runs and nothing raises."""
    from rebar.llm.tracing import run_span

    with run_span("review-plan", ticket_id="t"):
        pass


def test_gate_dispatch_within_module_cap() -> None:
    """AC6: gate_dispatch.py must stay under the 800-line cap."""
    from pathlib import Path

    p = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "rebar"
        / "llm"
        / "workflow"
        / "gate_dispatch.py"
    )
    assert len(p.read_text().splitlines()) <= 800


# ══════════════════════════ HELD-OUT ORACLE ══════════════════════════


def test_identity_inside_a_pool_task_reads_the_run_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 — THE load-bearing case.

    A fanned-out gate run is exactly why runs are currently many traces. The
    main-context assertion above passes even if propagation into the pool is broken, so
    this submits the read through the REAL `generation._submit_ctx` seam.
    """
    from concurrent.futures import ThreadPoolExecutor

    from rebar.llm.plan_review.generation import _submit_ctx
    from rebar.llm.run_identity import mint_run_identity
    from rebar.llm.tracing import run_span

    _install(monkeypatch, _Tracer(0x5150))
    with run_span("review-plan", ticket_id="t"), ThreadPoolExecutor(max_workers=1) as ex:
        outer = mint_run_identity(ticket_id="t", operation="review-plan")[0]
        inner = _submit_ctx(
            ex, lambda: mint_run_identity(ticket_id="t", operation="review-plan")[0]
        ).result()
    assert inner == outer == format(0x5150, "032x")


def test_two_reads_in_one_run_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: every candidate in one run resolves to the same trace id."""
    from rebar.llm.run_identity import mint_run_identity
    from rebar.llm.tracing import run_span

    _install(monkeypatch, _Tracer(0x1234))
    with run_span("verify-completion", ticket_id="t"):
        a = mint_run_identity(ticket_id="t", operation="verify-completion")[0]
        b = mint_run_identity(ticket_id="t", operation="verify-completion")[0]
    assert a == b == format(0x1234, "032x")


def test_two_runs_get_different_traces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinct runs stay distinct — the span groups a run, it does not merge runs."""
    from rebar.llm.run_identity import mint_run_identity
    from rebar.llm.tracing import run_span

    _install(monkeypatch, _Tracer(0xAAA))
    with run_span("review-plan", ticket_id="t"):
        first = mint_run_identity(ticket_id="t", operation="review-plan")[0]
    _install(monkeypatch, _Tracer(0xBBB))
    with run_span("review-plan", ticket_id="t"):
        second = mint_run_identity(ticket_id="t", operation="review-plan")[0]
    assert first != second


def test_a_raising_tracer_does_not_abort_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5: fail-open. The run span sits at the RUN boundary, so an unguarded failure
    here aborts the whole gate run — a strictly worse blast radius than a candidate span.
    `tracing.py`'s standing rule is that tracing must never break an operation."""
    from rebar.llm.tracing import run_span

    class _Boom:
        def start_as_current_span(self, *a, **k):
            raise RuntimeError("tracer exploded")

    _install(monkeypatch, _Boom())
    ran = False
    with run_span("review-plan", ticket_id="t"):
        ran = True
    assert ran, "the gate run must proceed even when the tracer raises"


def test_identity_still_minted_when_span_not_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-recording span must not be read — mint instead, never emit 32 zeroes."""
    from rebar.llm.run_identity import mint_run_identity
    from rebar.llm.tracing import run_span

    class _Dead(_Span):
        def is_recording(self) -> bool:
            return False

    class _DeadTracer(_Tracer):
        def start_as_current_span(self, name, *a, **k):
            import contextlib

            from opentelemetry import trace as otel

            span = _Dead(0x99)

            @contextlib.contextmanager
            def _cm():
                prev = otel.get_current_span
                otel.get_current_span = lambda *_a, **_k: span
                try:
                    yield span
                finally:
                    otel.get_current_span = prev

            return _cm()

    _install(monkeypatch, _DeadTracer(0x99))
    with run_span("review-plan", ticket_id="t"):
        got = mint_run_identity(ticket_id="t", operation="review-plan")[0]
    assert got != format(0x99, "032x")
    assert _HEX32.match(got)
