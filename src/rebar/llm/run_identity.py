"""The per-gate-run correlation identity (story 6cd0, smart-evadable-teledu).

``review_result.schema.json`` and ``completion_verdict.schema.json`` have always declared a
``trace_id``, but every emitting site in ``runner.py`` hardcoded ``None``: a verdict could not
name the run that produced it, and no request header could carry a run-stable value. This module
mints that identity ONCE per gate run, at the two boundaries in ``workflow/gate_dispatch.py``
that already resolve the run's config, and it rides to every op through the ``gate_config``
contextvar (and to pool workers through ``copy_context().run``).

Three values, no ``run_id`` — ``trace_id`` already identifies the run:

* ``trace_id`` — 32 lowercase hex, the W3C trace-id shape Langfuse v3+ requires. READ from the
  active OpenTelemetry span when one is recording (so a caller-owned span's runs correlate),
  else MINTED with :func:`secrets.token_hex`. Random per run rather than derived from the
  ticket: a derived value would merge every re-review into a single trace.
* ``ticket_id`` / ``operation`` — knowable only at the boundary. ``operation`` is one of
  rebar's canonical verb names, ``"review-plan"`` or ``"verify-completion"``, so the vocabulary
  matches the CLI and the docs.

The OpenTelemetry import is LAZY and guarded, so this module never requires the optional
``[tracing]`` extra: an absent API, an absent span, or a non-recording one all fall through to
the mint. Nothing here reads ambient configuration or the environment — reading the live trace
context is neither — so this module stays below the config-ownership seam (RP-04 S7.1).
"""

from __future__ import annotations

import secrets
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime
    from rebar.llm.config import LLMConfig

__all__ = ["mint_run_identity", "with_identity"]


def _recording_span_trace_id() -> str | None:
    """The active OpenTelemetry span's trace id as 32 lowercase hex, or ``None``.

    ``None`` covers every "no usable enclosing trace" case uniformly: the ``opentelemetry-api``
    package is not installed (it is an optional extra), no span is current, the current span is
    not recording (the no-op / INVALID span the API returns by default), or the SDK raised while
    being asked. Every one of them means "mint a fresh id" to the caller.
    """
    try:
        from opentelemetry import trace as _otel_trace
    except ImportError:  # the [tracing] extra is optional — absent API means "mint"
        return None
    try:
        span = _otel_trace.get_current_span()
        if span is None or not span.is_recording():
            return None
        trace_id = span.get_span_context().trace_id
    except Exception:  # noqa: BLE001 — a broken/partial SDK must never fail a gate run
        return None
    # 0 is the INVALID trace id; treat it as "no trace" rather than emitting 32 zeroes.
    return format(trace_id, "032x") if trace_id else None


def mint_run_identity(*, ticket_id: str, operation: str) -> tuple[str, str, str]:
    """The identity triple ``(trace_id, ticket_id, operation)`` for one gate run.

    ``trace_id`` is the active recording span's id when there is one, else freshly minted.
    ``ticket_id`` and ``operation`` are passed straight through: the boundary is the only place
    they are knowable, and returning them here keeps the triple the single unit a caller
    attaches.
    """
    return (_recording_span_trace_id() or secrets.token_hex(16), ticket_id, operation)


def with_identity(cfg: LLMConfig, ticket_id: str, operation: str) -> LLMConfig:
    """A COPY of ``cfg`` carrying a freshly minted run identity.

    ``dataclasses.replace`` — never mutation: the caller's ``LLMConfig`` may be a long-lived
    object it passed in explicitly, and a gate run must not write back into it.
    """
    trace_id, run_ticket_id, run_operation = mint_run_identity(
        ticket_id=ticket_id, operation=operation
    )
    return replace(cfg, trace_id=trace_id, ticket_id=run_ticket_id, operation=run_operation)
