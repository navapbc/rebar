#!/usr/bin/env python3
"""RP-03 S1 T4 — the summary-executor selector + ``OperationOutcome`` mapping.

``apply_handlers.handle_update`` gains a constructor-injected, provider-neutral
``summary_executor`` (``BatchApplyContext.summary_executor``) that DEFAULTS TO
``None``. This leaf owns the two pure decisions that seam needs, kept out of the
740-LOC ``dispatch_one`` so neither module crosses the 800-LOC cap:

* :func:`is_exact_summary_update` — the SELECTOR predicate. An outbound update is
  eligible for the executor ONLY when its ``fields`` are EXACTLY
  ``{"summary": <str>}`` — one key, that key is ``"summary"``, and its value is a
  ``str``. Any other shape (mixed fields, a non-``summary`` key, a non-string
  value, an empty or non-mapping ``fields``) stays on the legacy generic path and
  is NEVER split or dual-sent.

* :func:`apply_summary_outcome` — the MAPPING helper. It folds the executor's
  returned :class:`OperationOutcome` onto the manifest ``outcome`` dict following
  the ADR-0103 disposition contract, and returns whether the summary CONFIRMEDLY
  landed (so the caller advances the ADR-0026 baseline).

Neither function performs I/O or touches the generic retry wrapper: the executor
owns its own retry budget per ADR 0103, so a selected summary route bypasses
``dispatch_apply_phases._call_with_retry`` entirely.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rebar_reconciler.operation_outcome import (
    Disposition,
    OperationOutcome,
    _redact_message,
)

# A CONFIRMED success: the write landed (``applied`` / ``recovered``) or the
# target was already at the desired value (``already_satisfied`` — a success
# no-op). Every OTHER disposition is TERMINAL for this pass: it advances no
# baseline and records a single redacted per-mutation error naming the
# disposition. Enumerated here as the single source of the success set.
_SUCCESS_DISPOSITIONS: frozenset[Disposition] = frozenset(
    {
        Disposition.applied,
        Disposition.recovered,
        Disposition.already_satisfied,
    }
)


def is_exact_summary_update(fields: Any) -> bool:
    """Return ``True`` iff ``fields`` is EXACTLY ``{"summary": <str>}``.

    The selector is intentionally strict — a lone ``summary`` key whose value is a
    ``str``. A mapping carrying any additional key, a differently-named key, a
    non-string ``summary`` value, an empty mapping, or a non-mapping is NOT
    eligible and stays on the legacy generic path (never split, never dual-sent).
    """
    if not isinstance(fields, Mapping):
        return False
    if len(fields) != 1 or "summary" not in fields:
        return False
    return isinstance(fields["summary"], str)


def _first_diagnostic_message(outcome: OperationOutcome) -> str:
    """The first diagnostic ``message`` on ``outcome``, or ``""`` if none carries one.

    The diagnostics are already bounded/redacted at construction, but the terminal
    error string is re-routed through ``_redact_message`` by the caller so the
    final combined ``"<disposition>: <msg>"`` string is redacted AND capped as one
    unit (defence in depth against a disposition value or separator ever growing).
    """
    for entry in outcome.diagnostics:
        if "message" in entry:
            return str(entry["message"])
    return ""


def apply_summary_outcome(
    manifest_outcome: dict,
    outcome: OperationOutcome,
    jira_key: str,
) -> bool:
    """Fold ``outcome`` onto ``manifest_outcome`` per the ADR-0103 disposition contract.

    Returns ``True`` when the summary CONFIRMEDLY landed (a ``applied`` /
    ``recovered`` / ``already_satisfied`` disposition) so the caller advances the
    ADR-0026 baseline; ``False`` for every terminal disposition.

    Success arm — mirror the legacy successful-update result shape
    (``{"key": jira_key}``) and leave ``error`` untouched (legacy success never
    sets it).

    Terminal arm — ``result`` is falsy (``None``, never a false success);
    ``error`` is EXACTLY ONE redacted, ≤512-code-point string that NAMES the
    disposition, derived from the outcome's first diagnostic message and routed
    whole through ``operation_outcome._redact_message`` (ADR-0041 sanitizer +
    512-code-point cap). A set ``retry_not_before`` is carried onto the manifest;
    no durable deferral or provenance is persisted here (that is RP-03 S3).
    """
    if outcome.disposition in _SUCCESS_DISPOSITIONS:
        manifest_outcome["result"] = {"key": jira_key}
        return True

    manifest_outcome["result"] = None
    msg = _first_diagnostic_message(outcome)
    manifest_outcome["error"] = _redact_message(f"{outcome.disposition.value}: {msg}")
    if outcome.retry_not_before is not None:
        manifest_outcome["retry_not_before"] = outcome.retry_not_before
    return False
