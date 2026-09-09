"""Faithfully materialize an immutable git ref without touching the working index.

Attested gates need a pinned committed tree, not the mutable server checkout or lossy
``git archive`` output. A coalesced fetch resolves the SHA; ``read-tree`` and
``checkout-index`` use a throwaway ``GIT_INDEX_FILE`` before atomic publication. This
preserves committed bytes, including export-ignored and unsubstituted files, while
different SHAs contend only during fetch. LFS pointer blobs and omitted submodule gitlinks
are detected and surfaced on the handle.

Builds use a private configured/temp root outside the repository, POSIX locking with an
atomic-mkdir fallback, and fsynced rename from ``tmp`` so readers never see partial trees;
:func:`sweep_tmp` recovers crashes. Arbitrary-SHA fetches require the remote to allow
reachable wants, and private-repo credential failures close with :class:`SnapshotFetchError`.

Pure resolution may fetch ``--filter=blob:none``. A fetch backing materialization uses
``--no-filter`` so blobs arrive together and an ordinary clone is not latched into promisor
mode; :func:`_ensure_blobs_present` repairs already-partial clones in one batch.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from rebar._snapshot.delta_tree import materialize_via_donor
from rebar._snapshot.git_fetch import (
    SnapshotError,
    SnapshotFetchError,
    SnapshotRefError,
    fetch_origin,
    fetch_timeout,
    git_run,
    has_remote,
    is_missing_ref,
    is_present_full_sha,
    rev_parse,
    scoped_fetch_target,
    stall_abort_args,
)
from rebar._store import fsutil

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

# Store snapshots outside the repository: ``<root>/<sha>`` is immutable and
# content-addressed; ``<root>/tmp/<uuid>`` is an unpublished build renamed on success.
# Sibling cache/janitor modules own ``locks``, ``trash``, and ``gc``.

_STORE_DIRNAME = "rebar-gate-snapshots"

# An entry directory's name: a bare 40-hex code entry or a `tickets-<sha>` ticket-store
# entry. Lexical twin of the janitor's ``_is_entry`` (which also stats the fs).
_ENTRY_NAME_RE = re.compile(r"\A(tickets-)?[0-9a-f]{40}\Z")


def in_snapshot_entry(path: str | os.PathLike[str]) -> bool:
    """Return whether ``path`` is structurally inside a published snapshot entry.

    Published trees are immutable: derived writes would invalidate digest reverification
    and could corrupt hardlinked neighbours. Recognizing the store layout in the path lets
    any process suppress such writes without reproducing its environment/config root."""
    parts = Path(os.path.abspath(os.fspath(path))).parts
    return any(
        parent == _STORE_DIRNAME and _ENTRY_NAME_RE.match(child) is not None
        for parent, child in pairwise(parts)
    )


def peek_store_root() -> Path:
    """Derive the store root without creating it or changing permissions.

    Read-only diagnostics such as ``rebar doctor`` can therefore name absent-store locks."""
    from rebar import config

    base = config.resolve_gate_tmpdir() or tempfile.gettempdir()
    return Path(base) / _STORE_DIRNAME


def store_root() -> Path:
    """Return the content-addressed store root, creating it privately if absent.

    The owned config seam selects an operator override or :func:`tempfile.gettempdir`,
    never a hardcoded ``/tmp``; creation uses mode ``0700``."""
    root = peek_store_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:  # best-effort on platforms without full POSIX perms
        pass
    return root


def _fetch_lock_path() -> Path:
    """Return the store-owned process fetch-lock path.

    Passing it to lower-level ``fetch_origin`` preserves the one-way dependency."""
    return store_root() / "locks" / "fetch.lock"


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
    try:
        payload = json.dumps({"lfs_pointers": list(lfs), "submodules": list(subs)})
        fsutil.atomic_write(path, payload)
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
    """A gate's code and ticket read roots plus faithfulness metadata.

    Attested ``path`` and ``sha`` identify an immutable tree; local mode uses the possibly
    dirty checkout and ``sha=None``. ``lfs_pointers`` and ``submodules`` expose incomplete
    content. ``tickets_path`` separately pins the orphan ticket store in attested mode, or
    is ``None`` for local reads. Handles own no lifetime; the janitor manages shared entries."""

    path: Path
    sha: str | None
    source: str
    lfs_pointers: tuple[str, ...] = ()
    submodules: tuple[str, ...] = ()
    tickets_path: str | None = None

    @property
    def signable(self) -> bool:
        """Only an attested snapshot pinned to an immutable SHA may back a signature."""
        return self.source == SOURCE_ATTESTED and self.sha is not None


def resolve_ref(
    ref: str,
    repo_root: str | None = None,
    *,
    fetch: bool = True,
    remote: str = "origin",
    blobless: bool = True,
) -> str:
    """Resolve a branch, tag, or SHA to an immutable commit.

    A present full SHA skips fetching; other refs fetch only their scoped target. Absent SHAs
    require reachable-want support. Resolution and transport/auth/stall failures close as
    :class:`SnapshotRefError` and :class:`SnapshotFetchError`; materializers pass
    ``blobless=False`` through to ``fetch_origin``."""
    root = str(repo_root) if repo_root else "."
    remote_present = fetch and has_remote(root, remote)
    # Present full SHAs are immutable and need no fetch; otherwise target only `ref`, never
    # all heads. Transport failures close; a missing target becomes a resolution error.
    if remote_present and not is_present_full_sha(root, ref):
        try:
            fetch_origin(
                root,
                lock_path=_fetch_lock_path(),
                ref=scoped_fetch_target(ref, remote),
                remote=remote,
                blobless=blobless,
            )
        except SnapshotFetchError as exc:
            if not is_missing_ref(exc.stderr):
                raise
    sha = rev_parse(root, ref)
    if sha is None and remote_present:
        try:
            fetch_origin(
                root, lock_path=_fetch_lock_path(), ref=ref, remote=remote, blobless=blobless
            )
        except SnapshotFetchError as exc:
            if not is_missing_ref(exc.stderr):
                raise
        sha = rev_parse(root, ref)
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
    proc = git_run(repo_root, "ls-tree", "-r", "--full-tree", sha)
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


# --------------------------------------------------------------------------------------
# Blob top-up — make materialization independent of git's per-blob lazy fetch
# --------------------------------------------------------------------------------------
def _has_missing_blobs(repo_root: str, sha: str) -> bool:
    """Probe offline for missing objects in one commit's tree.

    ``rev-list --missing=print --no-walk`` disables lazy fetching and marks absences with
    ``?``. A normal clone or failed probe reports no known missing blobs, so this best-effort
    optimization cannot fail materialization."""
    probe = ["rev-list", "--objects", "--missing=print", "--no-object-names", "--no-walk"]
    # --end-of-options: the SHA is a positional, so it must never be read as an option.
    proc = git_run(repo_root, *probe, "--end-of-options", sha)
    if proc.returncode != 0:
        return False
    return any(line.startswith("?") for line in proc.stdout.splitlines())


# raw-git-ok: read-oriented git helper, variable subcommand
def _ensure_blobs_present(repo_root: str, sha: str, remote: str) -> None:
    """Best-effort batch-fetch ``sha``'s missing blobs before plumbing runs.

    ``read-tree`` plus ``checkout-index`` would otherwise lazy-fetch each blob separately.
    One ``--no-filter --refetch`` RPC overrides partial-clone filtering and transfers objects
    even for an existing commit. Unsupported git, absent remotes, or fetch failure stay silent
    and fall back to lazy fetching; never loop or add a materialization failure mode."""
    if not _has_missing_blobs(repo_root, sha) or not has_remote(repo_root, remote):
        return
    # SECURITY: terminate options before the SHA so an option-shaped value cannot execute
    # through git; this mirrors git_fetch.fetch_origin's targeted fetch.
    argv = ["fetch", "--no-filter", "--refetch", "--quiet", remote, "--end-of-options", sha]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        # Reuse fetch_origin's low-speed abort and live generous wall-clock bound; ``-c``
        # pairs precede the subcommand.
        subprocess.run(
            ["git", "-C", repo_root, *stall_abort_args(), *argv],
            capture_output=True,
            text=True,
            env=env,
            timeout=fetch_timeout(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass  # best-effort: fall through to the plumbing, which still has lazy fetch


def _materialize_tree(repo_root: str, sha: str, dest_tmp: Path) -> None:
    """Write ``sha`` faithfully into ``dest_tmp`` through a throwaway git index.

    The repository index and worktree remain untouched, so SHAs materialize concurrently.
    Disable terminal prompts because lazy fetch may reach the network and missing credentials
    must not hang the server."""
    dest_tmp.mkdir(parents=True, exist_ok=True)
    index_file = dest_tmp.parent / (dest_tmp.name + ".index")
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file), "GIT_TERMINAL_PROMPT": "0"}
    try:
        read = git_run(repo_root, "read-tree", sha, env=env)
        if read.returncode != 0:
            raise SnapshotError(
                f"git read-tree {sha[:12]} failed: {(read.stderr or '').strip() or '<no detail>'}"
            )
        # checkout-index creates leading directories under --prefix automatically.
        prefix = str(dest_tmp) + os.sep
        checkout = git_run(
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
    """Remove unread stale builds and indexes from ``<root>/tmp`` at startup.

    Published SHA entries remain untouched; return the number of top-level items removed."""
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


def _snapshot_store_has_room(root: Path) -> bool:
    from rebar._snapshot.janitor import has_min_free_space

    return has_min_free_space(root)


def _raise_snapshot_low_disk(root: Path) -> None:
    from rebar._snapshot.janitor import SnapshotLowDiskError, volume_free_space

    raise SnapshotLowDiskError(volume_free_space(root))


def materialize(
    ref: str = DEFAULT_REF,
    *,
    source_mode: str = SOURCE_ATTESTED,
    repo_root: str | None = None,
    fetch: bool = True,
) -> SnapshotHandle:
    """Materialize a code-reading read root for ``ref`` and return a SnapshotHandle.

    Attested mode resolves to an immutable SHA and populates/reuses the content-addressed
    store; local mode hands back the in-place checkout unsigned. Snapshot/ref/fetch errors
    fail closed.
    """
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

    sha = resolve_ref(ref, repo_root, fetch=fetch, blobless=False)
    store = store_root()
    dest = entry_path(sha, store)
    if dest.is_dir():
        # Cache hit: immutable by SHA; read persisted caveats or recompute once.
        cached = _load_caveats(sha, store)
        if cached is None:
            lfs, subs = _detect_lfs_pointers(dest), _list_submodules(root_dir, sha)
            _store_caveats(sha, store, lfs, subs)
        else:
            lfs, subs = cached
        return SnapshotHandle(
            path=dest, sha=sha, source=SOURCE_ATTESTED, lfs_pointers=lfs, submodules=subs
        )

    if not _snapshot_store_has_room(store):
        _raise_snapshot_low_disk(store)

    tmp_parent = _tmp_root(store)
    build = tmp_parent / f"build-{sha[:12]}-{uuid.uuid4().hex}"
    try:
        _ensure_blobs_present(root_dir, sha, "origin")
        # Reuse a neighbouring entry when possible (bug 8386); falls back closed to a full build.
        if not materialize_via_donor(root_dir, sha, build, store=store, entry_prefix="", subdir=""):
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
        # Persist faithfulness caveats while this builder has the SHA, keeping later cache
        # hits cheap and independent of the clone that populated the entry.
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


# The dir the materialized store sits under — deliberately the DEFAULT name so a root made by
# :func:`materialize_tickets` resolves through ``config.tracker_dir(<that root>)`` onto it (the
# ``tickets`` tree's top level IS the tracker contents). Relocation moves the OPERATOR's store.
# tickets-boundary-ok: names a dir in the snapshot root this module creates, not the live store
_TRACKER_DIRNAME = ".tickets-tracker"


def _pin_tickets_sha(
    root_dir: str, ref: str, remote: str, *, fetch: bool
) -> tuple[str, str] | None:
    """Return a live tracker ``(sha, object-owning repo)``, or ``None`` for ref fallback.

    An absent/non-git tracker cannot pin. With ``fetch=True``, reconverge unthrottled and
    independently confirm its HEAD contains the shared branch; any uncertainty falls back so
    currently visible content is never lost. With ``fetch=False``, the local close gate pins
    tracker HEAD as-is instead of a stale mirror."""
    from rebar.config import ConfigError as _ConfigError
    from rebar.config import tickets_branch as _tickets_branch
    from rebar.config import tracker_dir as _tracker_dir

    try:
        tracker = str(_tracker_dir(root_dir))
    except _ConfigError:
        return None
    if not os.path.isdir(tracker):
        return None
    # Ask git rather than stat .git: standalone clones use a directory, linked worktrees a
    # file, and both valid layouts must resolve.
    probe = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return None

    if fetch:
        try:
            branch = _tickets_branch(root_dir)
        except _ConfigError:
            branch = ref
        try:
            from rebar._store.sync import reconverge as _reconverge

            _reconverge(tracker)
        except Exception:  # noqa: BLE001 — best-effort freshness; the confirm below decides
            pass
        # Confirm, don't assume: HEAD must contain everything the shared ref holds.
        probe = subprocess.run(
            [
                "git",
                "-C",
                tracker,
                "merge-base",
                "--is-ancestor",
                f"{remote}/{branch}",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            return None

    head = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    sha = (head.stdout or "").strip()
    if head.returncode != 0 or not sha:
        return None
    return sha, tracker


def materialize_tickets(
    ref: str = "tickets",
    *,
    repo_root: str | None = None,
    fetch: bool = True,
) -> str:
    """Materialize a pinned, read-only ticket store and return its root.

    The orphan ticket branch is absent from an attested code tree, so gate tools need
    ``<store>/tickets-<sha>/.tickets-tracker``. This mirrors :func:`materialize` with a
    throwaway index, build directory, atomic rename, and path-keyed cache; the returned parent
    lets ``config.tracker_dir`` find the event store and failures close. Prefer the separate
    live tracker's HEAD, which matches ordinary reads, over the code repo's fetch-dependent
    mirror; otherwise use the shared remote then local-ref fallback."""
    root_dir = str(repo_root) if repo_root else "."
    # Prefer the configured remote's freshly fetched shared ref; malformed config defaults to
    # origin. Fall back locally when a new store has not yet published that remote ref.
    from rebar.config import ConfigError as _ConfigError
    from rebar.config import tickets_remote as _tickets_remote

    try:
        remote = _tickets_remote(root_dir)
    except _ConfigError:
        remote = "origin"
    # Prefer the LIVE tracker repo's HEAD (bug 2a6f) — see the docstring. `source_dir` is the
    # repo the tree is materialized FROM: the tracker owns those objects, the code repo may not.
    pinned = _pin_tickets_sha(root_dir, ref, remote, fetch=fetch)
    if pinned is not None:
        sha, source_dir = pinned
    else:
        source_dir = root_dir
        # blobless=False on every fetching resolution — this ref is about to be materialized.
        if fetch and has_remote(root_dir, remote):
            try:
                sha = resolve_ref(
                    f"{remote}/{ref}", repo_root, fetch=fetch, remote=remote, blobless=False
                )
            except SnapshotRefError:
                sha = resolve_ref(ref, repo_root, fetch=False)
        else:
            sha = resolve_ref(ref, repo_root, fetch=fetch, blobless=False)
    store = store_root()
    dest = store / f"tickets-{sha}"
    if dest.is_dir():
        # The tickets prefix separates immutable ticket and code entries. Touch hit mtime for
        # janitor LRU; defer the cache import to avoid its reverse module dependency.
        from rebar._snapshot.cache import touch_entry as _touch_entry

        _touch_entry(dest)
        return str(dest)

    tmp_parent = _tmp_root(store)
    build = tmp_parent / f"tickets-{sha[:12]}-{uuid.uuid4().hex}"
    tracker = build / _TRACKER_DIRNAME
    try:
        # Use the code path's one-RPC probe/top-up against the resolved tickets remote and
        # object-owning source_dir, which may be the separate tracker repo.
        _ensure_blobs_present(source_dir, sha, remote)
        # Fast-moving ticket tips rarely hit cache, so reuse a hardlinked neighbour and rewrite
        # its delta. Any doubt returns False and preserves full materialization.
        if not materialize_via_donor(
            source_dir,
            sha,
            tracker,
            store=store,
            entry_prefix="tickets-",
            subdir=_TRACKER_DIRNAME,
        ):
            _materialize_tree(source_dir, sha, tracker)
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
            # The populate-race winner alone accounts this large ticket entry's exclusive
            # bytes; hits and losers were already counted. Defer imports to avoid the cache's
            # reverse dependency.
            from rebar._snapshot.cache import add_bytes as _add_bytes
            from rebar._snapshot.cache import exclusive_size as _exclusive_size

            try:
                _add_bytes(_exclusive_size(dest), store)
            except OSError:  # pragma: no cover - best effort, like the janitor's fs calls
                pass  # accounting is bookkeeping; it must never fail a materialization
        return str(dest)
    except BaseException:
        shutil.rmtree(build, ignore_errors=True)
        idx = tracker.parent / (tracker.name + ".index")
        try:
            idx.unlink()
        except OSError:
            pass
        raise
