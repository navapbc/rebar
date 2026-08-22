"""Durable, machine-local record of a tickets-branch push that did not land.

Bug ``vapoury-attack-lamb`` (RC-3 of ``2a76-c9d9-4e8a-471b``). ``2a76`` made the
terminal push-failure WARNING informative — it now names the git rejection reason and
the unpushed-commit backlog. That fix is a no-op on three surfaces, because on them the
warning is not merely content-free, it is never DELIVERED at all:

* ``sync.push = async`` re-spawns the push as a detached child whose ``stdout``/``stderr``
  are ``/dev/null`` (``push.py``), so every warning it emits is discarded by the OS;
* a **library** embedder gets a ``NullHandler`` on the ``rebar`` root (``rebar/__init__``)
  — ``install_stderr_handler`` is called only by the CLI and the MCP server;
* an **MCP** client reads only the tool result. Measured on a real rejecting origin, the
  ``comment_ticket`` tool returned ``{"result": "ok"}`` while two ticket commits sat
  stranded;
* the **reconciler** subprocess installs its handler on the ``rebar_reconciler`` root,
  while this package logs under ``rebar``.

A log line is therefore the wrong carrier. This module records the outcome as STATE: a
marker file that outlives the process that failed, so any later write or read can report
"your ticket events are not on the remote" until a push actually succeeds.

**The best-effort contract is load-bearing and unchanged.** ``docs/concurrency.md`` makes
"a failed push never fails the caller" authoritative intent, pinned by
``push_tickets_branch``'s "ALWAYS returns None". Everything here is a SIGNAL, never an
exception: every entry point swallows its own errors, so an unwritable tracker dir or a
corrupt marker degrades to "no status" rather than turning a diagnostic into a crash.

The marker is LOCAL state, never a ticket event: it lives in the tracker's **git dir**, not
its working tree, so it is structurally incapable of being committed or pushed — a record
that the remote is unreachable must not itself need the remote.

The git dir specifically, rather than a dot-file beside the events: ``push.py`` sets a
dirty working tree ASIDE (stash-commit → merge → restore) to reconcile a non-fast-forward,
and existing tests pin that a failed push leaves the tracker's ``git status`` byte-identical
(``test_commit_and_push_tickets_branch_heldout``). A marker in the working tree perturbs
exactly the state that dance operates on and shows up as an untracked file in every store.
``.git/`` is outside all of it — invisible to ``git status``, to the stash dance, and to
``fsck``'s foreign-path scan.
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

#: The marker file's name, inside the tracker's git dir. JSON content, but deliberately
#: NO ``.json`` suffix: the store's own guards enumerate ticket events with
#: ``tracker.rglob("*.json")`` (e.g. test_push_rejection_surfacing's "no event was
#: written" assertion), and rglob descends into the git dir. An extension-less name keeps
#: this local artifact from being counted as a ticket event by anything that scans for one.
MARKER = "rebar-push-pending"

#: ``PushDeliveryError`` reasons that are NOT delivery failures — the cases where no push
#: was ever ATTEMPTED, so there is no backlog in flight to report:
#:
#: * ``push-disabled`` — the operator set ``sync.push = off``;
#: * ``async-delivery-unobservable`` — raised in the PARENT of a detached push the instant
#:   it hands off, before the child has succeeded or failed; the child records its own
#:   outcome, so recording here would report a failure that has not happened;
#: * ``remote-not-found`` / ``invalid-destination`` — no usable ``sync.remote`` is
#:   configured. ``push.py`` skips these deliberately ("a local-only store is a supported
#:   mode"), and a store that never intended to publish is not a store whose publish failed.
#:
#: Recording any of them would leave a perfectly healthy store permanently pending — and a
#: signal that is always on is one operators learn to ignore, which would make this fix
#: worse than the defect it addresses. Measured: without the last pair, every local-only
#: store (and every test fixture) reported ``pending`` with ``reason=remote-not-found``.
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
    """``<remote_ref>..HEAD`` commit count as a string, or ``"unknown"``.

    Relocated here from ``push.py`` (bug 2a76 added it there): the size of the local
    backlog IS push state, and ``push.py`` sits against the hard 800-LOC module cap.
    Best-effort by construction — the remote-tracking ref can legitimately be absent on
    a store that has never fetched, and a diagnostic must never fail a best-effort push.
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
    """A ``" (N unpushed commits …)"`` suffix for a terminal push-failure report.

    Bug 2a76: without it every failed write logged a byte-identical line, so a permanent
    outage looked like the same transient blip repeating. The count makes the backlog
    ESCALATE across successive failed writes, which is the signal an operator (or fsck's
    ``PUSH_PENDING``) acts on.

    Bug 3ff9 (squeamish-halfawake-fantail): the wording names the tickets branch and
    states the self-healing contract instead of the bare ``{remote_ref}..HEAD`` range —
    the tracker's HEAD reads to an agent session as its code worktree's HEAD, and nothing
    said the backlog publishes by itself, so sessions adopted the shared backlog as their
    own emergency and burned tokens investigating a state that heals on the next write.
    """
    return (
        f" ({unpushed_count(base, remote_ref)} unpushed commits on the local tickets branch"
        f" ahead of {remote_ref}; committed locally, they publish on the next successful push)"
    )


def backlog_grew(tracker: str, remote_ref: str) -> bool:
    """Whether the unpushed backlog GREW since the previously recorded failure.

    Bug 3ff9 (squeamish-halfawake-fantail): the level of a terminal best-effort push
    report keys on this — a lost contention race whose backlog is not growing is
    expected and self-healing (INFO material), while growth across successive failures
    is the persistent-outage signal bug 2a76 kept loud. Reads the PREVIOUS marker, so
    it must run BEFORE :func:`record_failure` overwrites it. Ambiguity — no prior
    marker, an ``unknown`` or corrupt count — resolves to ``False``: growth must be
    PROVEN before a report escalates to operator-loud. Never raises (both reads are
    best-effort by construction).
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
    """Record that a tickets-branch write did not reach the remote. Never raises.

    A no-op for the reasons in :data:`NON_FAILURE_REASONS`. Overwrites any existing
    marker, so the recorded reason is always the most recent failure and ``unpushed``
    re-counts (the backlog escalates across successive failed writes).

    ``reason`` spans the whole delivery path, not only the push itself: the commit leg's
    ``commit-failed`` / ``stage-failed`` / ``commit-lock-timeout`` reach here too, and are
    recorded DELIBERATELY. A write that never got committed also never got delivered, and
    it is invisible on exactly the same three surfaces — the caller is better served by
    ``pending`` plus a reason naming the commit leg than by silence. ``reason`` is what
    disambiguates; a later successful push clears the marker either way.
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
    """The store's current push-delivery status. Never raises.

    Returns ``{"state": "ok"}`` when the last push landed (or none has failed since the
    last success), otherwise the recorded ``{"state": "pending", "reason", "detail",
    "remote_ref", "unpushed", "since"}``. ``tracker`` defaults to the configured store for
    the current repo, so a library embedder can call this with no arguments.

    An unreadable or corrupt marker reports ``ok``: this is a diagnostic, and a broken
    diagnostic must not be able to convince a caller that a healthy store is broken.
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
