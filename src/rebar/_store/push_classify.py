"""Classify push failures and select backoff and strict reporting.

This module does not invoke Git or modify the worktree. The push loop imports these helpers,
while they remain independent of the loop.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from rebar._optional import OptionalDependencyError
from rebar._store import git_outcome, push_state
from rebar._store.push_state import unpushed_summary as _unpushed_summary

logger = logging.getLogger(__name__)


class PushDeliveryError(RuntimeError):
    """A strict tickets-branch delivery failure with a stable classification."""

    def __init__(self, reason: str, detail: str, base_path: str, remote_ref: str) -> None:
        self.reason = reason
        self.detail = detail
        self.message = f"{reason}: {detail}{_unpushed_summary(base_path, remote_ref)}"
        super().__init__(self.message)


def _raise_if_strict(
    strict: bool, reason: str, detail: str, base_path: str, remote_ref: str
) -> None:
    """Record the delivery outcome, then raise only for a strict caller.

    Every terminal exit in this module already routes through here carrying the closed set
    of :class:`PushDeliveryError` reasons, which makes it the one place a failure cannot be
    missed — so the durable marker is written HERE rather than at a dozen call sites. The
    default (best-effort) path still returns ``None``: recording is a SIGNAL, not a raise.
    """
    push_state.record_failure(base_path, reason, detail, remote_ref)
    if strict:
        raise PushDeliveryError(reason, detail, base_path, remote_ref)


# :mod:`rebar._store.git_outcome` owns marker tables and their subtractive precedence.
# Policy declines stay terminal while non-fast-forward and transport failures retain distinct
# recovery paths. These aliases remain because the push loop imports them.
_is_policy_decline = git_outcome.is_policy_decline
_is_non_fast_forward = git_outcome.is_non_fast_forward


_is_multi_bundle = git_outcome.is_multi_bundle
_is_transport_retriable = git_outcome.is_transport_retriable

_DIRTY_WD = git_outcome.DIRTY_WD_RE
_MAX_RETRIES = 5
# Three bounded attempts ride out short transport faults. Injectable sleep avoids wall-clock
# cost in tests.
_MAX_TRANSPORT_ATTEMPTS = 3
_TRANSPORT_BACKOFF_SECONDS = (0.5, 2.0)


# Escalating CAS delays let a concurrent tickets writer finish before the next refetch.
_CAS_BACKOFF_SECONDS = (0.25, 0.5, 1.0, 2.0)

# Small additive jitter prevents lockstep wakeups without reaching the next doubled base.
_CAS_BACKOFF_JITTER = 0.25


def _cas_backoff(attempt: int, sleep_fn: Callable[[float], None] | None = None) -> None:
    """Sleep before a non-fast-forward recovery *attempt* (1-based) re-fetches.

    The delay is the clamped base schedule plus additive jitter, landing in
    ``[base, base * (1 + _CAS_BACKOFF_JITTER)]`` — always at least ``base`` and bounded, so
    colliding writers do not wake in lockstep.
    """
    base = _CAS_BACKOFF_SECONDS[min(attempt, len(_CAS_BACKOFF_SECONDS)) - 1]
    delay = base * (1.0 + random.random() * _CAS_BACKOFF_JITTER)
    (time.sleep if sleep_fn is None else sleep_fn)(delay)


def _transport_backoff(attempt: int, sleep_fn: Callable[[float], None] | None = None) -> None:
    """Sleep before transport retry *attempt* (1-based), clamped to the declared schedule."""
    delay = _TRANSPORT_BACKOFF_SECONDS[min(attempt, len(_TRANSPORT_BACKOFF_SECONDS)) - 1]
    (time.sleep if sleep_fn is None else sleep_fn)(delay)


def _heal_multi_bundle_or_stop(
    base_path: str,
    remote: str,
    branch: str,
    remote_ref: str,
    stderr: str,
    strict: bool,
) -> bool:
    """Collapse git-remote-s3's divergent bundles: ``True`` retry the push, ``False`` stop.

    A terminal outcome raises (strict) or logs before returning.
    """
    from rebar._store import s3_doctor

    try:
        s3_doctor.heal_multi_bundle(base_path, remote, branch)
    except s3_doctor.S3DoctorConflict as exc:
        logger.warning(
            "s3 doctor could not heal multi-bundle ref %s: %s (%s)%s",
            remote_ref,
            exc,
            exc.hint,
            _unpushed_summary(base_path, remote_ref),
        )
        _raise_if_strict(strict, "push-multi-bundle-conflict", str(exc), base_path, remote_ref)
        return False
    except OptionalDependencyError as exc:
        logger.warning("s3 doctor unavailable for multi-bundle heal: %s", exc)
        _raise_if_strict(strict, "push-transport-failed", stderr, base_path, remote_ref)
        return False
    return True


def _retry_transport_or_stop(
    base_path: str,
    remote_ref: str,
    stderr: str,
    returncode: int,
    strict: bool,
    transport_attempts: int,
    sleep_fn: Callable[[float], None] | None,
) -> bool:
    """Return whether a push failure without non-fast-forward evidence should retry.

    Transport failures retry with bounded backoff. Policy declines terminate after one
    attempt. A terminal outcome raises for strict callers or logs before returning ``False``."""
    if _is_transport_retriable(stderr) and transport_attempts < _MAX_TRANSPORT_ATTEMPTS:
        # Automatic recovery is a debug diagnostic, not an operator warning.
        logger.debug(
            "tickets branch push hit a transient transport fault "
            "(transport attempt %s/%s); retrying automatically, no action needed: %s",
            transport_attempts,
            _MAX_TRANSPORT_ATTEMPTS,
            stderr.strip()[:200],
        )
        _transport_backoff(transport_attempts, sleep_fn)
        return True
    reason = "push-policy-declined" if _is_policy_decline(stderr) else "push-transport-failed"
    _raise_if_strict(strict, reason, stderr, base_path, remote_ref)
    logger.warning(
        "tickets branch push failed (exit %s): %s%s",
        returncode,
        stderr,
        _unpushed_summary(base_path, remote_ref),
    )
    return False


def _terminal_severity(base_path: str, remote_ref: str, stderr: str) -> tuple[int, str]:
    """Return the log level and backlog suffix for terminal best-effort contention.

    Ordinary contention stays at debug because the local commit and push-pending marker let
    the next write publish it. A policy decline or growing backlog uses warning. Call this
    before :func:`_raise_if_strict` overwrites the marker used for growth comparison."""
    summary = _unpushed_summary(base_path, remote_ref)
    if _is_policy_decline(stderr):
        return logging.WARNING, summary
    if push_state.backlog_grew(base_path, remote_ref):
        return logging.WARNING, summary + "; the backlog GREW since the previous failure"
    return (
        logging.DEBUG,
        summary + "; expected under concurrent tickets writers — no action needed",
    )
