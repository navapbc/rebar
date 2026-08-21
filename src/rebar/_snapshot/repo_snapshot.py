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
``uploadpack.allowReachableSHA1InWant`` (else fetch a containing ref then resolve).
A private-repo fetch with missing credentials raises a descriptive, actionable
:class:`SnapshotFetchError` (attested mode fails closed; local mode never fetches).

Filtering policy — **filter iff you will NOT materialize**. ``--filter=blob:none`` is kept
only for *pure ref-RESOLUTION* fetches (its designed use: commits and trees) and DROPPED
(``--no-filter``) for any fetch backing a ref we are about to materialize, because (a) git
batches missing-blob fetches only in ``unpack-trees``' working-tree update path, which
``read-tree`` (no ``-u``) and ``checkout-index`` never reach — so a blob-starved
materialization degrades to ONE round-trip per file (hours on a 25k-file tree) — and (b)
``git fetch --filter`` permanently marks the remote a promisor
(``remote.<name>.promisor``/``.partialclonefilter``), so a clone that filters once keeps
filtering forever. An ALREADY-latched clone is repaired by :func:`_ensure_blobs_present`.
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
    _GIT_TIMEOUT,
    SnapshotError,
    SnapshotFetchError,
    SnapshotRefError,
    fetch_origin,
    git_run,
    has_remote,
    rev_parse,
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

# --------------------------------------------------------------------------------------
# Store layout
# --------------------------------------------------------------------------------------
# The content-addressed snapshot store lives OUTSIDE the repo so a gate's read-only/no-git
# tools never reach it and a snapshot is never mistaken for working-tree state. Layout:
#   <root>/<sha>/      a materialized, immutable snapshot (entry; content-addressed)
#   <root>/tmp/<uuid>/ an in-progress build (renamed into <sha> on success)
# Sibling modules add <root>/locks, <root>/trash, <root>/gc for the cache + janitor.

_STORE_DIRNAME = "rebar-gate-snapshots"

# An entry directory's name: a bare 40-hex code entry or a `tickets-<sha>` ticket-store
# entry. Lexical twin of the janitor's ``_is_entry`` (which also stats the fs).
_ENTRY_NAME_RE = re.compile(r"\A(tickets-)?[0-9a-f]{40}\Z")


def in_snapshot_entry(path: str | os.PathLike[str]) -> bool:
    """True when ``path`` lies inside a published snapshot-store entry.

    Entries are immutable, content-addressed trees (ADR 0005 D2): after publication
    nothing may write inside one — reads keep adding derived files otherwise (bug
    5c27-7926), which breaks the janitor's TOFU reverify digest and, with hardlink
    blob-sharing between adjacent entries (bug 8386), makes any in-place write a
    cross-entry corruption hazard. Writers of derived state (e.g. the reducer cache)
    consult this to skip the write when the tree they were pointed at is a snapshot.

    The check is structural — the store layout (``…/rebar-gate-snapshots/<entry>/…``)
    is recognized in the path itself — so it holds in any process regardless of which
    env/config produced the root it was handed."""
    parts = Path(os.path.abspath(os.fspath(path))).parts
    return any(
        parent == _STORE_DIRNAME and _ENTRY_NAME_RE.match(child) is not None
        for parent, child in pairwise(parts)
    )


def store_root() -> Path:
    """The base directory of the content-addressed snapshot store.

    ``REBAR_GATE_TMPDIR`` overrides the base (operators point it at a roomy local FS);
    otherwise :func:`tempfile.gettempdir` is used — never a hardcoded ``/tmp``. Created
    ``0700`` if absent. The override is resolved through the owned config seam
    (:func:`rebar.config.resolve_gate_tmpdir`), not read from ``os.environ`` here."""
    from rebar import config

    base = config.resolve_gate_tmpdir() or tempfile.gettempdir()
    root = Path(base) / _STORE_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:  # best-effort on platforms without full POSIX perms
        pass
    return root


def _fetch_lock_path() -> Path:
    """The cross-process ``git fetch`` lock file, inside the snapshot store.

    The store owns this layout, so the path is computed here and handed to
    :func:`~rebar._snapshot.git_fetch.fetch_origin` rather than that lower layer
    importing the store back (which would close an import cycle)."""
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

    The handle owns NO resource lifetime: snapshot-store entries are a shared cache whose
    cleanup is the janitor's (snapshot GC's) responsibility, never the handle's — a
    per-handle release would defeat the caching (bug 8386).
    """

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
    """Resolve a client ``ref`` (branch | tag | SHA) to an immutable commit SHA.

    When an ``origin`` remote exists and ``fetch`` is set, fetches first so the default
    ``origin/main`` (and any moving branch/tag) resolves against the remote, not a stale
    local copy. A SHA that is not present locally triggers a targeted fetch (requires the
    remote's ``uploadpack.allowReachableSHA1InWant``). Raises :class:`SnapshotRefError`
    (fail-closed) if the ref still does not resolve. ``blobless`` is forwarded verbatim to
    :func:`~rebar._snapshot.git_fetch.fetch_origin`; it defaults to ``True``
    (commits/trees only) because a bare resolution never needs file content. A caller
    about to MATERIALIZE the resolved SHA must pass ``blobless=False``."""
    root = str(repo_root) if repo_root else "."
    remote_present = fetch and has_remote(root, remote)
    if remote_present:
        fetch_origin(root, lock_path=_fetch_lock_path(), remote=remote, blobless=blobless)
    sha = rev_parse(root, ref)
    if sha is None and remote_present:
        # A bare SHA (or a ref only reachable by an explicit want) not present after the
        # general fetch — try a targeted fetch (allowReachableSHA1InWant on the remote).
        try:
            fetch_origin(
                root, lock_path=_fetch_lock_path(), ref=ref, remote=remote, blobless=blobless
            )
        except SnapshotFetchError:
            pass  # fall through to the descriptive ref error below
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
    """Cheap, OFFLINE probe: does the local object DB lack any blob of ``sha``'s tree?

    ``git rev-list --objects --missing=print --no-object-names --no-walk <sha>`` lists the
    commit's tree entries, prefixing each ABSENT object with ``?``. ``--missing`` turns git's
    own lazy fetch OFF internally, so the probe never perturbs what it measures (no env guard
    needed — and none may be used: ``GIT_NO_LAZY_FETCH`` is silently ignored before git
    2.45); ``--no-walk`` scopes it to this ONE commit's tree, not all history. A normal clone
    has nothing missing, so this returns ``False`` and the top-up is inherently a no-op there.
    A probe failure reads as "nothing known to be missing" — it must never become a new way
    for materialization to fail."""
    probe = ["rev-list", "--objects", "--missing=print", "--no-object-names", "--no-walk"]
    # --end-of-options: the SHA is a positional, so it must never be read as an option.
    proc = git_run(repo_root, *probe, "--end-of-options", sha)
    if proc.returncode != 0:
        return False
    return any(line.startswith("?") for line in proc.stdout.splitlines())


# raw-git-ok: read-oriented git helper, variable subcommand
def _ensure_blobs_present(repo_root: str, sha: str, remote: str) -> None:
    """Best-effort: guarantee ``sha``'s blobs are local BEFORE the plumbing runs.

    ``read-tree`` (without ``-u``) and ``checkout-index`` never enter ``unpack-trees``'
    ``check_updates()`` — the only place git batches missing-blob requests (via
    ``prefetch_cache_entries()``). So on a blob-starved partial clone each absent blob costs
    its own sequential fetch RPC (~2 hours on a 25k-file tree). Fetching them up front in ONE
    batched RPC is what makes the attested path INDEPENDENT of lazy fetching rather than
    merely slow because of it. ``--no-filter`` overrides the remote's configured
    ``partialclonefilter`` (a plain fetch silently re-applies it and refills nothing);
    ``--refetch`` disables negotiation so an already-present commit still transfers its
    objects instead of no-op'ing.

    Deliberately BEST-EFFORT and silent on failure — ``--refetch`` needs git >= 2.36, and the
    remote may be absent, offline, or reject our credentials. Production still has lazy fetch
    as the fallback, so a failed top-up degrades to today's behaviour and never adds a NEW
    failure mode. Exactly ONE fetch per materialization; a loop would defeat the point."""
    if not _has_missing_blobs(repo_root, sha) or not has_remote(repo_root, remote):
        return
    # SECURITY: the SHA reaches a fetch positional, so it MUST be terminated with
    # --end-of-options — same reasoning as the targeted fetch in git_fetch.fetch_origin (without it,
    # a value like "--upload-pack=<cmd>" would be parsed as an option and EXECUTE).
    argv = ["fetch", "--no-filter", "--refetch", "--quiet", remote, "--end-of-options", sha]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        # Same throughput-keyed abort as fetch_origin: this is the OTHER network fetch on
        # the materialization path, so without it a stalled remote parks this child on the
        # 300s wall clock too. The -c pairs must precede the subcommand.
        subprocess.run(
            ["git", "-C", repo_root, *stall_abort_args(), *argv],
            capture_output=True,
            text=True,
            env=env,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        pass  # best-effort: fall through to the plumbing, which still has lazy fetch


def _materialize_tree(repo_root: str, sha: str, dest_tmp: Path) -> None:
    """Faithfully write the committed tree at ``sha`` into ``dest_tmp`` via git plumbing.

    Uses a throwaway index (``GIT_INDEX_FILE``) so the repo's own index/working tree is
    never touched — this is what keeps concurrent materializations of different SHAs from
    contending on ``index.lock``. ``GIT_TERMINAL_PROMPT=0`` is set for the same reason
    :func:`~rebar._snapshot.git_fetch.fetch_origin` sets it — this path can drive git's
    own (lazy) network fetch, so without it a missing credential would block on a TTY
    prompt and hang the server."""
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

    # blobless=False: this ref WILL be materialized, so its blobs must arrive with the commit
    # in one RPC (see the module docstring's filtering policy).
    sha = resolve_ref(ref, repo_root, fetch=fetch, blobless=False)
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
        # Gated on "are blobs actually missing?", NOT on `fetch`: cache.acquire (the real
        # entry point) resolves the ref itself and calls us with fetch=False.
        _ensure_blobs_present(root_dir, sha, "origin")
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


def _pin_tickets_sha(
    root_dir: str, ref: str, remote: str, *, fetch: bool
) -> tuple[str, str] | None:
    """Resolve the ticket-store pin from the LIVE tracker repo, or ``None`` to use the
    caller's ``origin/<ref>`` -> local-branch chain (bug 2a6f).

    Returns ``(sha, source_dir)`` — ``source_dir`` being the repo that owns the objects.

    ``None`` is returned whenever the tracker repo is not usable as a pin source (absent —
    CI, a checkout-less environment — or not a git repo). Otherwise:

    * ``fetch=True`` (``review-plan``, standalone ``verify-completion``, MCP): reconverge the
      tracker with ``<remote>/<ref>`` FIRST, un-throttled, then CONFIRM the outcome — the
      store's :func:`~rebar._store.sync.reconverge` returns ``None`` on success and on every
      failure/early-out alike, so it carries no usable signal and we ask git directly with
      ``merge-base --is-ancestor``. Only a confirmed at-or-ahead ``HEAD`` is pinned; anything
      else (fetch failed, lock timeout, no remote-tracking ref) returns ``None`` so the caller
      falls back to today's exact behaviour. That keeps "no content visible today is lost" an
      unconditional invariant rather than one contingent on a throttle window being open.
    * ``fetch=False`` (the close gate, which passes it for the LOCAL code ref ``HEAD``): pin
      tracker ``HEAD`` as-is. It is still current-at-close, and never the stale mirror.
    """
    from rebar.config import ConfigError as _ConfigError
    from rebar.config import tickets_branch as _tickets_branch
    from rebar.config import tracker_dir as _tracker_dir

    try:
        tracker = str(_tracker_dir(root_dir))
    except _ConfigError:
        return None
    if not os.path.isdir(tracker):
        return None
    # Ask git, don't stat `.git` — the tracker is a standalone CLONE in a long-lived checkout
    # (`.git` is a directory) but a linked WORKTREE of the code repo in a freshly initialized
    # one (`.git` is a FILE). Only the clone layout can drift, but both must resolve here, and
    # a `.git`-is-a-dir test silently excludes the worktree layout.
    probe = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-dir"], capture_output=True, text=True
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
        )
        if probe.returncode != 0:
            return None

    head = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "HEAD"], capture_output=True, text=True
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
    materialized event dirs. Fails closed (no path) on error, like :func:`materialize`.

    The pin is taken from the LIVE tracker repo's ``HEAD`` whenever that repo is on disk (bug
    2a6f): the tracker is a SEPARATE repository, so the code repo's ``refs/heads/tickets`` is
    only a mirror that advances on fetch — pinning it made the gate read an arbitrarily stale
    store (measured in the wild at 6757 commits behind) and report recorded comments as
    nonexistent. Tracker ``HEAD`` is the state every other rebar read sees, so the pin is
    current-by-construction and the gate's deterministic half (which reads the live store)
    agrees with the agent's ``show_ticket``. See :func:`_pin_tickets_sha`."""
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
        # Cache hit — immutable by SHA (same key scheme as the code entries, namespaced by
        # the `tickets-` prefix so it never collides with a `<sha>` code entry). Bump the
        # entry's recency: the janitor evicts LRU by mtime, which every hit must touch
        # (ADR 0005 D4) — mirrors ``cache.acquire``. Deferred import: ``cache`` imports
        # THIS module, so a module-level import would be a cycle.
        from rebar._snapshot.cache import touch_entry as _touch_entry

        _touch_entry(dest)
        return str(dest)

    tmp_parent = _tmp_root(store)
    build = tmp_parent / f"tickets-{sha[:12]}-{uuid.uuid4().hex}"
    tracker = build / _TRACKER_DIRNAME
    try:
        # Same probe-gated, one-RPC top-up as the code path — against the already-resolved
        # tickets remote (sync.remote), never a hardcoded "origin". Both run against
        # `source_dir`: when the pin came from the tracker repo, only that repo is guaranteed
        # to hold the commit's objects.
        _ensure_blobs_present(source_dir, sha, remote)
        # The tickets tip advances every few seconds while each commit touches a handful of
        # files, so the cache-hit branch above almost never fires and a full checkout-index
        # would write a whole fresh copy of the tree per resolution (47 GiB measured in the
        # wild). Build from a hardlinked neighbouring entry when one exists, rewriting only
        # the changed paths; the helper fails CLOSED (returns False) on any doubt, and the
        # full materialization below is then the unchanged behaviour.
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
            # We WON the populate race, so account this entry's bytes exactly once. The
            # ticket-store entries are the largest thing in the store, and without this the
            # janitor's running byte total — and therefore its byte-total cap — simply cannot
            # see them. Mirrors ``cache.acquire``'s add_bytes on its own populate branch; the
            # cache-hit and lost-race paths above deliberately do NOT account (already counted
            # / the winner counts it). Deferred import: ``cache`` imports THIS module, so a
            # module-level import would be a cycle.
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
