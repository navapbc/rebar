"""Git subprocess + fetch-locking plumbing for the snapshot materializer.

Extracted whole from :mod:`rebar._snapshot.repo_snapshot` along the call-graph seam it
already formed there (that module sits against the 800-LOC cap). Everything here is the
*lowest* layer of the snapshot stack — the bounded git child process, the two locks that
coalesce concurrent fetches, and the fail-closed error vocabulary they raise — so the
dependency runs one way: ``repo_snapshot`` imports this module, never the reverse.

"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rebar._store.gitutil import run_git

try:  # POSIX advisory locking; absent on some platforms (e.g. plain Windows)
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

# stderr fragments that mean "the remote rejected us for AUTH reasons" — surfaced as a
# credential error with an actionable remedy rather than a raw git dump.
_AUTH_STDERR_MARKERS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "permission denied (publickey)",
    "permission denied, please try again",
    "fatal: could not read from remote repository",
    "remote: invalid username or password",
    "remote: support for password authentication",
    "terminal prompts disabled",
    "403 forbidden",
    "401 unauthorized",
)


class SnapshotError(RuntimeError):
    """A snapshot could not be materialized (fail-closed in attested mode)."""


class SnapshotFetchError(SnapshotError):
    """``git fetch`` failed — typically missing/invalid credentials for a private repo.

    Carries an actionable remedy (configure a credential helper / deploy key / token);
    see the MCP-server setup docs. Attested mode treats this as fail-closed.

    ``stderr`` preserves git's raw failure text so a caller can classify the failure
    (e.g. :func:`is_missing_ref` — a scoped fetch of a ref the remote simply lacks is a
    resolution miss, not a transport failure) without re-parsing the composed message."""

    def __init__(self, *args: object, stderr: str = "") -> None:
        super().__init__(*args)
        self.stderr = stderr


class SnapshotRefError(SnapshotError):
    """A client ``ref`` did not resolve to a commit (after fetching)."""


@contextmanager
def interprocess_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock for the duration of the block.

    Uses ``fcntl.flock(LOCK_EX)`` where available; otherwise falls back to an atomic
    ``mkdir`` spin-lock (``mkdir`` is atomic on a local FS). A lost race here is only
    ever *wasteful* (a redundant fetch), never *wrong*, so the fallback's coarseness is
    acceptable."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return
    # Fallback: atomic mkdir spin-lock.
    mkdir_lock = lock_path.with_suffix(lock_path.suffix + ".d")
    import time

    while True:
        try:
            os.mkdir(str(mkdir_lock))
            break
        except FileExistsError:
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            os.rmdir(str(mkdir_lock))
        except OSError:  # pragma: no cover - best effort
            pass


# In-process fetch coalescing: at most one fetch per repo at a time within this process
# (the cross-process flock handles the multi-process case).
_fetch_locks: dict[str, threading.Lock] = {}
_fetch_locks_guard = threading.Lock()


def fetch_lock_for(repo_root: str) -> threading.Lock:
    key = os.path.realpath(repo_root)
    with _fetch_locks_guard:
        lk = _fetch_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _fetch_locks[key] = lk
        return lk


# --------------------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------------------
# Bound every git call on this path so a stuck remote (or hung credential helper) can never
# wedge the long-lived MCP server. Deliberately MUCH larger than the 30s in _store/push.py
# and _store/sync.py: those bound a tickets push/fetch (a few tiny event files), whereas a
# materialization fetch here is UNFILTERED and transfers every blob of a whole tree in one
# RPC — legitimately minutes on a cold clone. A timeout surfaces as a failed
# CompletedProcess (returncode 124), never a hang.
_GIT_TIMEOUT = 300


# raw-git-ok: generic command runner, argv supplied by caller
def git_run(
    repo_root: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return run_git(repo_root, *args, check=False, env=env, timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", "-C", repo_root, *args],
            124,
            "",
            f"git timed out after {_GIT_TIMEOUT}s",
        )


def has_remote(repo_root: str, remote: str = "origin") -> bool:
    proc = git_run(repo_root, "remote")
    remotes = {ln.strip() for ln in proc.stdout.splitlines()}
    return remote in remotes


def rev_parse(repo_root: str, ref: str) -> str | None:
    """Resolve ``ref`` to a full commit SHA, or ``None`` if it does not resolve."""
    proc = git_run(
        repo_root,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        f"{ref}^{{commit}}",
    )
    sha = proc.stdout.strip()
    return sha or None


# A full object name: 40 hex (sha1) or 64 hex (sha256). Abbreviations are deliberately
# excluded — a short prefix is ambiguous, so it is NOT eligible for the fetch short-circuit.
_FULL_SHA_RE = re.compile(r"\A(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")


def is_present_full_sha(repo_root: str, ref: str) -> bool:
    """True when ``ref`` is a FULL object name already present locally as a commit.

    A full SHA is immutable, so — unlike a branch or tag — it owes no remote-freshness
    fetch: when the commit object is already in the repo, an attested resolution can skip
    the opening fetch entirely (bug sawdusty-snotty-fossa) rather than paying even a scoped
    single-want round-trip. Abbreviated names are rejected (ambiguous) and moving refs never
    match, so freshness for branches/tags and targeted recovery for an ABSENT full SHA are
    both preserved by the callers that gate on this predicate. ``GIT_NO_LAZY_FETCH`` keeps
    the presence probe OFFLINE — on a promisor/partial clone an absent object must NOT be
    lazily fetched here (that is the very round-trip we skip); it returns non-zero instead,
    so the caller falls through to the explicit, fail-closed targeted-want fetch."""
    if not _FULL_SHA_RE.match(ref):
        return False
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    proc = git_run(repo_root, "cat-file", "-e", "--end-of-options", f"{ref}^{{commit}}", env=env)
    return proc.returncode == 0


def is_auth_failure(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _AUTH_STDERR_MARKERS)


# stderr fragments that mean "the remote simply does not have the ref we asked for" — a
# scoped fetch of a nonexistent branch. This is a RESOLUTION miss (the caller should fall
# through to a targeted SHA want / fail-closed ref error), NOT a transport failure that must
# fail closed. Distinct from an auth/stall/timeout failure.
_MISSING_REF_STDERR_MARKERS = (
    "couldn't find remote ref",
    "no such ref",
    "not our ref",
)


def is_missing_ref(stderr: str) -> bool:
    """True when git's stderr shows the requested ref does not exist on the remote."""
    low = stderr.lower()
    return any(marker in low for marker in _MISSING_REF_STDERR_MARKERS)


def scoped_fetch_target(ref: str, remote: str) -> str:
    """The single-ref fetch target that scopes an attested resolution's opening fetch.

    A remote-qualified ``ref`` (``<remote>/<name>``) becomes a forced tracking refspec
    ``+<name>:refs/remotes/<remote>/<name>`` — git transfers ONLY that branch and updates
    ``refs/remotes/<remote>/<name>`` so ``rev_parse(ref)`` resolves it (a bare
    ``git fetch <remote> <name>`` only writes ``FETCH_HEAD``). Any other form (a plain
    branch/tag, or a bare SHA served under ``allowReachableSHA1InWant``) is passed through
    as a targeted want. Either way the fetch names a target, so it never falls back to the
    clone's configured all-heads refspec (bug lemuroid-compliant-hoopoe). Handed to
    :func:`fetch_origin` as ``ref`` — placed after ``--end-of-options``, so a hostile value
    is treated strictly as a refspec (fail-closed on an invalid one), never as an option."""
    prefix = f"{remote}/"
    if ref.startswith(prefix) and len(ref) > len(prefix):
        name = ref[len(prefix) :]
        return f"+{name}:refs/remotes/{remote}/{name}"
    return ref


# --------------------------------------------------------------------------------------
# Stall detection: a throughput-keyed abort, NOT a second wall clock
# --------------------------------------------------------------------------------------
# _GIT_TIMEOUT above bounds ELAPSED TIME, which is the wrong axis for a stalled transfer.
# A remote that completes the TCP handshake and the HTTP response headers and then sends
# ZERO bytes looks, to subprocess.run(), exactly like a legitimately slow cold clone: both
# are "still running". So the only thing that ends a dead-air fetch is the 300s ceiling —
# five minutes of a wedged child before the caller learns anything.
#
# git's curl transport already carries the right instrument: http.lowSpeedLimit /
# http.lowSpeedTime abort the transfer when throughput stays BELOW a floor for a window.
# That distinction matters: a slow-but-alive transfer keeps clearing the floor and is left
# alone (a dribbling remote at 2000 B/s is never aborted under a 1000 B/s floor), while a
# genuinely dead connection trips the window in seconds. Real cold clones of the ~80 MB
# mirror (88-101s wall) sustain far more than 1000 B/s throughout, so the defaults below
# cannot fire on a healthy-but-large fetch.
_STALL_FLOOR_BYTES_PER_SEC = 1000
_STALL_WINDOW_SECONDS = 10
# A stall is the one failure worth retrying in-process: it is a transport-level flap with
# no diagnosis attached, and a fresh connection usually succeeds. Deliberately bounded, so
# a persistently dead remote still fails closed in bounded time.
_STALL_ATTEMPTS = 3

# curl's wording when the low-speed check fires, as git relays it on stderr:
#   "fatal: ... Operation too slow. Less than 1000 bytes/sec transferred the last 5 seconds"
_STALL_STDERR_MARKER = "operation too slow"


def stall_abort_args() -> list[str]:
    """The ``git -c`` pairs that arm the throughput-keyed abort on the child's curl handle.

    Must be spliced in BEFORE the subcommand (``git -C <root> -c ... fetch ...``); git only
    accepts ``-c`` as a top-level option, so ``git fetch -c ...`` is a usage error. The
    floor/window overrides are resolved through the owned config seam
    (:func:`rebar.config.resolve_stall_abort_limits`) — live per call — rather than read
    from ``os.environ`` here."""
    from rebar import config

    floor, window = config.resolve_stall_abort_limits(
        _STALL_FLOOR_BYTES_PER_SEC, _STALL_WINDOW_SECONDS
    )
    return ["-c", f"http.lowSpeedLimit={floor}", "-c", f"http.lowSpeedTime={window}"]


def is_stall_abort(stderr: str) -> bool:
    """True when git's stderr shows the low-speed abort fired (i.e. the remote went quiet).

    Keyed on curl's own wording rather than the exit code, because git reports every
    transport failure with the same generic status — the message is the only signal that
    distinguishes "went silent" from "rejected us"."""
    return _STALL_STDERR_MARKER in stderr.lower()


# raw-git-ok: read-oriented git helper, variable subcommand
def fetch_origin(
    repo_root: str,
    *,
    lock_path: Path,
    ref: str | None = None,
    remote: str = "origin",
    blobless: bool = True,
) -> None:
    """Coalesced ``git fetch <remote>`` (optionally a targeted ref/SHA).

    Serialized in-process (one fetch per repo at a time) and cross-process (an exclusive
    flock), since fetch is the only lock-taking step. ``blobless`` selects the filtering
    policy (see the module docstring): ``True`` (the default, preserving today's behaviour
    for pure-resolution callers) fetches ``--filter=blob:none`` — commits and trees only. A
    caller whose ref WILL be materialized must pass ``blobless=False`` → ``--no-filter``, so
    blobs arrive with the commit in a single RPC and an ordinary clone is never latched into
    a promisor remote.

    ``lock_path`` is the cross-process fetch lock file (the snapshot store owns its
    location, so the caller supplies it and this layer stays free of store layout).

    Raises :class:`SnapshotFetchError` (fail-closed) on failure, with an actionable
    credential remedy when the remote rejected us for auth reasons; a timeout is likewise
    surfaced as a descriptive error rather than a hang."""
    # Disable any interactive credential prompt so a missing credential fails fast with a
    # descriptive error instead of hanging the long-lived server on a TTY prompt.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    filter_arg = "--filter=blob:none" if blobless else "--no-filter"
    args = ["fetch", "--quiet", filter_arg, remote]
    if ref is not None:
        # SECURITY: a client ref reaches this positional, so it MUST be terminated with
        # --end-of-options. Without it, git reorders interspersed options and a ref like
        # "--upload-pack=<cmd>" would be parsed as an option and EXECUTE (RCE). With it,
        # git treats the value strictly as a refspec (invalid refspec -> fail closed).
        args += ["--end-of-options", ref]
    # The -c pairs must precede the subcommand; see stall_abort_args().
    argv = ["git", "-C", repo_root, *stall_abort_args(), *args]
    from rebar import config

    attempts = config.resolve_stall_attempts(_STALL_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        # Re-acquire both locks PER ATTEMPT rather than holding them across the whole retry
        # budget: a peer that is waiting to fetch the same repo gets a turn between our
        # attempts, and — better still — may land the fetch we were failing at, so our next
        # attempt is served by a warmer remote instead of queueing behind a dead one.
        with fetch_lock_for(repo_root), interprocess_lock(lock_path):
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=_GIT_TIMEOUT,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                # Backstop for a hang the low-speed check cannot see (e.g. a wedged
                # credential helper, which moves no bytes because it never reaches the
                # transport at all). Fail closed with a description (this function's
                # contract) — never leave the caller, or the long-lived server, blocked.
                raise SnapshotFetchError(
                    f"git fetch from '{remote}' timed out after {_GIT_TIMEOUT}s (attested "
                    "mode fails closed) — the remote may be unreachable or the transfer "
                    "too large."
                ) from exc
        if proc.returncode == 0:
            return
        stderr = (proc.stderr or "").strip()
        # ONLY a stall is retried. Every other failure carries a diagnosis that a second
        # identical invocation cannot change — bad credentials stay bad, a missing ref
        # stays missing, an unreachable host stays unreachable — so retrying them would
        # just multiply the latency of a certain failure. Those fall straight through to
        # the error branches below on the first attempt.
        if not is_stall_abort(stderr) or attempt == attempts:
            break
    if proc.returncode != 0:
        if is_stall_abort(stderr):
            raise SnapshotFetchError(
                f"git fetch from '{remote}' stalled — the connection was established but "
                f"transferred almost nothing, and {attempts} attempt(s) all aborted on the "
                "low-speed check (attested mode fails closed). The remote may be wedged or "
                f"the network path broken. git said: {stderr or '<no detail>'}",
                stderr=stderr,
            )
        if is_auth_failure(stderr):
            raise SnapshotFetchError(
                f"git fetch from '{remote}' was rejected for authentication — the rebar "
                "MCP server needs read credentials to fetch the verified ref from a "
                "private repository. Configure a git credential helper, a deploy key, "
                "or a token for the server's clone (see the MCP-server setup docs), "
                f"then retry. git said: {stderr or '<no detail>'}",
                stderr=stderr,
            )
        raise SnapshotFetchError(
            f"git fetch from '{remote}' failed (attested mode fails closed): "
            f"{stderr or '<no detail>'}",
            stderr=stderr,
        )
