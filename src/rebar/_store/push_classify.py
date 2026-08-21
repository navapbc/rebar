"""Push-failure classification, backoff, and strict-delivery reporting.

Split out of :mod:`rebar._store.push` (bug f61c) along the seam these functions already
formed: everything here answers "what KIND of push failure is this, how long do we wait,
and how is it reported", and nothing here shells out to git or touches the worktree.
The push loop imports these names; they do not import it.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

from rebar._optional import OptionalDependencyError
from rebar._store import push_state
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


_NON_FF = re.compile(r"non-fast-forward|rejected|fetch first", re.IGNORECASE)

# Bug 2a76: the bare token ``rejected`` above is NOT specific to a non-fast-forward.
# git prints ``! [remote rejected] HEAD -> tickets (pre-receive hook declined)`` for
# EVERY server-side decline — GitHub push protection (GH013 secret scanning), a
# pre-receive hook, branch protection, a rate limit, an internal server error. Those
# are PERMANENT policy rejections: a fetch+merge cannot fix them, so classifying them
# as non-fast-forward burned all three retries (three real hits on the remote's hook,
# zero merge commits) and then reported only "failed after 3 retries" — the reason git
# gave us was thrown away, making an 8-hour outage indistinguishable from transient
# contention while commits piled up locally. The fix is the same SUBTRACTIVE exclusion
# shape already proven in _engine/rebar_reconciler/_ref_lock.py (bug 4afc): a broad
# marker counts only when nothing names a non-mergeable cause.
_POLICY_DECLINE_MARKERS = (
    "hook declined",  # pre-receive / update hook (incl. GitHub push protection GH013)
    "push declined",
    "protected branch",
    "branch protection",
    "internal server error",
    "rate limit",
    "gh0",  # GitHub push-protection / policy error codes: GH006, GH013, ...
)


def _is_policy_decline(stderr: str) -> bool:
    """Whether the remote explicitly declined the push for a policy reason."""
    return any(marker in stderr.lower() for marker in _POLICY_DECLINE_MARKERS)


def _is_non_fast_forward(stderr: str) -> bool:
    """Whether *stderr* shows a genuine non-fast-forward (retriable by fetch+merge).

    A policy decline also carries the word ``rejected``, so it must be excluded
    explicitly; ambiguity resolves to TERMINAL (report the reason once) rather than
    to a retry loop that provably cannot converge (bug 2a76).
    """
    if _is_policy_decline(stderr):
        return False
    return bool(_NON_FF.search(stderr))


def _is_multi_bundle(stderr: str) -> bool:
    """Whether *stderr* shows the git-remote-s3 multi-bundle state (a ref with two bundles).

    True when the message reports ``multiple bundles`` or ``multiple updates for ref``
    (case-insensitive); False for a plain non-fast-forward or a transport error.
    """
    low = stderr.lower()
    return "multiple bundles" in low or "multiple updates for ref" in low


# Bug f61c: a TRANSPORT fault is not a permanent rule violation. The terminal branch below
# was written for policy declines ("hitting the remote twice more cannot change a permanent
# rule violation") but swallowed transport faults too, so a converged reconcile pass was
# abandoned after ONE blip: an observed CI pass died on `server certificate verification
# failed. CAfile: none CRLfile: none` (the hosted runner's CA bundle not resolving) with 3
# unpushed commits, and another lost merge recovery the same way. Those commits die with the
# runner: a converged pass whose events never reach origin/tickets is silently lost work.
#
# Same SUBTRACTIVE shape as _is_non_fast_forward (bug 2a76) and _ref_lock (bug 4afc): a policy
# decline can never be transport-retriable, and ambiguity resolves to TERMINAL rather than to a
# retry loop that provably cannot converge.
_TRANSPORT_RETRIABLE_MARKERS = (
    "server certificate verification failed",  # runner CA bundle unresolved (CAfile: none)
    "ssl certificate problem",
    "gnutls_handshake",
    "openssl ssl_read",
    "could not resolve host",
    "connection reset by peer",
    "connection timed out",
    "operation timed out",
    "failed to connect",
    "empty reply from server",
    "the remote end hung up unexpectedly",
    "early eof",
    "rpc failed",
    "unable to access",  # git's generic transport preamble (curl/http layer)
    "from promisor remote",  # blob:none partial clone: on-demand fetch hit the network
    "git timed out after",  # the synthetic _GIT_TIMEOUT CompletedProcess (returncode 124)
)


def _is_transport_retriable(stderr: str) -> bool:
    """Whether *stderr* shows a TRANSIENT transport fault worth another attempt.

    False for a policy decline (permanent) and for a non-fast-forward (which has its own
    fetch+merge recovery path); ambiguity resolves to False, i.e. terminal.
    """
    if _is_policy_decline(stderr):
        return False
    low = stderr.lower()
    return any(marker in low for marker in _TRANSPORT_RETRIABLE_MARKERS)


_DIRTY_WD = re.compile(
    r"would be overwritten by merge|local changes.*would be overwritten", re.IGNORECASE
)
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
