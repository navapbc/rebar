"""Gate-sidecar economics + env-diagnosis readers (ticket 3c07).

These are *read-only* derivations over the gate **sidecar** event streams that
the reducer ignores. Because the reducer folds none of them, we scan the raw
event files directly, exactly as :mod:`rebar.metrics.event_metrics` does: the
tracker dir is ``rebar.config.tracker_dir(repo_root)``, each ticket is a
subdirectory named by ticket id, and event files are named
``<ts_ns>-<uuid>-<EVENT_TYPE>.json`` carrying the envelope
``{"event_type","timestamp","uuid","env_id","author","data"}``.

Two sidecar streams are read:

- ``*-REVIEW_RESULT.json`` — plan/code-review gate results. A normal result is
  ``{"schema":"plan_review_result_v2","verdict":"PASS|BLOCK|FAIL",
  "gate":"plan_review","metrics":{"llm_calls":N,...}}``. **Only REVIEW_RESULT
  carries a ``metrics`` block** (hence the only source of ``llm_calls``).
- ``*-COMPLETION_VERDICT.json`` — completion-verifier verdicts, either
  ``{"schema":"completion_verifier_pass_v1","verdict":"PASS"}`` or
  ``{"schema":"completion_verifier_fail_v1","verdict":"FAIL"}``. These carry no
  metrics block.

A ``gate_error_v1`` record — ``{"schema":"gate_error_v1","verdict":"ERROR",
"gate":"plan_review|code_review|completion","error":{...}}`` — rides on the
REVIEW_RESULT stream (plan/code-review gates) or the COMPLETION_VERDICT stream
(completion gate). The schema-guarded verdict readers skip it, so the env
diagnosis derivation uses a raw scan to see it.

The three derivations (the oracle's direct targets):

- :func:`cost_per_accepted_change` — total ``llm_calls`` over all REVIEW_RESULT
  records / count of accepted (``completion_verifier_pass_v1``) closes.
- :func:`env_diagnosis_intervals` — each ``gate_error_v1`` ERROR paired with the
  next same-gate PASS on that ticket, carrying the ns duration between them.
- :func:`first_pass_verification` — fraction of tickets whose earliest
  completion verdict is a pass.

Each is registered into the c085 :data:`~rebar.metrics.registry.REGISTRY` via a
single-arg *context adapter* (c085's ``MetricSpec.compute`` is
``Callable[[context], value | None]``): the adapter pulls ``repo_root`` / range
off the context object and calls the multi-arg derivation.
"""

from __future__ import annotations

import os
from typing import Any

from rebar.metrics.event_metrics import (
    _bounds,
    _event_files,
    _in_range,
    _load,
    _ticket_dirs,
)
from rebar.metrics.registry import REGISTRY, MetricSpec
from rebar.reducer._sort import event_sort_key

# Stream event types.
_REVIEW = "REVIEW_RESULT"
_COMPLETION = "COMPLETION_VERDICT"

# Schema tags.
_GATE_ERROR = "gate_error_v1"
_COMPLETION_PASS = "completion_verifier_pass_v1"


# ---------------------------------------------------------------------------
# Per-record classification helpers.
# ---------------------------------------------------------------------------


def _record_gate(event_type: Any, data: dict[str, Any]) -> Any:
    """The gate identity a record belongs to.

    REVIEW_RESULT records name their gate explicitly in ``data.gate``
    (``plan_review`` / ``code_review``); COMPLETION_VERDICT records belong to
    the ``completion`` gate (a ``gate_error_v1`` on that stream still names it).
    """

    if event_type == _COMPLETION:
        return data.get("gate") or "completion"
    return data.get("gate")


def _is_error(data: dict[str, Any]) -> bool:
    return data.get("schema") == _GATE_ERROR


def _is_pass(data: dict[str, Any]) -> bool:
    return data.get("verdict") == "PASS"


# ---------------------------------------------------------------------------
# Derivations (the oracle's direct targets).
# ---------------------------------------------------------------------------


def cost_per_accepted_change(
    repo_root: Any,
    since: str | None = None,
    until: str | None = None,
) -> float | None:
    """Total gate ``llm_calls`` per accepted change.

    Sums ``data.metrics.llm_calls`` across ALL REVIEW_RESULT records (the only
    stream that carries a metrics block) and divides by the COUNT of accepted
    closes — ``completion_verifier_pass_v1`` records on the COMPLETION_VERDICT
    stream. Returns ``None`` when there are zero accepted closes (never a
    ``ZeroDivisionError``); COMPLETION_VERDICT records are never read for
    ``llm_calls`` because they carry none.
    """

    lo, hi = _bounds(since, until)
    total_calls: float = 0
    accepted = 0
    for ticket_dir in _ticket_dirs(repo_root):
        for path in _event_files(ticket_dir, _REVIEW):
            event = _load(path)
            if not _in_range(event.get("timestamp"), lo, hi):
                continue
            metrics = (event.get("data") or {}).get("metrics") or {}
            calls = metrics.get("llm_calls")
            if isinstance(calls, (int, float)) and not isinstance(calls, bool):
                total_calls += calls
        for path in _event_files(ticket_dir, _COMPLETION):
            event = _load(path)
            if not _in_range(event.get("timestamp"), lo, hi):
                continue
            if (event.get("data") or {}).get("schema") == _COMPLETION_PASS:
                accepted += 1
    if accepted == 0:
        return None
    return total_calls / accepted


def _ticket_sidecar_records(ticket_dir: str) -> list[dict[str, Any]]:
    """Both sidecar streams of one ticket, merged and chronologically sorted."""

    paths: list[str] = []
    for event_type in (_REVIEW, _COMPLETION):
        paths.extend(_event_files(ticket_dir, event_type))
    paths.sort(key=event_sort_key)
    return [_load(path) for path in paths]


def env_diagnosis_intervals(
    repo_root: Any,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Pair each ``gate_error_v1`` ERROR with the next same-gate PASS.

    For every ticket, the two sidecar streams are merged chronologically. Each
    ``gate_error_v1`` ERROR is paired with the FIRST later record that is a PASS
    for the SAME gate (matched on the ERROR's ``data.gate``): a ``plan_review``
    ERROR pairs with the next ``plan_review`` REVIEW_RESULT PASS, a
    ``completion`` ERROR with the next ``completion_verifier_pass_v1`` — never
    merely the next PASS on the stream. Each closed interval is returned as
    ``{"ticket_id","gate","duration_ns"}`` where ``duration_ns`` is
    ``PASS.timestamp - ERROR.timestamp``. An ERROR with no following same-gate
    PASS is omitted.
    """

    lo, hi = _bounds(since, until)
    out: list[dict[str, Any]] = []
    for ticket_dir in _ticket_dirs(repo_root):
        ticket_id = os.path.basename(ticket_dir)
        records = _ticket_sidecar_records(ticket_dir)
        for i, event in enumerate(records):
            data = event.get("data") or {}
            if not _is_error(data):
                continue
            err_ts = event.get("timestamp")
            if not _in_range(err_ts, lo, hi):
                continue
            gate = _record_gate(event.get("event_type"), data)
            pass_ts = _next_same_gate_pass(records[i + 1 :], gate, lo, hi)
            if pass_ts is None or not isinstance(err_ts, int):
                continue
            out.append(
                {
                    "ticket_id": ticket_id,
                    "gate": gate,
                    "duration_ns": pass_ts - err_ts,
                }
            )
    return out


def _next_same_gate_pass(
    later: list[dict[str, Any]],
    gate: Any,
    lo: int | None,
    hi: int | None,
) -> int | None:
    """Timestamp of the first later record that is a PASS for ``gate``, else None."""

    for event in later:
        data = event.get("data") or {}
        if _is_error(data):
            continue
        if not _is_pass(data):
            continue
        if _record_gate(event.get("event_type"), data) != gate:
            continue
        ts = event.get("timestamp")
        if not _in_range(ts, lo, hi):
            continue
        if isinstance(ts, int):
            return ts
    return None


def first_pass_verification(
    repo_root: Any,
    since: str | None = None,
    until: str | None = None,
) -> float | None:
    """Fraction of tickets whose EARLIEST completion verdict is a pass.

    A ticket counts toward the denominator when it has any COMPLETION_VERDICT
    record in range; it counts toward the numerator when its oldest such record
    (by filename/chronological order) is a ``completion_verifier_pass_v1``.
    Returns ``None`` when no ticket has any completion verdict in range.
    """

    lo, hi = _bounds(since, until)
    tickets = 0
    first_passed = 0
    for ticket_dir in _ticket_dirs(repo_root):
        earliest_schema: str | None = None
        for path in _event_files(ticket_dir, _COMPLETION):
            event = _load(path)
            if not _in_range(event.get("timestamp"), lo, hi):
                continue
            earliest_schema = (event.get("data") or {}).get("schema")
            break
        if earliest_schema is None:
            continue
        tickets += 1
        if earliest_schema == _COMPLETION_PASS:
            first_passed += 1
    if tickets == 0:
        return None
    return first_passed / tickets


# ---------------------------------------------------------------------------
# c085 registry integration — single-arg context adapters.
# ---------------------------------------------------------------------------

_ACCRUING_SINCE = "2026-01-01T00:00:00+00:00"


def _spec(metric_id: str, fn: Any) -> MetricSpec:
    """Build a MetricSpec whose single-arg ``compute`` adapts to the c085 context."""

    def compute(ctx: Any) -> Any:
        if ctx is None:
            return None
        return fn(
            getattr(ctx, "repo_root", None),
            getattr(ctx, "since", None),
            getattr(ctx, "until", None),
        )

    return MetricSpec(
        id=metric_id,
        lens="gate_economics",
        source="sidecar",
        confidence="high",
        compute=compute,
        accruing_since=_ACCRUING_SINCE,
    )


def register() -> None:
    """Append this module's specs to the c085 REGISTRY (idempotent on id)."""

    existing = {spec.id for spec in REGISTRY}
    specs = [
        _spec("cost_per_accepted_change", cost_per_accepted_change),
        _spec("env_diagnosis_intervals", env_diagnosis_intervals),
        _spec("first_pass_verification", first_pass_verification),
    ]
    for spec in specs:
        if spec.id not in existing:
            REGISTRY.append(spec)
            existing.add(spec.id)


register()
