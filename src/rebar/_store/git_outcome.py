"""One git failure classifier for the tickets store's git operations.

Before this module the same git stderr was classified at five sites with five private
marker tables, and the SAME text got different verdicts at each. ``cannot lock ref``
alone carries three deliberately different, bug-hardened verdicts. So the tables live
HERE — one registry, one place to add a marker — while the VERDICTS stay per-operation:
the registry is keyed ``(marker, operation)``, never ``marker`` alone.

Two entry kinds:

* a **marker row** — the common case: a substring (or regex) of the failure text maps to
  a :class:`GitKind` for one operation;
* a **structural predicate** — registered for an operation and handed the whole outcome
  object, because some verdicts are not marker-based at all.
  :func:`is_cas_mismatch` (the reconciler's ``update-ref`` discriminator) is the proof
  case: it reads the command SHAPE and the EXIT CODE, which no ``(marker, operation) ->
  kind`` table can express.

What this module deliberately does NOT do is merge the divergent verdicts. Collapsing
``cannot lock ref`` into one kind would re-open bug 4afc (only ``stale info`` is the
``--force-with-lease`` signal) and bug ebee (``fatal error in commit_refs`` is a GitHub
5xx ref-transaction fault, not lease movement). The consolidation is of the marker
STRINGS, not of the judgements made from them.

The synthetic rc-124 timeout RESULT is a different construct with a different owner:
:func:`rebar._store.gitutil.run_git_bounded` builds it, because that is where the shared
runner lives. Only the marker row that RECOGNISES it lives here.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class GitKind(Enum):
    """What a failed git invocation actually was, for one operation."""

    LOCK = "lock"
    TRANSIENT_FS = "transient-fs"
    TRANSPORT = "transport"
    NON_FF = "non-ff"
    CAS_MISMATCH = "cas-mismatch"
    POLICY_DECLINE = "policy-decline"
    DIRTY_WD = "dirty-wd"
    UNMERGED = "unmerged"
    INVALID_OBJECT = "invalid-object"
    FATAL = "fatal"


# ── Operations ─────────────────────────────────────────────────────────────────
# The classification CONTEXT. The same marker means different things in different
# ones, which is exactly why the registry is keyed by the pair.
LOCAL = "local"  # gitutil's store-local git ops (add/commit/reset on the tracker)
COMMIT = "commit"  # event_append's lock-held write-tree/commit
PUSH = "push"  # push.py's network push of the tickets branch
LEASE_PUSH = "lease-push"  # the reconciler's --force-with-lease ref push
REF_CAS = "ref-cas"  # the reconciler's update-ref compare-and-swap
OPERATIONS: tuple[str, ...] = (LOCAL, COMMIT, PUSH, LEASE_PUSH, REF_CAS)


@dataclass(frozen=True)
class GitOutcome:
    """The verdict for one failed git invocation under one operation."""

    kind: GitKind
    operation: str
    marker: str | None = None


# ── Marker tables (moved here verbatim; the comments travel with them) ─────────

# git refuses ``git add``/``git commit`` with "Unable to create '<gitdir>/index.lock':
# File exists. Another git process seems to be running …" when a peer (or a crashed git
# that left a stale lock) holds the index. A CONTENDED lock clears on retry.
INDEX_LOCK_MARKER = "index.lock"
INDEX_LOCK_COMPANIONS: tuple[str, ...] = ("file exists", "another git process")
# A ref lock, or another ``<name>.lock`` create conflict (``HEAD.lock`` /
# ``packed-refs.lock`` / ``config.lock``). Only index.lock gets the stale-reclaim
# treatment; the others are purely ridden out (git holds ref locks for microseconds).
REF_LOCK_MARKER = "cannot lock ref"
GENERIC_LOCK_MARKER = ".lock'"

# The READ-side runner-FS transient: git resolves HEAD before writing and aborts with
# ``fatal: could not parse HEAD`` (exit 128) BEFORE mutating anything, so the identical
# invocation succeeds on retry (bug childsafe-special-springtail).
TRANSIENT_HEAD_MARKERS: tuple[str, ...] = ("could not parse head",)
# The object-DB analogue: ``parse_object()`` returned NULL, so git aborts with ``fatal:
# bad object <name>`` having read and written nothing (bug wrongful-chemic-squeaker).
# The name in the message carries no information, so the marker matches the fault.
# Deliberately does NOT cover git's CORRUPT-object signatures, which are real damage.
TRANSIENT_OBJECT_MARKERS: tuple[str, ...] = ("bad object",)
# The WRITE-side members of the same family, all transient runner-FS hiccups (NOT data
# faults): the loose-object temp create under ``.git/objects/`` intermittently fails (ENOENT
# on Linux, EINVAL on macOS) — bugs vocal-dip-robin / brainy-floral-globefish — and git's
# lockfile-commit of the INDEX itself (``read-cache.c`` ``write_locked_index`` via
# ``commit_lock_file``, emitted by ``git add`` / ``git write-tree`` / a commit's
# pre-ref-update index prep) intermittently fails with ``unable to write new index file``,
# which a production ``rebar create`` surfaced as a hard write requiring an operator retry
# (bug scary-fiscal-grunion). The pre-ref-update marker is deliberately the ONLY index-write
# phrase here: git's POST-ref-update failure is the DISTINCT ``repository has been updated,
# but unable to write new_index file`` (underscore), which this substring cannot match — so a
# match provably means HEAD had not moved and retrying cannot duplicate a committed event.
TRANSIENT_WRITE_MARKERS: tuple[str, ...] = (
    "unable to create temporary file",
    "failed to insert into database",
    "unable to index file",
    "unable to write new index file",
)

# Bug 2a76: the bare token ``rejected`` in the non-FF pattern is NOT specific to a
# non-fast-forward — git prints ``! [remote rejected] … (pre-receive hook declined)`` for
# EVERY server-side decline. Those are PERMANENT: a fetch+merge cannot fix them, so
# classifying them as non-fast-forward burned all three retries and reported only "failed
# after 3 retries". The fix is the SUBTRACTIVE exclusion shape proven in _ref_lock (bug
# 4afc): a broad marker counts only when nothing names a non-mergeable cause.
POLICY_DECLINE_MARKERS: tuple[str, ...] = (
    "hook declined",  # pre-receive / update hook (incl. GitHub push protection GH013)
    "push declined",
    "protected branch",
    "branch protection",
    "internal server error",
    "rate limit",
    "gh0",  # GitHub push-protection / policy error codes: GH006, GH013, ...
)
NON_FF_RE = re.compile(r"non-fast-forward|rejected|fetch first", re.IGNORECASE)

# Bug f61c: a TRANSPORT fault is not a permanent rule violation. Same SUBTRACTIVE shape:
# a policy decline can never be transport-retriable, and ambiguity resolves to TERMINAL
# rather than to a retry loop that provably cannot converge.
TRANSPORT_RETRIABLE_MARKERS: tuple[str, ...] = (
    "server certificate verification failed",  # runner CA bundle unresolved
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
    # The synthetic rc-124 result gitutil.run_git_bounded builds for a watchdog timeout.
    # Recognised HERE; CONSTRUCTED there (two constructs, two owners).
    "git timed out after",
)

MULTI_BUNDLE_MARKERS: tuple[str, ...] = ("multiple bundles", "multiple updates for ref")
DIRTY_WD_RE = re.compile(
    r"would be overwritten by merge|local changes.*would be overwritten", re.IGNORECASE
)
# git refuses a commit/merge outright while the index holds unmerged (UU) entries.
UNMERGED_MARKERS: tuple[str, ...] = (
    "unmerged files",
    "unmerged paths",
    "unresolved conflict",
    "you need to resolve your current index first",
)
# git ``write-tree`` refuses the commit with this signature when an index entry
# references an object MISSING from the object DB. Its handler is a RECOVERY (rebuild
# the index and retry), not a terminal failure — which is why it is its own kind and
# not folded into FATAL (bug 4c1c).
INVALID_OBJECT_MARKERS: tuple[str, ...] = ("invalid object", "error building trees")

# Bug 4afc: only "stale info" is the --force-with-lease signal; ``rejected`` and
# ``cannot lock ref`` also cover hook declines, rate limits, server errors and ref.lock
# contention, which are not lease movement. Bug ebee added ``fatal error in commit_refs``
# (a GitHub 5xx ref-transaction fault) to the same non-CAS set.
LEASE_MISMATCH_MARKER = "stale info"
PUSH_REJECT_MARKERS: tuple[str, ...] = ("stale info", "rejected", REF_LOCK_MARKER)
LEASE_NON_CAS_ROWS: tuple[tuple[str, GitKind], ...] = (
    ("file exists", GitKind.LOCK),  # server-side ref.lock contention
    ("hook declined", GitKind.POLICY_DECLINE),
    ("internal server error", GitKind.POLICY_DECLINE),
    ("rate limit", GitKind.POLICY_DECLINE),
    ("fatal error in commit_refs", GitKind.FATAL),  # bug ebee
    ("(failure)", GitKind.FATAL),
)
NON_CAS_REJECT_MARKERS: tuple[str, ...] = tuple(m for m, _ in LEASE_NON_CAS_ROWS)


# ── Atom predicates (the single definition every caller's predicate delegates to) ──


def _any(text: str, markers: tuple[str, ...]) -> str | None:
    low = text.lower()
    for marker in markers:
        if marker in low:
            return marker
    return None


def is_index_lock(text: str) -> bool:
    """git's index.lock-contention signature (case-insensitive)."""
    low = text.lower()
    return INDEX_LOCK_MARKER in low and any(c in low for c in INDEX_LOCK_COMPANIONS)


def is_git_lock(text: str) -> bool:
    """ANY git lock-conflict signature: index.lock contention, a ref lock, or another
    ``<name>.lock`` create conflict."""
    if is_index_lock(text):
        return True
    low = text.lower()
    return REF_LOCK_MARKER in low or (GENERIC_LOCK_MARKER in low and "file exists" in low)


# The concurrent ref compare-and-swap mismatch a RACING ref-updating fetch leaves behind:
# ``cannot lock ref '<ref>': is at <new> but expected <old>``. A peer advanced the
# remote-tracking ref between this fetch's negotiation and its ref update, so re-reading and
# retrying converges (bug agrologic-oval-bobolink). Distinct from a stuck ``<name>.lock``
# create conflict (:func:`is_git_lock`), which is a still-HELD lock file, not ref MOVEMENT —
# the two are DIFFERENT outcomes (a lock file is ridden out where it lives; the CAS mismatch
# is serialized by the common-dir fetch lock and bounded-retried by the fetch callers).
_REF_CAS_MISMATCH_RE = re.compile(
    r"cannot lock ref.*\bis at\b.*\bbut expected\b", re.IGNORECASE | re.DOTALL
)


def is_ref_cas_mismatch(text: str) -> bool:
    """True for the concurrent ref compare-and-swap mismatch (``cannot lock ref '<ref>':
    is at <new> but expected <old>``) that two uncoordinated ref-updating fetches produce."""
    return _REF_CAS_MISMATCH_RE.search(text) is not None


def is_transient_object_read(text: str) -> bool:
    """git's transient object-DB READ signature."""
    return _any(text, TRANSIENT_OBJECT_MARKERS) is not None


def is_transient_object_write(text: str) -> bool:
    """git's transient object-DB WRITE signature (loose-object temp create)."""
    return _any(text, TRANSIENT_WRITE_MARKERS) is not None


def is_transient_fs(text: str) -> bool:
    """Any transient runner-FS git signature: the READ-side HEAD-parse and ``bad
    object`` faults, or the WRITE-side loose-object temp-create fault."""
    return (
        _any(text, TRANSIENT_HEAD_MARKERS) is not None
        or is_transient_object_read(text)
        or is_transient_object_write(text)
    )


def is_policy_decline(text: str) -> bool:
    """The remote explicitly declined the push for a policy reason (PERMANENT)."""
    return _any(text, POLICY_DECLINE_MARKERS) is not None


def is_non_fast_forward(text: str) -> bool:
    """A genuine non-fast-forward (retriable by fetch+merge).

    A policy decline also carries the word ``rejected``, so it is excluded explicitly;
    ambiguity resolves to TERMINAL rather than to a retry loop that cannot converge.
    """
    if is_policy_decline(text):
        return False
    return bool(NON_FF_RE.search(text))


def is_transport_retriable(text: str) -> bool:
    """A TRANSIENT transport fault worth another attempt. False for a policy decline."""
    if is_policy_decline(text):
        return False
    return _any(text, TRANSPORT_RETRIABLE_MARKERS) is not None


def is_multi_bundle(text: str) -> bool:
    """The git-remote-s3 multi-bundle state (a ref with two bundles)."""
    return _any(text, MULTI_BUNDLE_MARKERS) is not None


def is_dirty_working_tree(text: str) -> bool:
    """git refused the merge because local changes would be overwritten."""
    return bool(DIRTY_WD_RE.search(text))


def is_unmerged(text: str) -> bool:
    """git refused the operation because the index holds unmerged (UU) entries."""
    return _any(text, UNMERGED_MARKERS) is not None


def is_invalid_object(text: str) -> bool:
    """``write-tree`` refused: an index entry references a MISSING object."""
    return _any(text, INVALID_OBJECT_MARKERS) is not None


def is_lease_mismatch(text: str) -> bool:
    """The ``--force-with-lease`` lease MOVED, not merely a rejection.

    ``stale info`` is conclusive; a broader marker counts only when nothing names a
    non-lease cause, so ambiguity fails closed per the documented posture (bug 4afc).
    """
    return classify_text(text, operation=LEASE_PUSH).kind is GitKind.CAS_MISMATCH


def is_cas_mismatch(
    exc: subprocess.CalledProcessError, ref_name: str = "refs/heads/tickets"
) -> bool:
    """Return True iff *exc* is an ``update-ref`` compare-and-swap old-sha mismatch.

    ``git update-ref <ref> <new> <old>`` (create-only or advance) reports a CAS
    old-sha mismatch as **exit 128**; the delete form ``git update-ref -d <ref>
    <old>`` reports it as **exit 1**. Both carry ``cannot lock ref '<ref>'`` in
    stderr, so we accept exit 128 OR an exit-1 ``cannot lock ref`` — a strict superset
    that never misclassifies an unrelated failure. We discriminate on the command
    shape (an ``update-ref`` invocation naming *ref_name*) so an unrelated exit-128
    from some other git command is not treated as a retryable race.

    This is the registry's ``ref-cas`` STRUCTURAL predicate: it reads the command shape
    and the exit code, which no ``(marker, operation) -> kind`` table can express. Moved
    here verbatim from ``_advisory_lock``, which re-exports it under the same name.
    """
    args = exc.cmd or []
    is_update_ref = "update-ref" in args and ref_name in args
    if not is_update_ref:
        return False
    if exc.returncode == 128:
        return True
    stderr = getattr(exc, "stderr", "") or ""
    return REF_LOCK_MARKER in stderr


# ── The registry ───────────────────────────────────────────────────────────────

_Rule = Callable[[str], tuple[GitKind, str] | None]


def _row(predicate: Callable[[str], bool], kind: GitKind, marker: str) -> _Rule:
    return lambda text: (kind, marker) if predicate(text) else None


def _marker_row(marker: str, kind: GitKind) -> _Rule:
    """A marker row: *marker* (case-insensitive substring) means *kind* for this operation."""

    def rule(text: str) -> tuple[GitKind, str] | None:
        return (kind, marker) if marker in text.lower() else None

    return rule


def _lease_rules() -> tuple[_Rule, ...]:
    """Ordered exactly as the subtractive lease classifier reads: the conclusive lease
    marker first, then every non-CAS cause, then the broad reject markers."""
    rules: list[_Rule] = [_marker_row(LEASE_MISMATCH_MARKER, GitKind.CAS_MISMATCH)]
    rules += [_marker_row(marker, kind) for marker, kind in LEASE_NON_CAS_ROWS]
    rules += [_marker_row(marker, GitKind.CAS_MISMATCH) for marker in PUSH_REJECT_MARKERS]
    return tuple(rules)


_RULES: dict[str, tuple[_Rule, ...]] = {
    LOCAL: (
        _row(is_git_lock, GitKind.LOCK, REF_LOCK_MARKER),
        _row(is_transient_fs, GitKind.TRANSIENT_FS, "transient-fs"),
        _row(is_invalid_object, GitKind.INVALID_OBJECT, INVALID_OBJECT_MARKERS[0]),
        _row(is_unmerged, GitKind.UNMERGED, UNMERGED_MARKERS[0]),
        _row(is_dirty_working_tree, GitKind.DIRTY_WD, "would be overwritten"),
    ),
    COMMIT: (
        _row(is_git_lock, GitKind.LOCK, REF_LOCK_MARKER),
        _row(is_transient_fs, GitKind.TRANSIENT_FS, "transient-fs"),
        _row(is_invalid_object, GitKind.INVALID_OBJECT, INVALID_OBJECT_MARKERS[0]),
        _row(is_unmerged, GitKind.UNMERGED, UNMERGED_MARKERS[0]),
    ),
    PUSH: (
        _row(is_policy_decline, GitKind.POLICY_DECLINE, POLICY_DECLINE_MARKERS[0]),
        _row(is_non_fast_forward, GitKind.NON_FF, "non-fast-forward"),
        _row(is_transport_retriable, GitKind.TRANSPORT, "transport"),
        _row(is_dirty_working_tree, GitKind.DIRTY_WD, "would be overwritten"),
        _row(is_transient_fs, GitKind.TRANSIENT_FS, "transient-fs"),
    ),
    LEASE_PUSH: _lease_rules(),
    REF_CAS: (),  # structural only — see _STRUCTURAL below
}

# Structural predicates: registered per operation, handed the WHOLE outcome object.
_STRUCTURAL: dict[str, Callable[[Any], GitOutcome | None]] = {
    REF_CAS: lambda result: (
        GitOutcome(GitKind.CAS_MISMATCH, REF_CAS, REF_LOCK_MARKER)
        if isinstance(result, subprocess.CalledProcessError) and is_cas_mismatch(result)
        else None
    ),
}


def _failure_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    return (getattr(result, "stderr", "") or "") + "\n" + (getattr(result, "stdout", "") or "")


def classify(result: Any, *, operation: str) -> GitOutcome:
    """Classify one git outcome under *operation*.

    *result* carries the FULL outcome — command args, returncode, stdout, stderr — not
    just stderr text, because some verdicts (the ``ref-cas`` one) discriminate on the
    command shape and the exit code rather than on a marker. A plain ``str`` is accepted
    for the marker-only operations.

    Unrecognised text is :attr:`GitKind.FATAL`: ambiguity resolves to TERMINAL, never to
    a retry loop that provably cannot converge.
    """
    if operation not in _RULES:
        raise ValueError(f"unknown git operation {operation!r}; expected one of {OPERATIONS}")
    structural = _STRUCTURAL.get(operation)
    if structural is not None:
        hit = structural(result)
        if hit is not None:
            return hit
    text = _failure_text(result)
    for rule in _RULES[operation]:
        row = rule(text)
        if row is not None:
            return GitOutcome(row[0], operation, row[1])
    return GitOutcome(GitKind.FATAL, operation, None)


def classify_text(text: str, *, operation: str) -> GitOutcome:
    """:func:`classify` for a caller that holds only the failure text."""
    return classify(text, operation=operation)
