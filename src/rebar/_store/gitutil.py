"""One shared ``git`` subprocess wrapper.

A leaf helper (stdlib only at module level — no ``rebar.*`` import at all) that
consolidates the dozen hand-rolled ``_git()`` wrappers that had
drifted into a different signature/return/error contract each. Every wrapper ran
the identical shape underneath — ``subprocess.run(["git", "-C", cwd, *args],
capture_output=True, text=True, …)`` — so :func:`run_git` is that shape once, and
each call site keeps its OWN return/error contract by adapting the returned
:class:`subprocess.CompletedProcess` locally (inspect ``returncode``/``stdout``,
raise its own exception, translate a timeout, …).

It also owns the tracker's shared FILESYSTEM primitives — ``_resolve_tracker_git_dir``,
``_ticket_dirs`` and ``_dir_is_archived``. They previously lived in
``rebar._commands.fsck_repair``, which inverted the layering: this store-layer module had to
defer-import a command-layer *repair* module to answer 'where is the tracker's git dir', and
seven consumers reached into 'repair' for helpers that have nothing to do with repairing
(ticket b432-c9dc-c1b4-4a45). They are stdlib-only here; ``_dir_is_archived``'s reducer import
stays function-local so this module keeps its no-module-level-``rebar.*`` property.

NEVER ``shell=True`` — argv is a list, so a git argument can never be reinterpreted
by a shell. This helper does not redact: it returns the ``CompletedProcess``
verbatim, and any token/secret redaction stays where it already lives — in the
caller that formats ``stderr`` into a message.

**Blocking policy under local git lock contention (bug 9305-b42c, operator-ratified):
bounded jittered retry plus per-store advisory serialization.** rebar is
driven by many concurrent local agent processes, so git-lock conflicts (``index.lock`` /
ref locks) on the shared tickets store are the normal case, not an anomaly. The policy,
chosen against the OSS research recorded on ticket 9305 (git lockfile.c's jittered
backoff; JGit's own-fair-lock-before-the-file-lock; Mercurial's bounded wait; Bazel's
"print the holder, never parse it") and applied consistently at this seam:

* rebar's OWN index-mutating git ops on a store are serialized behind a per-store
  advisory ``flock`` (:func:`_store_git_op_lock`) — non-blocking when uncontended (zero
  added latency), kernel-released on holder death (no staleness logic to get wrong);
* a git lock-conflict signature is ridden out with a bounded, JITTERED backoff
  (:func:`_with_index_lock_retry`; total budget ~tens of seconds — see
  :func:`_lock_retry_budget_s`), resolving transient contention silently (debug log only);
* only a genuinely STUCK lock — the budget exhausted — surfaces, and then as ONE
  actionable error naming the contended lock file with holder guidance
  (:func:`_augment_lock_exhaustion`), never as raw git stderr alone.

Chosen over block-forever (cargo/Bazel) because rebar's callers are unattended agents
with their own deadlines, and over fail-fast because the observed conflicts resolve on
retry. Not config-tunable: no store/git config surface exists, and inert one-off config
keys are a known defect class here; tests tune the module-level constants.
"""

from __future__ import annotations

import fcntl
import logging
import os
import random
import re
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

logger = logging.getLogger(__name__)


# ── Tracker filesystem primitives ────────────────────────────────────────────
# Shared by fsck's diagnostic + repair paths, compact, bridge_repair,
# tracker_maintenance and this module's own lock handling. Pure filesystem: no git
# subprocess, no rebar module-level import (ticket b432-c9dc-c1b4-4a45).


def _resolve_tracker_git_dir(tracker: str) -> str:
    tracker_git = os.path.join(tracker, ".git")
    if os.path.isfile(tracker_git):
        with open(tracker_git, encoding="utf-8") as f:
            gitdir = f.read().strip()
        gitdir = gitdir[len("gitdir: ") :] if gitdir.startswith("gitdir: ") else gitdir
        if not gitdir.startswith("/"):
            gitdir = os.path.join(tracker, gitdir)
        return gitdir
    if os.path.isdir(tracker_git):
        return tracker_git
    return ""


def _dir_is_archived(ticket_path: str) -> bool:
    """True only when the ``.archived`` marker exists AND the event log net-confirms archival.

    The marker is a fast-path cache, never the decision: a stale marker (reverted archive, or
    a marker written without an ARCHIVED event) must not hide the ticket from store walks, so
    the log check (:func:`rebar.reducer._api._is_net_archived` — ARCHIVED uuids minus
    REVERT-targeted uuids) always confirms before a dir is skipped.

    The reducer import is deliberately function-local: it keeps this module free of any
    module-level ``rebar.*`` import, so no consumer can create an import cycle through it."""
    if not os.path.exists(os.path.join(ticket_path, ".archived")):
        return False
    from rebar.reducer._api import _is_net_archived

    return _is_net_archived(ticket_path)


def _ticket_dirs(tracker: str, *, include_archived: bool = False) -> list[str]:
    """The shared store-walk iterator: sorted ticket dirs, ACTIVE-only by default.

    Skips hidden dirs (.git, .bridge_state, …): the bash `"$TRACKER_DIR"/*/` glob never
    matched dot-dirs, and ticket ids never start with '.'. Archived tickets are excluded
    unless ``include_archived`` — an archive is terminal (the fold at archive time leaves no
    unfolded tail), so maintenance walks cost store ACTIVITY, not store history."""
    dirs = sorted(
        d
        for d in os.listdir(tracker)
        if not d.startswith(".") and os.path.isdir(os.path.join(tracker, d))
    )
    if include_archived:
        return dirs
    return [d for d in dirs if not _dir_is_archived(os.path.join(tracker, d))]


# raw-git-ok: locked store seam internal
def run_git(
    cwd: str | os.PathLike[str] | None,
    *args: str,
    check: bool = True,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    input_data: str | bytes | None = None,
) -> subprocess.CompletedProcess:
    """Run ``git -C <cwd> <args…>`` and return the :class:`subprocess.CompletedProcess`.

    A thin, uniform wrapper over :func:`subprocess.run` for the tickets-store git
    plumbing. Defaults match the historical wrappers' common shape (capture stdout
    and stderr, decode as text). ``check=True`` raises
    :class:`subprocess.CalledProcessError` on a non-zero exit (call sites that
    inspect ``returncode`` or raise their own error pass ``check=False``);
    ``timeout`` (when set) lets :class:`subprocess.TimeoutExpired` propagate — a
    caller that wants a timeout folded into a synthetic failed result catches it
    itself. ``env=None`` inherits the current environment.

    ``cwd=None`` omits the ``-C <cwd>`` prefix entirely, running ``git`` in the
    process CWD (some callers verify commits relative to the caller's directory
    rather than a fixed repo). ``input_data`` (when set) is fed to git's stdin —
    forwarded to :func:`subprocess.run`'s ``input`` for e.g. ``git hash-object``.

    Contract note: with ``text=True`` (the default), ``input_data`` must be ``str`` —
    :func:`subprocess.run` encodes text-mode stdin. Passing ``bytes`` with ``text=True``
    would otherwise fail deep in the stdlib with an opaque ``AttributeError: 'bytes'
    object has no attribute 'encode'``; this wrapper raises a clear :class:`TypeError`
    instead. Binary stdin requires ``text=False`` (then stdout/stderr are ``bytes`` too).
    """
    if text and isinstance(input_data, bytes):
        raise TypeError(
            "run_git: bytes input_data requires text=False (binary stdin cannot be "
            "encoded in text mode); pass text=False for binary stdin, or a str for text mode."
        )
    argv = ["git", *args] if cwd is None else ["git", "-C", cwd, *args]
    return subprocess.run(
        argv,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
        input=input_data,
    )


# The tickets tracker is a SHARED git worktree, so rebar's own write lock (which only
# serialises writes WITHIN one clone) does not stop a concurrent rebar process — or a
# crashed git that left a stale lock — from colliding on git's OWN ``.git/index.lock``.
# git then refuses ``git add``/``git commit`` with "Unable to create '<gitdir>/index.lock':
# File exists. Another git process seems to be running …". A CONTENDED lock (a live peer
# that releases quickly) clears on retry, so riding it out with a bounded backoff turns a
# hard write loss into a self-healed write. A STALE lock (a crashed git that never
# released) is reclaimed between attempts ONLY when provably old (mtime age >
# ``_INDEX_LOCK_STALE_S``) — never a young/live lock, whose removal can corrupt a peer's
# index; a young lock that never releases still ultimately FAILS the write. Same staleness
# threshold + resolution helper as fsck's Check 3 (bug fix-indexlock-retry). Shared here so
# EVERY index-mutating git op (event_append's add/commit AND txn.py's claim/transition
# add/commit) self-heals through the one implementation.
_INDEX_LOCK_STALE_S = 300  # a lock older than this is a crash remnant, safe to reclaim
# Bounded jittered backoff (bug 9305): gap(n) = min(base * 2^(n-1), cap) x jitter — the sum
# of the nominal inter-attempt gaps is ~21s (see _lock_retry_budget_s), the operator-ratified
# "tens of seconds" default, up from the pre-9305 ~2s that a peer's ordinary commit could
# outlast. Exponential-with-cap is git lockfile.c's own shape (quadratic-with-cap there).
_INDEX_LOCK_ATTEMPTS = 10
_INDEX_LOCK_BACKOFF_S = 0.2
_INDEX_LOCK_BACKOFF_CAP_S = 5.0


def _jitter(delay: float) -> float:
    """*delay* scaled by uniform [0.75, 1.25] — git lockfile.c's exact jitter shape
    ("back off for between 0.75*backoff_ms and 1.25*backoff_ms"), so competing processes
    de-synchronize instead of thundering in lockstep (9305 research rec #4)."""
    return delay * random.uniform(0.75, 1.25)


def _backoff_sleep(seconds: float) -> None:
    """rebar's own retry/poll backoff (bug 9305-b42c), behind one seam so a test can
    measure rebar's deliberate latency and not stdlib subprocess's timeout-poll sleep."""
    time.sleep(seconds)


def _lock_backoff_s(attempt: int) -> float:
    """The nominal (pre-jitter) backoff before retry *attempt* (1-based gap index)."""
    return min(_INDEX_LOCK_BACKOFF_S * (2 ** (attempt - 1)), _INDEX_LOCK_BACKOFF_CAP_S)


def _lock_retry_budget_s() -> float:
    """The nominal total wait the lock retry can spend backing off (the bounded budget a
    stuck lock exhausts before its one actionable error surfaces)."""
    return sum(_lock_backoff_s(n) for n in range(1, _INDEX_LOCK_ATTEMPTS))


def _is_index_lock_error(text: str) -> bool:
    """True if *text* is git's index.lock-contention signature (case-insensitive)."""
    low = text.lower()
    return "index.lock" in low and ("file exists" in low or "another git process" in low)


def _is_git_lock_error(text: str) -> bool:
    """True if *text* is ANY git lock-conflict signature: the index.lock contention above,
    a ref lock (``cannot lock ref``), or another ``<name>.lock`` create conflict
    (``HEAD.lock`` / ``packed-refs.lock`` / ``config.lock``). Only index.lock gets the
    stale-reclaim treatment; the others are purely ridden out (git's maintenance holds ref
    locks for microseconds — retry has a real chance; 9305 research §1a)."""
    if _is_index_lock_error(text):
        return True
    low = text.lower()
    return "cannot lock ref" in low or (".lock'" in low and "file exists" in low)


# git names the contended lock file in its own message ("Unable to create '<path>': File
# exists." / "cannot lock ref '<ref>'"); parse it so the exhaustion error can name it.
_LOCK_PATH_RE = re.compile(r"[Uu]nable to create '([^']+\.lock)'")
_LOCK_REF_RE = re.compile(r"cannot lock ref '([^']+)'")


def _augment_lock_exhaustion(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    """Fold rebar's actionable guidance into the FINAL still-locked failure: name the
    contended lock file (parsed from git's own message when present) and state the remedy.
    The existing call-site contracts (raise from ``stderr`` / print ``stderr``) then surface
    one actionable error with no per-site changes."""
    text = result.stderr or result.stdout or ""
    m = _LOCK_PATH_RE.search(text)
    if m:
        lock_name = m.group(1)
    else:
        ref = _LOCK_REF_RE.search(text)
        lock_name = f"ref '{ref.group(1)}'" if ref else "a git lock file"
    result.stderr = (result.stderr or "") + (
        f"\nrebar: git lock still contended after {_INDEX_LOCK_ATTEMPTS} attempts over "
        f"~{_lock_retry_budget_s():.0f}s (lock: {lock_name}). Another git process holds it; "
        "if no git process is running on this store, the lock file is stale — remove it "
        "and retry."
    )
    return result


# Per-store advisory git-op lock (bug 9305). rebar's store write lock already serializes
# the LOCKED write paths, but git ops also run outside it (sync/push fetch-merge legs,
# fsck repair, init) and non-rebar git can hold the same locks — so the seam additionally
# serializes rebar's OWN index-mutating git invocations behind one kernel flock per store.
# flock is dropped by the kernel when its holder dies: no staleness/reclamation logic to
# get wrong (the cargo/Bazel property, 9305 research §3). ADVISORY: on budget exhaustion
# we log and proceed WITHOUT it — correctness stays with the git-level retry below (JGit
# takes its in-process fair lock before the file lock for the same reason, §1c(b)).
_STORE_GIT_LOCK_NAME = "rebar-git-op.lock"
_STORE_GIT_LOCK_WAIT_S = 30.0
_STORE_GIT_LOCK_POLL_S = 0.05


def _store_git_lock_path(tracker: str) -> str | None:
    """The per-store advisory lock file path (inside the tracker's git dir, like git's own
    locks — never tracked), or ``None`` when *tracker* is not a git repo."""
    git_dir = _resolve_tracker_git_dir(tracker)
    if not git_dir:
        return None
    return os.path.join(git_dir, _STORE_GIT_LOCK_NAME)


@contextmanager
def _store_git_op_lock(tracker: str) -> Iterator[None]:
    """Hold the per-store advisory git-op lock for one git invocation (plus its retries).

    Fast path is a single non-blocking ``flock`` — zero added latency, NO sleep, when
    uncontended. When a peer holds it, poll with short jittered sleeps up to
    ``_STORE_GIT_LOCK_WAIT_S`` (debug log), then — advisory, not load-bearing — proceed
    without it at warning level: the bounded git-level lock retry still governs
    correctness, so a wedged peer degrades to pre-9305 behavior rather than a new failure
    mode. Best-effort on any OS fault (an unwritable git dir must not fail the git op)."""
    lock_path = _store_git_lock_path(tracker)
    if lock_path is None:
        yield
        return
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError:
        yield
        return
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            logger.debug("store git-op lock %s contended; waiting (bounded)", lock_path)
            deadline = time.monotonic() + _STORE_GIT_LOCK_WAIT_S
            while time.monotonic() < deadline:
                _backoff_sleep(_jitter(_STORE_GIT_LOCK_POLL_S))
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    continue
            if not acquired:
                logger.warning(
                    "store git-op lock %s still held after %.0fs; proceeding without "
                    "serialization (the bounded git lock retry still governs)",
                    lock_path,
                    _STORE_GIT_LOCK_WAIT_S,
                )
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# Test seam: a no-arg callable (default ``None`` = disabled) invoked inside
# ``_reclaim_if_stale_index_lock`` at the TOCTOU window — after the lock is judged stale and
# before the guarded unlink — so a test can deterministically inject a peer replacing the
# lock in that window (no sleeps). Production leaves this ``None``.
_reclaim_probe: Callable[[], None] | None = None


def _reclaim_if_stale_index_lock(tracker: str, *, force: bool = False) -> None:
    """Remove the tracker's git ``index.lock`` ONLY IF provably stale (mtime age >
    ``_INDEX_LOCK_STALE_S``). Best-effort and safe: a young/live lock (age <= threshold,
    or unstat-able, or absent) is LEFT in place — removing a lock a live peer holds can
    corrupt the index. Uses the shared git-dir resolution so the same lock path is meant.

    ``force=True`` bypasses the age gate: the caller HOLDS the exclusive store write lock, so
    no live rebar peer can hold ``index.lock`` — any present lock is provably an orphan left by
    an abnormally-terminated git (SIGKILL/OOM/FS-fault). The age gate exists only to protect
    UNLOCKED contexts from a live peer's lock; under the write lock a YOUNG orphan would
    otherwise wedge every write for the full 300s threshold (the Mode B catastrophic cascade).
    The device+inode identity re-validation below is STILL applied under force, so a TOCTOU
    replacement is never removed."""
    git_dir = _resolve_tracker_git_dir(tracker)
    if not git_dir:
        return
    lock_file = os.path.join(git_dir, "index.lock")
    try:
        st = os.stat(lock_file)
    except OSError:
        return  # no lock file (or unstat-able) → nothing to reclaim
    if not force and time.time() - st.st_mtime <= _INDEX_LOCK_STALE_S:
        return  # young/live lock (unlocked context) → never reclaim
    if _reclaim_probe is not None:
        _reclaim_probe()
    # Re-validate identity (device+inode) AND age at the moment of removal: a peer may
    # have removed our stale lock and dropped a fresh LIVE one at the same path in the
    # window since the stat above (the TOCTOU). Only unlink if it is STILL the same file
    # AND still stale — otherwise abort, leaving the peer's fresh lock intact.
    try:
        st2 = os.stat(lock_file)
    except OSError:
        return  # already gone (a peer reclaimed it first) → nothing to do
    if (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino):
        return  # replaced by a different file (a fresh lock) → do NOT remove
    if not force and time.time() - st2.st_mtime <= _INDEX_LOCK_STALE_S:
        return  # refreshed in place → now live → do NOT remove
    try:
        os.remove(lock_file)
    except OSError:
        pass


# Test seam: a callable (default ``None`` = disabled) invoked after EVERY ``run_once()``
# attempt inside ``_with_index_lock_retry`` with ``(attempt_number, result)`` — the initial
# pre-loop call fires as attempt 1, each in-loop retry as 2, 3, …. It lets a test count the
# real attempts and release a planted lock ONLY after the first failure is confirmed (a
# deterministic alternative to timer-based lock release). Production leaves this ``None`` so
# the call is skipped and behavior is unchanged.
_retry_probe: Callable[[int, subprocess.CompletedProcess], None] | None = None


def _with_index_lock_retry(
    tracker: str,
    run_once: Callable[[], subprocess.CompletedProcess],
    *,
    force_reclaim: bool = False,
) -> subprocess.CompletedProcess:
    """Run *run_once* (an index-mutating git invocation), retrying ONLY the index.lock
    contention signature with a bounded backoff. On success or a NON-lock failure the
    result is returned immediately (behavior unchanged — a real error still surfaces at
    once). Between lock retries a provably-stale lock is reclaimed; a young lock that
    never releases exhausts the attempts and its final failing result is returned. This
    is the composition seam: a caller that also retries a DIFFERENT signature (e.g.
    event_append's object-DB ``git add`` retry) passes its own inner loop as *run_once*.

    ``force_reclaim=True`` (passed by the store-write git helpers, which hold the exclusive
    write lock) reclaims a stranded index.lock regardless of age — under the write lock the
    lock is provably an orphan, so a YOUNG one is cleared on the first retry instead of
    wedging every write for the 300s stale threshold (the Mode B cascade).

    Bug 9305: the whole run (initial attempt + retries) holds the per-store advisory git-op
    lock, serializing rebar's own writers; the backoff is jittered exponential-with-cap to a
    ~tens-of-seconds budget (up from ~2s) and also retries git's REF-lock conflict signature
    (:func:`_is_git_lock_error`; only index.lock gets stale reclamation). A lock signature
    that survives the whole budget gets rebar's actionable guidance folded into its
    ``stderr`` (:func:`_augment_lock_exhaustion`); a transient one self-heals with at most a
    debug log."""
    with _store_git_op_lock(tracker):
        result = run_once()
        if _retry_probe is not None:
            _retry_probe(1, result)
        for attempt in range(1, _INDEX_LOCK_ATTEMPTS):
            if result.returncode == 0:
                return result
            failure_text = result.stderr or result.stdout or ""
            if not _is_git_lock_error(failure_text):
                return result
            logger.debug(
                "git lock contention on %s (attempt %d/%d): retrying after jittered backoff",
                tracker,
                attempt,
                _INDEX_LOCK_ATTEMPTS,
            )
            if _is_index_lock_error(failure_text):
                _reclaim_if_stale_index_lock(tracker, force=force_reclaim)
            _backoff_sleep(_jitter(_lock_backoff_s(attempt)))
            result = run_once()
            if _retry_probe is not None:
                _retry_probe(attempt + 1, result)
    if result.returncode != 0 and _is_git_lock_error(result.stderr or result.stdout or ""):
        return _augment_lock_exhaustion(result)
    return result


# git intermittently fails to READ ``HEAD`` on CI runners during an index-mutating op that
# must resolve it first — most often ``git commit``, which parses HEAD (``parse_commit``) to
# set the new commit's parent. Under a SHARED tracker worktree the HEAD commit's loose object
# in ``.git/objects/`` is transiently unreadable (a runner-FS read hiccup, NOT data
# corruption), and git aborts with ``fatal: could not parse HEAD`` (exit 128) BEFORE writing
# anything. It is the READ-side analogue of the WRITE-side object-DB temp-create
# transient (``_TRANSIENT_WRITE_MARKERS`` below): the identical invocation succeeds on retry — a
# re-run on the same state passes (a Gerrit ``recheck`` on the same patchset goes green) — so
# retrying ONLY this signature turns a runner-FS blip from a hard write loss (which red-lights
# unrelated CI) into a self-healed write. The op is safe to re-run because it failed at the
# HEAD-parse step before mutating the store (idempotent). Kept a tight, greppable marker set
# (only the proven ``could not parse HEAD`` signature) so it never masks a genuine error.
# Bug childsafe-special-springtail.
_TRANSIENT_HEAD_MARKERS = ("could not parse head",)
# The object-DB analogue of the HEAD-parse transient above, and the SAME fault class: git
# resolved the name to an OID but ``parse_object()`` returned NULL, so it aborts with
# ``fatal: bad object <name>`` having read nothing and written nothing. Proven on this runner
# fleet by three CI failures of the s3 doctor's bundle step (bug wrongful-chemic-squeaker),
# where the merged tip's loose object — whose sha git had just printed — was briefly
# unreadable, and the identical invocation succeeds on retry. The name in the message carries
# no information: the same fault was raised as both ``bad object HEAD`` and ``bad object
# <sha>``, so the marker matches the fault, not the argument. Deliberately does NOT cover
# git's CORRUPT-object signatures (``loose object … is corrupt``, ``inflate: data stream
# error``), which are real damage rather than a read blip; a genuinely broken object that
# happens to also report ``bad object`` still surfaces once the bounded retries are spent.
_TRANSIENT_OBJECT_MARKERS = ("bad object",)
# The WRITE-side member of the same runner-FS fault family: git's loose-object temp create
# under ``.git/objects/`` intermittently fails while writing a blob/tree/commit, returning
# ENOENT on Linux ("unable to create temporary file: No such file or directory") or EINVAL on
# macOS ("… Invalid argument"), surfaced as "failed to insert into database" / "unable to
# index file". A filesystem hiccup, NOT a data fault — the identical op succeeds on retry (a
# Gerrit ``recheck`` on the same patchset passes). Proven on ``git add`` by bugs
# vocal-dip-robin / brainy-floral-globefish, where these markers first lived privately in
# event_append; they moved here so EVERY caller of the shared write seam inherits the same
# self-heal instead of only the ``git add`` path (bug unheedful-custodial-bluebottle, filed
# after the s3 doctor's ``commit-tree`` hit this fault through :func:`run_git_write` and
# red-lit CI). One definition, one budget, no second dialect. Like the read-side markers this
# deliberately does NOT cover git's CORRUPT-object signatures, and a fault that outlives the
# bounded retries still surfaces.
_TRANSIENT_WRITE_MARKERS = (
    "unable to create temporary file",
    "failed to insert into database",
    "unable to index file",
)
_TRANSIENT_FAULT_ATTEMPTS = 3
_TRANSIENT_FAULT_BACKOFF_S = 0.1


def is_transient_object_read_error(text: str) -> bool:
    """True if *text* is git's transient object-DB read signature (case-insensitive).

    Exposed so a caller that must distinguish this fault in the error it raises — an
    unreadable object is not a data conflict, and sends the operator to a different tool —
    classifies it against the SAME marker set the retry uses, never a second private copy."""
    return any(marker in text.lower() for marker in _TRANSIENT_OBJECT_MARKERS)


def _is_transient_object_write_error(text: str) -> bool:
    """True if *text* is git's transient object-DB WRITE signature (case-insensitive) — the
    loose-object temp-create fault of :data:`_TRANSIENT_WRITE_MARKERS`.

    Module-private, unlike the read-side predicate: that one is public because a production
    caller (the s3 doctor) folds a hint from it into the error it raises, and no caller
    classifies the write fault that way today. ``event_append`` re-exports this under its own
    historical name so there is still exactly ONE marker definition."""
    return any(marker in text.lower() for marker in _TRANSIENT_WRITE_MARKERS)


def _is_transient_git_fault(text: str) -> bool:
    """True if *text* is any transient runner-FS git signature (case-insensitive): the
    READ-side HEAD-parse and ``bad object`` faults, or the WRITE-side loose-object
    temp-create fault."""
    low = text.lower()
    return (
        any(marker in low for marker in _TRANSIENT_HEAD_MARKERS)
        or is_transient_object_read_error(low)
        or _is_transient_object_write_error(low)
    )


def _with_transient_fault_retry(
    run_once: Callable[[], subprocess.CompletedProcess],
    *,
    attempts: int = _TRANSIENT_FAULT_ATTEMPTS,
) -> subprocess.CompletedProcess:
    """Run *run_once* (an idempotent git invocation), retrying ONLY the transient runner-FS
    signatures of :func:`_is_transient_git_fault` with a bounded backoff. On success or a
    NON-transient failure the result is returned immediately (behavior unchanged — a real
    error still surfaces at once), and a transient one that outlives *attempts* returns its
    failing result, so a persistent fault still fails loudly. The retried invocation MUST be
    idempotent: the READ faults abort before anything is written, and re-running a
    content-addressed object write re-writes the same objects. This is the INNER composition
    loop — :func:`run_git_write` wraps it in :func:`_with_index_lock_retry` (index.lock is the
    OUTER retry, the runner-FS transient the inner)."""
    result = run_once()
    for attempt in range(1, attempts):
        if result.returncode == 0:
            return result
        if not _is_transient_git_fault(result.stderr or result.stdout or ""):
            return result
        _backoff_sleep(_TRANSIENT_FAULT_BACKOFF_S * attempt)
        result = run_once()
    return result


# Watchdog bound for the index-mutating store git ops routed through run_git_write
# (txn/compact/delete add/commit/rm). WATCHDOG-grade, not a latency budget (9305 research
# §2: "bounding a local git call is defensible watchdog-grade, not latency-grade"):
# deliberately NOT copied from the 30s/300s network values — these are small local
# add/commits on the tickets tree (sub-second normally), so 120s distinguishes "wedged
# fs/lock" from "slow" with ~100x margin while still freeing the store write lock a hung
# mount would otherwise hold forever. event_append keeps its own 30s (_GIT_TIMEOUT there).
_LOCAL_GIT_TIMEOUT = 120


# raw-git-ok: locked store seam internal
def run_git_write(
    tracker: str | os.PathLike[str],
    *args: str,
    check: bool = False,
    timeout: float = _LOCAL_GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """``run_git`` for an index-MUTATING op (``add``/``commit``/``reset``…), self-healing
    git's ``.git/index.lock`` / ref-lock contention AND the transient runner-FS git faults
    (the ``could not parse HEAD`` / ``bad object`` READ faults and the loose-object
    temp-create WRITE fault), serialized behind the per-store advisory git-op lock (bug 9305).
    Runs the op and, ONLY on a git lock-conflict signature, reclaims a provably-stale
    index.lock, backs off (jittered, bounded), and retries (see
    :func:`_with_index_lock_retry`); ONLY on a transient runner-FS signature, backs
    off and retries the identical (idempotent) op (see :func:`_with_transient_fault_retry`).
    The two compose — the lock conflict is the OUTER retry, the runner-FS transient the
    INNER — so each self-heals without interfering. A success or any OTHER failure returns
    at once, so a genuine error is unchanged; a lock conflict that outlives the bounded
    budget returns ONE actionable error naming the lock file. Each attempt is bounded by
    the :data:`_LOCAL_GIT_TIMEOUT` watchdog, folded into a synthetic rc-124 result (the
    ``event_append._run_git`` shape) so a hung filesystem fails the op cleanly instead of
    hanging the caller. ``check=True`` raises :class:`subprocess.CalledProcessError` on the
    final non-zero exit (default ``False`` so callers that inspect ``returncode`` / raise
    their own error get the result verbatim).

    Safe to route ANY tracker git op through: a read op never produces a lock or an
    object-write signature, so it simply never trips either retry.

    ``timeout`` overrides the :data:`_LOCAL_GIT_TIMEOUT` watchdog for callers that declare
    their own module-level bound (e.g. the s3 doctor's) — the bound travels with the caller
    rather than being silently replaced when it adopts this seam."""

    def _bounded_once() -> subprocess.CompletedProcess:
        try:
            return run_git(tracker, *args, check=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                ["git", "-C", str(tracker), *args],
                124,
                "",
                f"git timed out after {timeout}s",
            )

    result = _with_index_lock_retry(
        str(tracker),
        lambda: _with_transient_fault_retry(_bounded_once),
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            ["git", *args] if tracker is None else ["git", "-C", str(tracker), *args],
            result.stdout,
            result.stderr,
        )
    return result


# ── stranded-index classification (bug 2fa6) ────────────────────────────────────────────
# The tickets branch has a KNOWN SHAPE: per-ticket event directories (several id styles —
# `b636-f31a-d590-4642`, `jira-reb-1001`) plus a small set of store dotfiles. Rather than
# pattern-match those styles, ASK THE BRANCH: a path whose top-level component is absent from
# HEAD's tree does not belong to the store at all.
#
# This matters because a stranded unmerged index blocks EVERY store write. Before this, the
# recovery had only two buckets — reconciler-regenerable (discard) and everything-else
# (refuse as ticket data) — so paths that were NEITHER wedged the store until a human
# intervened with raw git. That happened when a `git stash pop` in the tickets worktree
# applied a stash created in a SOURCE worktree (the stash stack is shared across worktrees),
# dropping `src/…` and `.rebar/…` into the store.


def path_is_foreign_to_branch(tracker: str, path: str) -> bool:
    """True when ``path``'s top-level component is not tracked on the checked-out branch.

    Such a path CANNOT be ticket data, so a stranded conflict on it is safe to discard.
    Conservative by construction: anything the branch does track is treated as store data and
    left for a human. Fails CLOSED (returns False) if git cannot answer.
    """
    top = path.split("/", 1)[0]
    if not top:
        return False
    probe = run_git(tracker, "cat-file", "-e", f"HEAD:{top}", check=False)
    return probe.returncode != 0


# raw-git-ok: locked store seam internal — this is the store's OWN stranded-index recovery,
# invoked from event_append under the write lock. It is the sanctioned alternative to an
# operator running `git rm` / `git checkout` in the tracker by hand, which is what this bug
# (2fa6) exists to design out.
def discard_unmerged_paths(tracker: str, regenerable: list[str], foreign: list[str]) -> None:
    """Clear stranded unmerged entries: drop every stage from the index, then restore the
    REGENERABLE ones from HEAD (the reconciler rebuilds their content) and delete the FOREIGN
    ones outright (HEAD has no copy to restore — they never belonged to this branch)."""
    both = [*regenerable, *foreign]
    if not both:
        return
    run_git(  # raw-git-ok: locked store seam internal
        tracker, "rm", "-q", "--cached", "--", *both, check=False
    )
    if regenerable:
        run_git(  # raw-git-ok: locked store seam internal
            tracker, "checkout", "HEAD", "--", *regenerable, check=False
        )
    for rel in foreign:
        try:
            os.remove(os.path.join(tracker, rel))
        except OSError:
            pass
