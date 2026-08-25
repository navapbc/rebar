"""One completion-verifier attempt cluster for the close precheck.

This module is the call-graph seam between deterministic close checks and the optional LLM
runtime.  It owns three things that must stay together: direct phase timing, bounded
auto-resume dispatch, and conversion of any verifier/runtime failure into the close command's
stable error contract.  Keeping the cluster separate leaves ``close_precheck`` focused on
which deterministic facts are read from the pinned ticket session.

The caller supplies a ticket view when the experimental stable-read path is selected.  The
view remains caller-owned: every auto-resume attempt receives the same object, and this module
never closes or replaces it.  That ownership rule lets the caller run its deterministic
checks against the same ticket OID before entering this seam and close the view in one
``finally`` block afterward.
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic_ns
from typing import Any

from rebar._commands._seam import CommandError


def _direct_metrics_before_run(
    metrics: dict[str, int] | None, clock: Callable[[], int]
) -> tuple[int, int]:
    """Return verifier start/precheck start and stamp the pre-verifier total when enabled."""
    verifier_started_ns = clock()
    if metrics is None:
        return verifier_started_ns, verifier_started_ns
    pre_verifier_started_ns = metrics.pop("_pre_verifier_started_ns", verifier_started_ns)
    metrics["pre_verifier_total_ms"] = (verifier_started_ns - pre_verifier_started_ns) // 1_000_000
    return verifier_started_ns, pre_verifier_started_ns


def _failure_hint(outcome: Any | None) -> tuple[int, str]:
    """Map the framework classifier to the close command's exit code and safe hint."""
    from rebar.llm import failure

    returncode = 11 if (outcome and outcome.retryable) else 1
    if outcome is None:
        return returncode, ""
    message = failure.message_for(
        outcome.resolution_class.value,
        finish_reason=(outcome.diagnostic or {}).get("finish_reason"),
    )
    hint = f" [{outcome.resolution_class.value}: {message}]" if message else ""
    return returncode, hint


def _raise_close_failure(exc: Exception, ticket_id: str) -> None:
    """Raise the stable fail-closed command error for an unavailable or failed verifier."""
    from rebar.llm import failure

    outcome = failure.outcome_of(exc)
    failure.log_degrade(outcome, gate="completion-verify", ticket_id=ticket_id)
    returncode, hint = _failure_hint(outcome)
    remedy = failure.close_gate_remedy(exc, outcome)
    raise CommandError(
        f"Error: cannot close {ticket_id}: completion verification could not run "
        f"({exc}).{hint} {remedy} "
        'Override with --force="<reason>".',
        returncode=returncode,
    ) from None


def run_close_verifier(
    ticket_id: str,
    *,
    ref: str | None,
    code_root: str,
    metrics: dict[str, int] | None = None,
    ticket_view: Any | None = None,
    ticket_read_mode: str | None = None,
    clock: Callable[[], int] = monotonic_ns,
) -> dict:
    """Run bounded completion verification and merge its direct duration metrics.

    ``ticket_view`` is intentionally passed by identity to every auto-resume attempt.  The
    verifier owns neither its lifetime nor its revision choice.  Any exception is classified
    once here and re-raised as :class:`CommandError`; a retryable provider/runtime outage keeps
    the established exit-11 signal, while deterministic or permanent failures use exit 1.
    """
    from rebar._commands import close_autoresume

    verifier_started_ns, _ = _direct_metrics_before_run(metrics, clock)
    session_kwargs: dict[str, Any] = {}
    if ticket_view is not None:
        session_kwargs["ticket_view"] = ticket_view
    if ticket_read_mode is not None:
        session_kwargs["ticket_read_mode"] = ticket_read_mode
    try:
        result = close_autoresume.verify_with_auto_resume(
            ticket_id,
            ref=ref,
            repo_root=code_root,
            cfg_root=code_root,
            **session_kwargs,
        )
    except Exception as exc:  # Every verifier failure blocks the close.
        _raise_close_failure(exc, ticket_id)
        raise AssertionError("unreachable") from exc

    if metrics is None:
        return result
    metrics["verifier_call_ms"] = (clock() - verifier_started_ns) // 1_000_000
    result_metrics = result.get("metrics")
    if isinstance(result_metrics, dict):
        result_metrics.update(metrics)
    return result


__all__ = ["run_close_verifier"]
