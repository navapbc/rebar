"""Persist push failures where CLI, library, MCP, and detached callers can read them.

Those callers cannot rely on a warning: detached output is discarded, library logging
has a NullHandler, MCP returns tool results, and reconciler logging uses another root.
The marker survives the failed process until a successful push clears it. Recording or
reading it is best effort and never turns a failed push into a caller exception.

The marker lives in the tracker's git dir, outside ticket events and the working tree.
This keeps it out of commits, ``git status``, non-fast-forward recovery, and foreign-path
scans; a delivery-failure record must not itself require delivery.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from rebar._store import fsutil
from rebar._store.paths import StorePaths

logger = logging.getLogger(__name__)

#: The extensionless git-dir marker avoids event scans using ``rglob("*.json")``.
MARKER = "rebar-push-pending"

#: No push was attempted for these reasons. The detached parent's handoff is not the
#: child's outcome, and disabled or local-only stores must not remain falsely pending.
NON_FAILURE_REASONS = frozenset(
    {
        "push-disabled",
        "async-delivery-unobservable",
        "remote-not-found",
        "invalid-destination",
    }
)

#: Cap on the stored git stderr. A decline body is a few lines; the bound stops a
#: pathological remote from writing an unbounded file into the tracker.
_DETAIL_LIMIT = 2000


def _marker_path(tracker: str | os.PathLike[str]) -> str:
    return os.path.join(StorePaths(tracker).git_dir, MARKER)


# raw-git-ok: locked store seam internal
def unpushed_count(base: str, remote_ref: str) -> str:
    """Return the unpushed commit count, or ``"unknown"`` if the ref is unavailable.

    This diagnostic must not fail a best-effort push.
    """
    from rebar._store.push import _git

    try:
        res = _git(base, "rev-list", "--count", f"{remote_ref}..HEAD")
        count = (res.stdout or "").strip()
        if res.returncode != 0 or not count.isdigit():
            return "unknown"
        return count
    except Exception:  # noqa: BLE001 — diagnostics must never fail a best-effort push
        return "unknown"


def unpushed_summary(base: str, remote_ref: str) -> str:
    """Describe the tickets-branch backlog and its next-successful-push recovery.

    The count distinguishes a growing outage from repeated identical failures; naming
    the tickets branch avoids confusing its HEAD with a code worktree's HEAD.
    """
    return (
        f" ({unpushed_count(base, remote_ref)} unpushed commits on the local tickets branch"
        f" ahead of {remote_ref}; committed locally, they publish on the next successful push)"
    )


def backlog_grew(tracker: str, remote_ref: str) -> bool:
    """Compare the current backlog with the previous failure marker before replacing it.

    Escalate only proven growth; missing or unreadable counts return ``False``. Never
    raise during best-effort delivery.
    """
    prior = read_status(tracker)
    if prior.get("state") != "pending":
        return False
    prior_count = str(prior.get("unpushed", ""))
    current = unpushed_count(tracker, remote_ref)
    if not (prior_count.isdigit() and current.isdigit()):
        return False
    return int(current) > int(prior_count)


def record_failure(tracker: str, reason: str, detail: str, remote_ref: str) -> None:
    """Record the latest failed delivery and backlog without raising.

    Ignore non-failure reasons. Commit and staging failures also count as undelivered
    writes; their reason distinguishes them from push failures. A later successful push
    clears either marker.
    """
    if reason in NON_FAILURE_REASONS:
        return
    try:
        payload = {
            "state": "pending",
            "reason": reason,
            "detail": (detail or "").strip()[:_DETAIL_LIMIT],
            "remote_ref": remote_ref,
            "unpushed": unpushed_count(tracker, remote_ref),
            "since": time.time(),
        }
        fsutil.atomic_write(_marker_path(tracker), json.dumps(payload, indent=2) + "\n")
    except Exception:
        logger.debug("could not record the push-pending marker", exc_info=True)


def clear(tracker: str) -> None:
    """Drop the marker after a push that landed. Never raises.

    Symmetric with :func:`record_failure`; without it the signal would latch on and every
    subsequent healthy write would keep reporting a long-resolved outage.
    """
    try:
        os.remove(_marker_path(tracker))
    except FileNotFoundError:
        pass  # the overwhelmingly common case: nothing was pending
    except Exception:
        logger.debug("could not clear the push-pending marker", exc_info=True)


def read_status(tracker: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Read the latest delivery status from the configured store by default.

    Return ``ok`` for a missing, unreadable, or corrupt marker; diagnostics must not
    report a false delivery failure or raise.
    """
    try:
        if tracker is None:
            from rebar.config import tracker_dir

            tracker = tracker_dir()
        with open(_marker_path(tracker), encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict) or payload.get("state") != "pending":
            return {"state": "ok"}
        return payload
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "ok"}
    except Exception:
        logger.debug("could not read the push-pending marker", exc_info=True)
        return {"state": "ok"}
