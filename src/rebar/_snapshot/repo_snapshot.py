"""Faithful, lock-free git-ref snapshot materialization (epic ``raze-vet-ditch`` S1).

The gates need a real on-disk tree at a pinned SHA, but the server's worktree is
mutable and shared. ``git archive`` is *lossy* as an attestation basis — it drops
``.gitattributes`` ``export-ignore`` paths, rewrites ``export-subst`` placeholders,
omits submodule contents, and emits Git-LFS *pointer* text rather than smudged
content. For a verdict that claims "this is the code at SHA X" we want the committed
tree EXACTLY, so this builds it with git plumbing instead:

    (coalesced) ``git fetch origin`` → resolve ``ref`` to an immutable SHA →
    ``git read-tree <sha>`` into a *temp* index (``GIT_INDEX_FILE``) →
    ``git checkout-index --all --prefix=<tmp>/`` → atomic ``rename`` into the cache.

Why ``read-tree`` + ``checkout-index`` (not ``git archive``):
  * **Faithful.** ``checkout-index`` materializes the committed blob for every tree
    entry, so ``export-ignore`` files ARE present and ``export-subst`` is NOT applied
    (the committed bytes are preserved verbatim) — the snapshot byte-matches the tree.
  * **Lock-free across SHAs.** The index is a throwaway file pointed at by
    ``GIT_INDEX_FILE``; neither ``read-tree`` nor ``checkout-index`` touches the repo's
    own ``index.lock``/``config.lock`` or working tree, so two materializations of
    different SHAs never contend. Only the (coalesced) fetch takes repo locks.

Faithfulness limits, by construction (detected + surfaced, never silently wrong):
  * **Git-LFS** — a tracked LFS path's committed blob *is* its ~130-byte pointer text;
    no smudge filter runs here. We DETECT pointers (magic header) and record them on
    the handle so a gate is never handed pointer text as if it were real content.
  * **Submodules** — a gitlink (mode ``160000``) has no blob; ``checkout-index`` does
    not populate it. Submodule contents are intentionally OMITTED; the gitlink paths
    are recorded on the handle.

Portability + safety:
  * No external ``tar`` is ever invoked. The temp root comes from ``REBAR_GATE_TMPDIR``
    (overridable) else :func:`tempfile.gettempdir` — never a hardcoded ``/tmp`` — and is
    created OUTSIDE the repo/``.git`` with ``0700`` perms. Cross-process coordination
    uses ``fcntl.flock`` with an atomic-``mkdir`` fallback for platforms without it.
  * Population is atomic: the tree is built under ``<root>/tmp/<uuid>/`` and ``rename``-d
    into ``<root>/<sha>`` (fsync'd), so a reader never observes a partial tree and a
    crash leaves only a ``tmp/`` entry that :func:`sweep_tmp` clears on startup.

Fetch prerequisites (operators): fetching an *arbitrary SHA* requires the remote's
``uploadpack.allowReachableSHA1InWant`` (else fetch a containing ref then resolve);
this prefers ``--filter=blob:none`` over a deep shallow refetch. A private-repo fetch
with missing credentials raises a descriptive, actionable :class:`SnapshotFetchError`
(attested mode fails closed; local mode never fetches).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from rebar._store.gitutil import run_git

try:  # POSIX advisory locking; absent on some platforms (e.g. plain Windows)
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

# A Git-LFS pointer file starts with this version line (LFS spec v1). The committed
# blob for an LFS-tracked path is this pointer, not the real content.
_LFS_POINTER_MAGIC = b"version https://git-lfs.github.com/spec/v1"
# Pointer files are tiny by spec (a few lines); cap the sniff so we never read a large
# blob just to classify it.
_LFS_SNIFF_BYTES = 1024

# Valid source modes. ``attested`` materializes a pinned snapshot (signable);
# ``local`` reads the in-place checkout (dirty allowed, never signed).
SOURCE_ATTESTED = "attested"
SOURCE_LOCAL = "local"
_SOURCE_MODES = (SOURCE_ATTESTED, SOURCE_LOCAL)

DEFAULT_REF = "origin/main"

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
    see the MCP-server setup docs. Attested mode treats this as fail-closed."""


class SnapshotRefError(SnapshotError):
    """A client ``ref`` did not resolve to a commit (after fetching)."""


# --------------------------------------------------------------------------------------
# Store layout
# --------------------------------------------------------------------------------------
# The content-addressed snapshot store lives OUTSIDE the repo so a gate's read-only/no-git
# tools never reach it and a snapshot is never mistaken for working-tree state. Layout:
#   <root>/<sha>/      a materialized, immutable snapshot (entry; content-addressed)
#   <root>/tmp/<uuid>/ an in-progress build (renamed into <sha> on success)
# Sibling modules add <root>/locks, <root>/trash, <root>/gc for the cache + janitor.

_STORE_DIRNAME = "rebar-gate-snapshots"


def store_root() -> Path:
    """The base directory of the content-addressed snapshot store.

    ``REBAR_GATE_TMPDIR`` overrides the base (operators point it at a roomy local FS);
    otherwise :func:`tempfile.gettempdir` is used — never a hardcoded ``/tmp``. Created
    ``0700`` if absent."""
    base = os.environ.get("REBAR_GATE_TMPDIR") or tempfile.gettempdir()
    root = Path(base) / _STORE_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:  # best-effort on platforms without full POSIX perms
        pass
    return root


def _tmp_root(root: Path) -> Path:
    d = root / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def entry_path(sha: str, root: Path | None = None) -> Path:
    """The content-addressed path for a materialized snapshot at ``sha``."""
    return (root or store_root()) / sha


def _caveats_path(sha: str, root: Path) -> Path:
    """Sidecar (OUTSIDE the entry, so it never pollutes the materialized tree) recording
    the immutable faithfulness caveats for ``sha`` (LFS pointers + submodule gitlinks)."""
    return root / f"{sha}.caveats.json"


def _store_caveats(sha: str, root: Path, lfs: tuple[str, ...], subs: tuple[str, ...]) -> None:
    path = _caveats_path(sha, root)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps({"lfs_pointers": list(lfs), "submodules": list(subs)}))
        os.replace(tmp, path)
    except OSError:  # best-effort cache; absence just forces a recompute
        pass


def _load_caveats(sha: str, root: Path) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    try:
        data = json.loads(_caveats_path(sha, root).read_text())
    except (OSError, ValueError):
        return None
    return tuple(data.get("lfs_pointers", [])), tuple(data.get("submodules", []))


# --------------------------------------------------------------------------------------
# Handle
# --------------------------------------------------------------------------------------
@dataclass
class SnapshotHandle:
    """A read root for a code-reading gate.

    ``path`` is the directory the gate reads from: a materialized snapshot dir in
    ``attested`` mode, or the in-place checkout (``repo_root``) in ``local`` mode.
    ``sha`` is the resolved immutable commit (``None`` in local mode — the checkout may
    be dirty). ``lfs_pointers`` / ``submodules`` record the faithfulness caveats so a
    gate or signer is never silently handed pointer text / an empty submodule dir.
    ``tickets_path`` is the read root for the agent's rebar TICKET tools — a separately
    materialized, pinned copy of the ticket store (the ``tickets`` branch lives on an
    orphan ref, so it is absent from the code snapshot ``path``). ``None`` = read the
    in-place checkout's store (local mode); attested sets it via :func:`materialize_tickets`.
    """

    path: Path
    sha: str | None
    source: str
    lfs_pointers: tuple[str, ...] = ()
    submodules: tuple[str, ...] = ()
    tickets_path: str | None = None
    _cleanup: Callable[[], None] | None = field(default=None, repr=False)

    @property
    def signable(self) -> bool:
        """Only an attested snapshot pinned to an immutable SHA may back a signature."""
        return self.source == SOURCE_ATTESTED and self.sha is not None

    def __enter__(self) -> SnapshotHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        # Returns None (falsy) — never swallows the in-flight exception. (mypy rejects a
        # `-> bool` that only ever returns False as a context-manager footgun.)
        if self._cleanup is not None:
            self._cleanup()


# --------------------------------------------------------------------------------------
# Cross-process locking (flock, with an atomic-mkdir fallback)
# --------------------------------------------------------------------------------------
@contextmanager
def _interprocess_lock(lock_path: Path) -> Iterator[None]:
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


def _fetch_lock_for(repo_root: str) -> threading.Lock:
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
def _git(
    repo_root: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return run_git(repo_root, *args, check=False, env=env)


def _has_remote(repo_root: str, remote: str = "origin") -> bool:
    proc = _git(repo_root, "remote")
    remotes = {ln.strip() for ln in proc.stdout.splitlines()}
    return remote in remotes


def _rev_parse(repo_root: str, ref: str) -> str | None:
    """Resolve ``ref`` to a full commit SHA, or ``None`` if it does not resolve."""
    proc = _git(
        repo_root,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        f"{ref}^{{commit}}",
    )
    sha = proc.stdout.strip()
    return sha or None


def _is_auth_failure(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _AUTH_STDERR_MARKERS)


def _fetch_origin(repo_root: str, *, ref: str | None = None, remote: str = "origin") -> None:
    """Coalesced ``git fetch <remote>`` (optionally a targeted ref/SHA).

    Serialized in-process (one fetch per repo at a time) and cross-process (an exclusive
    flock), since fetch is the only lock-taking step. Prefers a blobless partial fetch
    (``--filter=blob:none``) to avoid deep history transfer. Raises
    :class:`SnapshotFetchError` (fail-closed) on failure, with an actionable credential
    remedy when the remote rejected us for auth reasons."""
    # Disable any interactive credential prompt so a missing credential fails fast with a
    # descriptive error instead of hanging the long-lived server on a TTY prompt.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    args = ["fetch", "--quiet", "--filter=blob:none", remote]
    if ref is not None:
        # SECURITY: a client ref reaches this positional, so it MUST be terminated with
        # --end-of-options. Without it, git reorders interspersed options and a ref like
        # "--upload-pack=<cmd>" would be parsed as an option and EXECUTE (RCE). With it,
        # git treats the value strictly as a refspec (invalid refspec -> fail closed).
        args += ["--end-of-options", ref]
    lock_path = store_root() / "locks" / "fetch.lock"
    with _fetch_lock_for(repo_root), _interprocess_lock(lock_path):
        proc = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            env=env,
        )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if _is_auth_failure(stderr):
            raise SnapshotFetchError(
                f"git fetch from '{remote}' was rejected for authentication — the rebar "
                "MCP server needs read credentials to fetch the verified ref from a "
                "private repository. Configure a git credential helper, a deploy key, "
                "or a token for the server's clone (see the MCP-server setup docs), "
                f"then retry. git said: {stderr or '<no detail>'}"
            )
        raise SnapshotFetchError(
            f"git fetch from '{remote}' failed (attested mode fails closed): "
            f"{stderr or '<no detail>'}"
        )


def resolve_ref(
    ref: str, repo_root: str | None = None, *, fetch: bool = True, remote: str = "origin"
) -> str:
    """Resolve a client ``ref`` (branch | tag | SHA) to an immutable commit SHA.

    When an ``origin`` remote exists and ``fetch`` is set, fetches first so the default
    ``origin/main`` (and any moving branch/tag) resolves against the remote, not a stale
    local copy. A SHA that is not present locally triggers a targeted fetch (requires the
    remote's ``uploadpack.allowReachableSHA1InWant``). Raises :class:`SnapshotRefError`
    (fail-closed) if the ref still does not resolve."""
    root = str(repo_root) if repo_root else "."
    has_remote = fetch and _has_remote(root, remote)
    if has_remote:
        _fetch_origin(root, remote=remote)
    sha = _rev_parse(root, ref)
    if sha is None and has_remote:
        # A bare SHA (or a ref only reachable by an explicit want) not present after the
        # general fetch — try a targeted fetch (allowReachableSHA1InWant on the remote).
        try:
            _fetch_origin(root, ref=ref, remote=remote)
        except SnapshotFetchError:
            pass  # fall through to the descriptive ref error below
        sha = _rev_parse(root, ref)
    if sha is None:
        raise SnapshotRefError(
            f"cannot resolve ref {ref!r} to a commit in {root!r}. Name a valid branch, "
            "tag, or full SHA reachable from 'origin'. Fetching an arbitrary SHA also "
            "requires the remote's uploadpack.allowReachableSHA1InWant (or fetch a "
            "containing ref then resolve)."
        )
    return sha


def is_lfs_pointer(path: Path) -> bool:
    """True if ``path``'s leading bytes are a Git-LFS pointer (not real content)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_LFS_SNIFF_BYTES)
    except OSError:
        return False
    return head.startswith(_LFS_POINTER_MAGIC)


def _list_submodules(repo_root: str, sha: str) -> tuple[str, ...]:
    """Gitlink (mode 160000) paths in the tree at ``sha`` — submodules, omitted from the
    materialized tree by construction."""
    proc = _git(repo_root, "ls-tree", "-r", "--full-tree", sha)
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        # "<mode> <type> <oid>\t<path>"
        meta, _, path = line.partition("\t")
        if not path:
            continue
        fields = meta.split()
        if fields and fields[0] == "160000":
            paths.append(path)
    return tuple(sorted(paths))


def _detect_lfs_pointers(tree_dir: Path) -> tuple[str, ...]:
    """Relative paths under ``tree_dir`` whose content is a Git-LFS pointer."""
    found: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(tree_dir):
        for name in filenames:
            p = Path(dirpath) / name
            if is_lfs_pointer(p):
                found.append(os.path.relpath(p, tree_dir))
    return tuple(sorted(found))


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):  # pragma: no cover - non-POSIX
        return
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:  # pragma: no cover - best effort
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - best effort
        pass
    finally:
        os.close(fd)


def _materialize_tree(repo_root: str, sha: str, dest_tmp: Path) -> None:
    """Faithfully write the committed tree at ``sha`` into ``dest_tmp`` via git plumbing.

    Uses a throwaway index (``GIT_INDEX_FILE``) so the repo's own index/working tree is
    never touched — this is what keeps concurrent materializations of different SHAs from
    contending on ``index.lock``."""
    dest_tmp.mkdir(parents=True, exist_ok=True)
    index_file = dest_tmp.parent / (dest_tmp.name + ".index")
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    try:
        read = _git(repo_root, "read-tree", sha, env=env)
        if read.returncode != 0:
            raise SnapshotError(
                f"git read-tree {sha[:12]} failed: {(read.stderr or '').strip() or '<no detail>'}"
            )
        # checkout-index creates leading directories under --prefix automatically.
        prefix = str(dest_tmp) + os.sep
        checkout = _git(
            repo_root,
            "checkout-index",
            "--all",
            "--force",
            f"--prefix={prefix}",
            env=env,
        )
        if checkout.returncode != 0:
            raise SnapshotError(
                f"git checkout-index for {sha[:12]} failed: "
                f"{(checkout.stderr or '').strip() or '<no detail>'}"
            )
    finally:
        try:
            index_file.unlink()
        except OSError:
            pass


def sweep_tmp(root: Path | None = None) -> int:
    """Remove stale in-progress build dirs under ``<root>/tmp`` (startup recovery).

    A materialization that crashed mid-build leaves a ``tmp/<uuid>/`` (and its
    ``<uuid>.index``); these are never read by anyone, so clearing them on startup
    reclaims the space without disrupting any published ``<sha>`` entry. Returns the
    number of top-level tmp entries removed."""
    root = root or store_root()
    tmp = root / "tmp"
    if not tmp.is_dir():
        return 0
    removed = 0
    for child in tmp.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
            removed += 1
        except OSError:  # pragma: no cover - best effort
            pass
    return removed


def materialize(
    ref: str = DEFAULT_REF,
    *,
    source_mode: str = SOURCE_ATTESTED,
    repo_root: str | None = None,
    fetch: bool = True,
) -> SnapshotHandle:
    """Materialize a code-reading read root for ``ref`` and return a :class:`SnapshotHandle`.

    ``attested`` (default): resolve ``ref`` to an immutable SHA (fetching from origin
    first when present) and materialize a faithful snapshot of the committed tree into the
    content-addressed store, reusing an existing ``<root>/<sha>`` entry. The handle is
    ``signable``.

    ``local``: hand back the in-place checkout (``repo_root``) untouched — no fetch, no
    materialization, dirty content allowed. The handle is never ``signable``.

    Raises :class:`SnapshotFetchError` / :class:`SnapshotRefError` / :class:`SnapshotError`
    on failure; attested mode fails closed (no snapshot, no handle)."""
    if source_mode not in _SOURCE_MODES:
        raise SnapshotError(
            f"invalid source mode {source_mode!r}; expected one of {', '.join(_SOURCE_MODES)}"
        )
    root_dir = str(repo_root) if repo_root else "."

    if source_mode == SOURCE_LOCAL:
        # The read root IS the server's checkout (possibly dirty); never signed.
        return SnapshotHandle(
            path=Path(root_dir).resolve(),
            sha=None,
            source=SOURCE_LOCAL,
        )

    sha = resolve_ref(ref, repo_root, fetch=fetch)
    store = store_root()
    dest = entry_path(sha, store)
    if dest.is_dir():
        # Cache hit — immutable by SHA. Read the caveats persisted at build time rather
        # than re-walking the tree / re-running ls-tree on every hit; if the sidecar is
        # missing (e.g. an entry built by an older binary), recompute once and persist.
        # Reading from the sidecar also keeps submodule detection correct when THIS
        # process's object DB lacks the SHA's objects (another process built the entry).
        cached = _load_caveats(sha, store)
        if cached is None:
            lfs, subs = _detect_lfs_pointers(dest), _list_submodules(root_dir, sha)
            _store_caveats(sha, store, lfs, subs)
        else:
            lfs, subs = cached
        return SnapshotHandle(
            path=dest, sha=sha, source=SOURCE_ATTESTED, lfs_pointers=lfs, submodules=subs
        )

    tmp_parent = _tmp_root(store)
    build = tmp_parent / f"build-{sha[:12]}-{uuid.uuid4().hex}"
    try:
        _materialize_tree(root_dir, sha, build)
        _fsync_dir(build)
        try:
            os.rename(build, dest)
        except OSError:
            # Another materialization won the race (same SHA == same content); keep
            # theirs and drop ours.
            if dest.is_dir():
                shutil.rmtree(build, ignore_errors=True)
            else:
                raise
        else:
            _fsync_dir(dest.parent)
        # Detect + persist the faithfulness caveats once, at build time (when this
        # process's object DB definitely holds the SHA), so later cache hits are cheap
        # and correct regardless of which clone built the entry.
        lfs, subs = _detect_lfs_pointers(dest), _list_submodules(root_dir, sha)
        _store_caveats(sha, store, lfs, subs)
        return SnapshotHandle(
            path=dest, sha=sha, source=SOURCE_ATTESTED, lfs_pointers=lfs, submodules=subs
        )
    except BaseException:
        # Never leave a partial build behind (it would only ever be swept anyway, but be
        # tidy); re-raise so attested mode fails closed.
        shutil.rmtree(build, ignore_errors=True)
        idx = build.parent / (build.name + ".index")
        try:
            idx.unlink()
        except OSError:
            pass
        raise


# The directory name the materialized ticket store sits under, so a root materialized by
# :func:`materialize_tickets` resolves through ``config.tracker_dir(<root>)`` (which defaults
# to ``.tickets-tracker`` under the root). The ``tickets`` branch tree's top level IS the
# tracker contents (the per-ticket event dirs), so we write that tree there verbatim.
_TRACKER_DIRNAME = ".tickets-tracker"


def materialize_tickets(
    ref: str = "tickets",
    *,
    repo_root: str | None = None,
    fetch: bool = True,
) -> str:
    """Materialize a pinned, read-only copy of the ticket store and return its ROOT path.

    The code-reading gates run their agent against an attested code snapshot, but the ticket
    store lives on the orphan ``tickets`` branch (gitignored worktree ``.tickets-tracker/``)
    and is therefore ABSENT from that code snapshot — so the agent's rebar ticket tools would
    error trying to read it. This mirrors :func:`materialize` for the ticket store: resolve
    ``ref`` to an immutable SHA (preferring ``origin/tickets`` when an origin exists so it
    pins the shared store, not a stale local copy) and materialize that tree into
    ``<store>/tickets-<sha>/.tickets-tracker/`` via the SAME throwaway-index +
    build-dir + atomic-``rename`` + cache-hit-by-path pattern. The returned ROOT
    (``<store>/tickets-<sha>``) is what a gate points its rebar ticket tools at:
    ``config.tracker_dir(<root>)`` resolves to the ``.tickets-tracker/`` subdir holding the
    materialized event dirs. Fails closed (no path) on error, like :func:`materialize`."""
    root_dir = str(repo_root) if repo_root else "."
    # Prefer <remote>/<ref> when the configured tickets remote exists (fetch first, so we
    # pin the SHARED store, not a stale local copy — matching how the code path resolves the
    # shared ref), else the local branch. The remote is config-resolved (sync.remote, default
    # "origin"); a malformed config falls back to "origin". Fall back to the local branch when
    # the remote has no such ref yet (a freshly initialized repo whose tickets branch has not
    # been pushed): a missing remote ref must not block reading the store that exists locally.
    from rebar.config import ConfigError as _ConfigError
    from rebar.config import tickets_remote as _tickets_remote

    try:
        remote = _tickets_remote(root_dir)
    except _ConfigError:
        remote = "origin"
    if fetch and _has_remote(root_dir, remote):
        try:
            sha = resolve_ref(f"{remote}/{ref}", repo_root, fetch=fetch, remote=remote)
        except SnapshotRefError:
            sha = resolve_ref(ref, repo_root, fetch=False)
    else:
        sha = resolve_ref(ref, repo_root, fetch=fetch)
    store = store_root()
    dest = store / f"tickets-{sha}"
    if dest.is_dir():
        # Cache hit — immutable by SHA (same key scheme as the code entries, namespaced by
        # the `tickets-` prefix so it never collides with a `<sha>` code entry).
        return str(dest)

    tmp_parent = _tmp_root(store)
    build = tmp_parent / f"tickets-{sha[:12]}-{uuid.uuid4().hex}"
    tracker = build / _TRACKER_DIRNAME
    try:
        _materialize_tree(root_dir, sha, tracker)
        _fsync_dir(build)
        try:
            os.rename(build, dest)
        except OSError:
            # Another materialization won the race (same SHA == same content); keep theirs.
            if dest.is_dir():
                shutil.rmtree(build, ignore_errors=True)
            else:
                raise
        else:
            _fsync_dir(dest.parent)
        return str(dest)
    except BaseException:
        shutil.rmtree(build, ignore_errors=True)
        idx = tracker.parent / (tracker.name + ".index")
        try:
            idx.unlink()
        except OSError:
            pass
        raise
