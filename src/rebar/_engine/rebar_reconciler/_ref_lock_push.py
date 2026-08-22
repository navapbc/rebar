"""Remote (push) CAS for reconciler ref locks: did the LEASE move, or did the server fault?

The authoritative-on-remote half of :mod:`_ref_lock`. A rejected
``git push --force-with-lease=<ref>:<old>`` is the remote-side equivalent of an
``update-ref`` compare-and-swap mismatch, but git does **not** exit 128 for it — it exits 1
and explains itself in stderr. This module translates a rejected push into the same
exit-128 ``update-ref`` ``CalledProcessError`` shape the shared ``_is_cas_mismatch`` /
``_cas_once`` seam already understands, so the reconciler keeps exactly **one** CAS
discriminator (ADR 0031, "Three exit-128 outcomes, one classifier").

Why the classification is load-bearing
--------------------------------------

The verdict this module reaches is a claim about *concurrency*. "CAS mismatch" propagates
as ``LeaseLostError`` -> heartbeat abort -> the whole reconcile pass aborts, and an operator
reads it as **"a competing pass stole this lock"**. So a wrong verdict does not merely fail;
it sends someone hunting a concurrent holder that never existed. Two bugs were exactly this:

* **Bug 4afc** — the marker set was purely additive (``stale info``, ``rejected``,
  ``cannot lock ref``). Only ``stale info`` is the ``--force-with-lease`` signal; git also
  prints ``! [remote rejected]`` for pre-receive hook declines, quota and rate-limit
  rejections and server errors, and ``cannot lock ref ... File exists`` for ordinary
  server-side ref contention. None of those is lease movement. That bug also added the
  logging: the CAS branch used to raise *silently* while the fail-closed branch below it
  logged, leaving the one consequential claim with no evidence behind it.
* **Bug ebee** (``freeborn-dizzy-raven``) — a GitHub ref-transaction fault
  (``remote: fatal error in commit_refs`` / ``-> refs/... (failure)``) against a perfectly
  healthy lease was reported as a stolen lease, for the same reason.

Hence the three-tier shape of :func:`is_cas_mismatch_stderr`:

1. ``stale info`` is **conclusive** — a real lease move is always detected.
2. Otherwise a **subtractive veto**: any marker naming a non-lease cause wins outright.
3. Only then may a broad marker count.

Ambiguity therefore **fails closed** — an unrecognised failure is re-raised as itself rather
than upgraded into a claim that someone else holds the lock.

The ``core`` parameter
----------------------

Every entry point here takes the calling ``_ref_lock`` **module object** as its first
argument and resolves ``core._git``, ``core._REMOTE_TIMEOUT_SECS`` and ``core.logger``
through it at **call time**. That is deliberate and load-bearing, not ceremony:

* ``rebar._engine`` is not an importable package, and this package is routinely loaded by
  file path under more than one ``sys.modules`` key. Binding names at import time could
  capture a *different* module object than the caller's.
* Tests intercept git with ``monkeypatch.setattr(_ref_lock, "_git", ...)``. Late-binding
  through ``core`` is what keeps that patch point working across the module boundary.
* Logging via ``core.logger`` keeps the operator-facing evidence on the
  ``rebar_reconciler._ref_lock`` logger, where it has always been emitted.

It also means the dependency runs one way — ``_ref_lock`` -> this module — with no import
cycle: the parent hands itself down, and this module imports nothing from the package.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType

from rebar._store import git_outcome as _git_outcome

# The lease/reject marker tables live in the shared registry
# (:mod:`rebar._store.git_outcome`), which owns every git marker string. Re-exported here
# under their historical names because ``_ref_lock`` and its tests read them from this
# module. Bug 4afc: only "stale info" is the --force-with-lease signal; ``rejected`` and
# the ref-lock marker also cover hook declines, rate limits, server errors and ref.lock
# contention, which are not lease movement. Bug ebee added ``fatal error in commit_refs``
# to the same non-CAS set.
PUSH_REJECT_MARKERS = _git_outcome.PUSH_REJECT_MARKERS
LEASE_MISMATCH_MARKER = _git_outcome.LEASE_MISMATCH_MARKER
NON_CAS_REJECT_MARKERS: tuple[str, ...] = _git_outcome.NON_CAS_REJECT_MARKERS


def is_cas_mismatch_stderr(stderr: str) -> bool:
    """Whether *stderr* (lowercased) shows the LEASE moved, not merely a rejection.

    "stale info" is conclusive; a broader marker counts only when nothing names a
    non-lease cause, so ambiguity fails closed per the documented posture. A lookup
    against the shared registry under the ``lease-push`` operation — the SAME text
    classifies differently under ``local`` and ``ref-cas``, which is why the registry is
    keyed by the pair and this verdict is not merged with theirs.
    """
    return _git_outcome.is_lease_mismatch(stderr)


def push_lease_cas(
    core: ModuleType,
    repo_root: Path,
    ref: str,
    old_oid: str,
    remote: str,
    refspec: str,
) -> None:
    """Do a ``--force-with-lease=<ref>:<old>`` push of *refspec* to *remote*.

    Shared by acquire (``<new-oid>:<ref>``) and release (``:<ref>`` delete). A
    rejected lease (remote ref moved) is re-raised in the update-ref exit-128
    shape so the shared ``_cas_once`` seam classifies it as a CAS mismatch; a
    genuine transport failure is logged and re-raised (fail-closed).

    *core* is the calling ``_ref_lock`` module; ``_git``, ``_REMOTE_TIMEOUT_SECS``
    and ``logger`` are read from it at call time (see the module docstring).
    """
    logger = core.logger
    result = core._git(
        repo_root,
        ["push", f"--force-with-lease={ref}:{old_oid}", remote, refspec],
        timeout=core._REMOTE_TIMEOUT_SECS,
        check=False,
    )
    if result.returncode == 0:
        return
    stderr = (result.stderr or "").lower()
    if is_cas_mismatch_stderr(stderr):
        # Remote-side CAS mismatch — same shape the update-ref discriminator reads.
        # Bug 4afc: LOG the evidence — this branch aborts a pass by claiming the lease
        # was stolen, and previously raised silently while fail-closed below logged.
        logger.warning(
            "ref-lock: push to %s %s (expected oid %s) classified as CAS mismatch "
            "(lease moved) — stderr: %s",
            remote,
            ref,
            old_oid,
            (result.stderr or "").strip()[:200],
        )
        raise subprocess.CalledProcessError(128, ["git", "update-ref", ref])
    logger.warning(
        "ref-lock: git push to %s %s failed (exit %s) — fail-closed: %s",
        remote,
        ref,
        result.returncode,
        (result.stderr or "").strip()[:200],
    )
    raise subprocess.CalledProcessError(
        result.returncode, result.args, result.stdout, result.stderr
    )
