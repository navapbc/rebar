"""Push-failure classification, backoff, and strict-delivery reporting.

Split out of :mod:`rebar._store.push` (bug f61c) along the seam these functions already
formed: everything here answers "what KIND of push failure is this, how long do we wait,
and how is it reported", and nothing here shells out to git or touches the worktree.
The push loop imports these names; they do not import it.
"""

from __future__ import annotations

import logging
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


# The non-FF / policy-decline / transport marker tables moved to
# :mod:`rebar._store.git_outcome`, which owns every git marker string in the store. The
# SUBTRACTIVE shape they encode is unchanged and now stated once there: bug 2a76 (the bare
# token ``rejected`` is not specific to a non-fast-forward — every server-side decline
# carries it, and those are PERMANENT), bug f61c (a TRANSPORT fault is not a permanent rule
# violation), and bug 4afc (the same exclusion shape in _ref_lock). These names stay as
# lookups because the push loop imports them.
_is_policy_decline = git_outcome.is_policy_decline
_is_non_fast_forward = git_outcome.is_non_fast_forward


_is_multi_bundle = git_outcome.is_multi_bundle
_is_transport_retriable = git_outcome.is_transport_retriable

_DIRTY_WD = git_outcome.DIRTY_WD_RE
_MAX_RETRIES = 5
# Bug f61c: attempts at a transport fault, and the backoff between them. Deliberately small —
# the goal is to ride out a blip, not to sit on a real outage while commits pile up. The sleep
# is injectable so the contract is testable without wall-clock cost.
_MAX_TRANSPORT_ATTEMPTS = 3
_TRANSPORT_BACKOFF_SECONDS = (0.5, 2.0)


# Bug ebee (freeborn-dizzy-raven): an observed pass lost `cannot lock ref
# 'refs/heads/tickets': is at <a> but expected <b>` — a genuine CAS race against a
# concurrent tickets writer. That rejection IS
# classified retriable, so the loop spent its whole budget; it still lost every attempt
# because the retries fired BACK-TO-BACK and kept colliding with the same writer, leaving 6
# unpushed commits. Escalating backoff gives the competing write a window to land.
_CAS_BACKOFF_SECONDS = (0.25, 0.5, 1.0, 2.0)


def _cas_backoff(attempt: int, sleep_fn: Callable[[float], None] | None = None) -> None:
    """Sleep after non-fast-forward recovery *attempt* (1-based) before the next push."""
    delay = _CAS_BACKOFF_SECONDS[min(attempt, len(_CAS_BACKOFF_SECONDS)) - 1]
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
    """Decide a non-fast-forward-free push failure: ``True`` retry, ``False`` terminal.

    Bug f61c: a TRANSIENT transport fault earns a bounded retry with backoff before it
    becomes terminal — abandoning an already-converged pass after one TLS blip stranded
    its commits on the runner. A policy decline is untouched: it stays terminal after
    exactly one attempt, because hitting the remote again cannot change a permanent rule
    violation (bug 2a76). A terminal outcome raises (strict) or logs, before returning.
    """
    if _is_transport_retriable(stderr) and transport_attempts < _MAX_TRANSPORT_ATTEMPTS:
        # Bug 3ff9: this retry is automatic — announcing it at WARNING primed agent
        # sessions to investigate a fault the code was already riding out; the operator
        # ruling update suppresses it under normal load ("noise, not an outage
        # signal") — DEBUG at most.
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
    """The (log level, backlog suffix) for a terminal best-effort contention failure.

    Bug 3ff9 (squeamish-halfawake-fantail): a lost contention race under concurrent
    tickets writers is EXPECTED and self-healing — the write is committed locally, the
    durable push-pending marker records the outcome, and the backlog publishes on the
    next successful write — so surfacing it to agent-visible output primed agent
    sessions to investigate a non-issue (operator ruling 2026-08-21, and its update:
    "We should suppress the message under normal load. This is noise, not an outage
    signal." — DEBUG at most). It stays operator-loud only when it is provably NOT
    self-healing: a policy decline in the terminal stderr (permanent, bug 2a76), or a
    backlog that GREW since the previously recorded failure
    (:func:`push_state.backlog_grew`). MUST be called BEFORE :func:`_raise_if_strict`
    records the new failure, which overwrites the marker the growth check reads.
    """
    summary = _unpushed_summary(base_path, remote_ref)
    if _is_policy_decline(stderr):
        return logging.WARNING, summary
    if push_state.backlog_grew(base_path, remote_ref):
        return logging.WARNING, summary + "; the backlog GREW since the previous failure"
    return (
        logging.DEBUG,
        summary + "; expected under concurrent tickets writers — no action needed",
    )
