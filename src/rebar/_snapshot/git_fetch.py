"""Bounded git subprocess and fetch-lock plumbing for snapshot materialization.
This lowest layer owns child-process bounds, thread/process fetch coalescing, and the
fail-closed error vocabulary. :mod:`rebar._snapshot.repo_snapshot` imports it one-way.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rebar._store.git_outcome import is_ref_cas_mismatch
from rebar._store.gitutil import fetch_coordination_lock, run_git

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
    """Fail-closed fetch error with an actionable credential remedy.

    Raw ``stderr`` lets callers distinguish a missing scoped ref from transport or
    authentication failure without parsing the composed message."""

    def __init__(self, *args: object, stderr: str = "") -> None:
        super().__init__(*args)
        self.stderr = stderr


class SnapshotRefError(SnapshotError):
    """A client ``ref`` did not resolve to a commit (after fetching)."""


@contextmanager
def interprocess_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive process lock via ``flock`` or atomic-``mkdir`` fallback.

    The fallback may permit redundant work but cannot change correctness."""
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


# Git subprocess bounds. _GIT_TIMEOUT covers quick local plumbing such as rev-parse and
# cat-file, preventing a hung child from wedging the server. Whole-tree network fetches
# use their separate, larger fetch_timeout() ceiling.
_GIT_TIMEOUT = 300

# Generous wall-clock backstop for network fetches and repo_snapshot's --refetch. The
# low-speed guard handles dead transfers; this live-configured ceiling catches pre-transport
# hangs without rejecting a healthy large clone.
_FETCH_TIMEOUT_SECONDS = 3600


def fetch_timeout() -> int:
    """The materialization-fetch wall-clock backstop, resolved live (env over default).

    See :data:`_FETCH_TIMEOUT_SECONDS` for why this is a generous backstop, not the primary
    stall guard. Read per call so an operator/test override applies without a reimport."""
    from rebar import config

    return config.resolve_fetch_timeout(_FETCH_TIMEOUT_SECONDS)


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
    """Return whether ``ref`` is a full commit SHA already present offline.

    Immutable local SHAs need no freshness fetch. Abbreviations and moving refs do not
    match; absent objects in partial clones cannot lazy-fetch, so callers fall through to
    the explicit fail-closed targeted fetch."""
    if not _FULL_SHA_RE.match(ref):
        return False
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    proc = git_run(repo_root, "cat-file", "-e", "--end-of-options", f"{ref}^{{commit}}", env=env)
    return proc.returncode == 0


def is_auth_failure(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _AUTH_STDERR_MARKERS)


# These stderr fragments mean a scoped ref is absent, a resolution miss that may fall
# through to a targeted SHA; authentication, stall, and timeout remain transport failures.
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
    """Return a single-ref target for an attested resolution fetch.

    ``<remote>/<name>`` becomes a forced remote-tracking refspec so later resolution sees
    it; branches, tags, and SHAs remain targeted wants. This avoids the clone's all-heads
    refspec. :func:`fetch_origin` places the result after ``--end-of-options`` so untrusted
    input is only a refspec and fails closed if invalid."""
    prefix = f"{remote}/"
    if ref.startswith(prefix) and len(ref) > len(prefix):
        name = ref[len(prefix) :]
        return f"+{name}:refs/remotes/{remote}/{name}"
    return ref


# Throughput-based stall detection complements the wall clock. Git's low-speed limit and
# window abort sustained dead air while allowing a slow transfer that keeps exceeding the
# floor. Healthy large cold clones remain above the conservative defaults.
_STALL_FLOOR_BYTES_PER_SEC = 1000
_STALL_WINDOW_SECONDS = 10
# Retry transient stalls only, with a bounded attempt count so persistent failure closes.
_STALL_ATTEMPTS = 3

# A CAS mismatch comes from a non-rebar git peer; briefly let it settle before the bounded
# retry, while the common-directory lock already excludes rebar peers.
_CAS_RETRY_BACKOFF_S = 0.1

# curl's wording when the low-speed check fires, as git relays it on stderr:
#   "fatal: ... Operation too slow. Less than 1000 bytes/sec transferred the last 5 seconds"
_STALL_STDERR_MARKER = "operation too slow"


def stall_abort_args() -> list[str]:
    """Return live-configured ``git -c`` low-speed options.

    Callers must place them before the subcommand, where git accepts top-level ``-c``."""
    from rebar import config

    floor, window = config.resolve_stall_abort_limits(
        _STALL_FLOOR_BYTES_PER_SEC, _STALL_WINDOW_SECONDS
    )
    return ["-c", f"http.lowSpeedLimit={floor}", "-c", f"http.lowSpeedTime={window}"]


def is_stall_abort(stderr: str) -> bool:
    """Detect curl's low-speed-abort wording in git stderr.

    Git's generic transport exit status cannot distinguish silence from rejection."""
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
    """Run a coalesced, optionally targeted ``git fetch``.

    Thread, snapshot-store, and canonical-common-directory locks serialize every rebar
    fetch sharing remote-tracking refs. ``blobless=True`` fetches commits and trees for
    pure resolution; materializers pass ``False`` to fetch all blobs in one RPC without
    latching ordinary clones into promisor mode. The caller supplies the store lock path,
    preserving dependency direction. Authentication, transport, and timeout failures raise
    actionable :class:`SnapshotFetchError`; stalls and non-rebar CAS races retry only within
    the configured bound."""
    # Disable any interactive credential prompt so a missing credential fails fast with a
    # descriptive error instead of hanging the long-lived server on a TTY prompt.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    filter_arg = "--filter=blob:none" if blobless else "--no-filter"
    args = ["fetch", "--quiet", filter_arg, remote]
    if ref is not None:
        # SECURITY: terminate before this untrusted positional so git cannot reinterpret a
        # ref such as --upload-pack=<cmd> as an executable option; invalid refspecs fail closed.
        args += ["--end-of-options", ref]
    # The -c pairs must precede the subcommand; see stall_abort_args().
    argv = ["git", "-C", repo_root, *stall_abort_args(), *args]
    from rebar import config

    attempts = config.resolve_stall_attempts(_STALL_ATTEMPTS)
    timeout_s = fetch_timeout()
    for attempt in range(1, attempts + 1):
        # Reacquire per attempt so peers can fetch between retries and warm the remote.
        # The common-directory lock covers sync and other worktrees; the store lock covers
        # snapshot fetches within this cache.
        with (
            fetch_lock_for(repo_root),
            fetch_coordination_lock(repo_root),
            interprocess_lock(lock_path),
        ):
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                # Backstop pre-transport hangs invisible to the low-speed check, such as a
                # wedged credential helper; fail closed rather than block the server.
                raise SnapshotFetchError(
                    f"git fetch from '{remote}' timed out after {timeout_s}s (attested "
                    "mode fails closed) — the remote may be unreachable or the transfer "
                    "too large."
                ) from exc
        if proc.returncode == 0:
            return
        stderr = (proc.stderr or "").strip()
        # Retry only a stall or non-rebar CAS race; credentials, missing refs, and unreachable
        # hosts have stable diagnoses. Brief backoff lets the external ref writer settle.
        cas = is_ref_cas_mismatch(stderr)
        if (not is_stall_abort(stderr) and not cas) or attempt == attempts:
            break
        if cas:
            time.sleep(_CAS_RETRY_BACKOFF_S)
    if proc.returncode != 0:
        _raise_fetch_failure(remote, stderr, attempts)


def _raise_fetch_failure(remote: str, stderr: str, attempts: int) -> None:
    """Translate a non-zero ``git fetch`` into the one actionable fail-closed error its
    signature calls for (stall / ref CAS exhaustion / auth / generic). Split out of
    :func:`fetch_origin` so the retry loop stays flat."""
    if is_stall_abort(stderr):
        raise SnapshotFetchError(
            f"git fetch from '{remote}' stalled — the connection was established but "
            f"transferred almost nothing, and {attempts} attempt(s) all aborted on the "
            "low-speed check (attested mode fails closed). The remote may be wedged or "
            f"the network path broken. git said: {stderr or '<no detail>'}",
            stderr=stderr,
        )
    if is_ref_cas_mismatch(stderr):
        raise SnapshotFetchError(
            f"git fetch from '{remote}' lost git's ref compare-and-swap to a concurrent "
            f"ref-updating fetch on the same Git common directory, and {attempts} "
            "coordinated attempt(s) did not converge (attested mode fails closed) — a "
            "non-rebar git peer may be updating the same remote-tracking ref. git said: "
            f"{stderr or '<no detail>'}",
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
        f"git fetch from '{remote}' failed (attested mode fails closed): {stderr or '<no detail>'}",
        stderr=stderr,
    )
