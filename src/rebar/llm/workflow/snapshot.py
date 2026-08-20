"""Hardened git-ref filesystem snapshot for agentic steps (WS-D2).

An agent step often needs to read the repository at a CALLER-CHOSEN git ref (e.g.
the commit a code-review workflow targets), not the dirty working tree. This builds
a safe, immutable, read-only view of the repo at a resolved commit:

  ``git rev-parse <ref>^{commit}`` → a full SHA → ``git archive`` that SHA →
  a HARDENED tar extract → ``chmod`` the tree read-only.

Safety properties:
  * **Immutable input** — the snapshot is bound to the resolved SHA, never the
    mutable ref/branch, so two steps at "the same ref" see byte-identical trees.
  * **No .git** — ``git archive`` emits only tracked content at the SHA, so an
    agent's read-only/no-git tools cannot reach repo history or hooks.
  * **Hardened extraction** — extraction uses the stdlib ``data`` tar filter
    (rejects absolute paths, ``..`` escapes, and links/symlinks pointing outside
    the destination), plus a total-size guard, so a malicious tar cannot write
    outside the snapshot.
  * **Cache by SHA** — snapshots live at ``<repo>/.rebar/run_snapshots/<sha>`` and
    are reused across steps/runs; the WS-C3 TTL sweep
    (:func:`rebar.llm.workflow.executor.sweep_orphan_snapshots`) GCs stale ones.

Documented git-archive behavior (callers should know): ``.gitattributes``
``export-ignore`` paths are omitted and ``export-subst`` is applied (git archive
does this natively); **submodule** contents are NOT included (archive stops at the
gitlink); **Git-LFS** files appear as their pointer text, not the smudged content
(no LFS smudge runs). Untracked/gitignored files are absent by construction.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

from rebar._snapshot.git_fetch import stall_abort_args
from rebar.llm.errors import WorkflowError

# Total extracted-bytes ceiling — a snapshot is a source tree, not a data lake;
# this bounds a pathological/hostile archive.
DEFAULT_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024  # 512 MiB
# Member-count ceiling — bounds a tar of millions of tiny entries (inode exhaustion
# / extraction-time blowup) that the byte cap alone would not catch.
DEFAULT_MAX_SNAPSHOT_FILES = 200_000
#: Wall-clock ceiling on the ``git archive`` child (bug 093a). The blocking read lives
#: inside ``tarfile.__read``, where a ``communicate(timeout=…)`` cannot reach it, so the
#: bound is enforced by a watchdog that KILLS the child — killing it closes the stdout
#: pipe, which unblocks the extractor. 300s matches the ceiling 77e1/747f chose for the
#: other git call sites and is deliberately NOT lower: it is a backstop, not the primary
#: instrument. The primary instrument for a stalled transfer is the throughput-keyed
#: abort below, because elapsed time is the wrong axis for dead air.
_GIT_TIMEOUT = 300
#: How long the cleanup path waits for a killed child before giving up on the reap.
_REAP_TIMEOUT = 10
#: Chunk size for discarding trailing stdout/stderr bytes (O(1) memory, not a read()).
_DRAIN_CHUNK = 64 * 1024


#: ``scheme://userinfo@host`` — the credential-bearing shape a ``TimeoutExpired.cmd`` can
#: carry (bug 77e1). Masks the WHOLE userinfo, so neither the token nor the username
#: survives into an error message or a log line.
_CRED_URL_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@")


def _redact_cmd(cmd: object) -> str:
    """Render a subprocess ``cmd`` with any URL userinfo masked.

    ``subprocess.TimeoutExpired`` is neither an ``OSError`` nor a ``CalledProcessError``,
    so it escapes ordinary ``except`` tuples — and its ``cmd`` carries any
    ``user:token@host`` URL verbatim. Bug 77e1 found that the hard way: converting a hang
    into a credential disclosure is not a fix. Every timeout on this path is routed
    through here before it reaches a :class:`SnapshotError` message.
    """
    text = " ".join(str(part) for part in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
    return _CRED_URL_RE.sub(lambda m: f"{m.group('scheme')}***@", text)


class SnapshotError(WorkflowError):
    """Building a git-ref snapshot failed (bad ref, git error, oversize, unsafe tar)."""


def _require_safe_extraction() -> None:
    """Refuse to extract unless the hardened stdlib tar filter is available.

    ``tarfile.data_filter`` (and the ``extractall(filter=…)`` keyword) were added
    in CPython 3.12 and backported to 3.11.4 — but ``requires-python`` is ``>=3.11``,
    so a 3.11.0–3.11.3 interpreter would otherwise fall through to an UNFILTERED
    ``extractall`` (CVE-2007-4559 path traversal). Fail closed with a clear message
    instead of extracting a git archive unsafely.
    """
    if not hasattr(tarfile, "data_filter"):  # pragma: no cover - depends on runtime
        raise SnapshotError(
            "this Python lacks tarfile.data_filter (the hardened tar-extraction "
            "filter added in 3.11.4 / 3.12); refusing to extract a snapshot "
            "unsafely — upgrade to Python >= 3.11.4"
        )


def _snapshot_root(repo_root: str | None) -> Path:
    # Mirror executor.snapshot_root without importing it (avoid a cycle); the WS-C3
    # sweep GCs this same directory.
    base = Path(repo_root) if repo_root else Path.cwd()
    return base / ".rebar" / "run_snapshots"


def resolve_sha(ref: str, repo_root: str | None = None) -> str:
    """Resolve ``ref`` to a full commit SHA (``<ref>^{commit}``).

    Pins the snapshot to an immutable commit object, never a moving branch/tag.
    Raises :class:`SnapshotError` if the ref doesn't resolve to a commit."""
    root = str(repo_root) if repo_root else "."
    proc = subprocess.run(
        ["git", "-C", root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise SnapshotError(
            f"cannot resolve git ref {ref!r} to a commit: {proc.stderr.strip() or 'no such ref'}"
        )
    return sha


def _hardened_filter(max_bytes: int, max_files: int = DEFAULT_MAX_SNAPSHOT_FILES):
    """A tarfile extraction filter: the stdlib ``data`` filter (rejects absolute
    paths, ``..`` escapes, and escaping links) plus cumulative size + count guards."""
    seen = {"total": 0, "count": 0}

    def _filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo | None:
        # data_filter raises on absolute paths / .. traversal / unsafe links.
        member = tarfile.data_filter(member, dest_path)
        seen["count"] += 1
        if seen["count"] > max_files:
            raise SnapshotError(f"snapshot exceeds the {max_files}-file cap; refusing to continue")
        seen["total"] += max(0, member.size)
        if seen["total"] > max_bytes:
            raise SnapshotError(
                f"snapshot exceeds the {max_bytes}-byte cap (extracted "
                f"{seen['total']} bytes); refusing to continue"
            )
        return member

    return _filter


def _chmod_readonly(root: Path) -> None:
    """Make the extracted tree read-only (files r-x/r--, dirs r-x) so a read-only
    step cannot mutate the snapshot."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                os.chmod(p, 0o444)
            except OSError:
                pass
        for name in dirnames:
            p = Path(dirpath) / name
            try:
                os.chmod(p, 0o555)
            except OSError:
                pass
    try:
        os.chmod(root, 0o555)
    except OSError:
        pass


def _rmtree_writable(path: Path) -> None:
    # The tree is chmod'd read-only; restore write bits so rmtree can remove it.
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                os.chmod(Path(dirpath) / name, 0o700)
            except OSError:
                pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _reap(proc: subprocess.Popen) -> None:
    """Kill (if running), wait for, and close the pipes of ``proc``.

    Never leaves an orphan or a leaked pipe FD, and never lets a ``TimeoutExpired`` from
    its own ``wait`` escape — during cleanup there is nothing actionable left to do, and
    that exception's ``cmd`` is exactly the credential-bearing payload of bug 77e1.
    """
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:  # pragma: no cover - the child raced us to exit
            pass
    try:
        proc.wait(timeout=_REAP_TIMEOUT)
    except Exception:  # noqa: BLE001 — best-effort child reap during cleanup
        pass
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:  # noqa: BLE001 — best-effort pipe close during cleanup
            pass


def _drain(stream, sink: list[bytes] | None = None, *, cap: int = _DRAIN_CHUNK * 16) -> None:
    """Read ``stream`` to EOF in bounded chunks, keeping at most ``cap`` bytes in ``sink``.

    Run against stderr on its own thread: the pre-093a code left ``stderr=PIPE`` undrained
    while it consumed stdout, so a child writing past the ~64 KiB pipe buffer blocked on
    its stderr write, therefore stopped writing stdout, and the parent blocked forever in
    ``tarfile.__read``. Draining concurrently is the only fix for that shape.
    """
    kept = 0
    try:
        while True:
            chunk = stream.read(_DRAIN_CHUNK)
            if not chunk:
                return
            if sink is not None and kept < cap:
                sink.append(chunk[: cap - kept])
                kept += len(chunk)
    except (OSError, ValueError):  # pipe closed under us by the watchdog kill
        return


def _stream_archive(root: str, sha: str, tmp: Path, max_bytes: int) -> None:
    """Stream ``git archive <sha>`` into ``tmp`` — bounded, drained, and prompt-free.

    Three properties the naive ``Popen`` + ``tarfile`` pairing did not have (bug 093a):
    stderr is drained on its own thread so it cannot deadlock the stdout reader; a
    watchdog kills the child at :data:`_GIT_TIMEOUT` so the extractor cannot park forever
    on dead air; and stdin is ``DEVNULL`` so no credential/host-key prompt can block here.
    """
    argv = ["git", "-C", root, *stall_abort_args(), "archive", "--format=tar", sha]
    proc = subprocess.Popen(  # raw-git-ok: read-only `git archive`; never mutates a repo
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    captured: list[bytes] = []
    drainer = threading.Thread(target=_drain, args=(proc.stderr, captured), daemon=True)
    drainer.start()
    timed_out = threading.Event()

    def _abort() -> None:
        timed_out.set()
        if proc.poll() is None:
            try:
                proc.kill()  # closes stdout, which unblocks the tarfile read
            except OSError:  # pragma: no cover - the child raced us to exit
                pass

    watchdog = threading.Timer(_GIT_TIMEOUT, _abort)
    watchdog.start()
    try:
        _extract_stream(proc, tmp, max_bytes, timed_out, argv, captured)
    finally:
        watchdog.cancel()
        _reap(proc)
        drainer.join(timeout=_REAP_TIMEOUT)


def _extract_stream(
    proc: subprocess.Popen,
    tmp: Path,
    max_bytes: int,
    timed_out: threading.Event,
    argv: list[str],
    captured: list[bytes],
) -> None:
    """Consume ``proc``'s tar on stdout into ``tmp``, converting every failure shape."""

    def _fail(detail: str) -> SnapshotError:
        if timed_out.is_set():
            return SnapshotError(
                f"git archive timed out after {_GIT_TIMEOUT} seconds and was "
                f"terminated: {_redact_cmd(argv)}"
            )
        stderr = b"".join(captured).decode("utf-8", "replace").strip()
        return SnapshotError(f"git archive failed: {stderr or detail}")

    try:
        # Stream the archive (mode "r|") so a large repo isn't buffered whole.
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            tar.extractall(path=str(tmp), filter=_hardened_filter(max_bytes))
        _drain(proc.stdout)  # trailing padding, so the child is never blocked on write
        rc = proc.wait(timeout=_GIT_TIMEOUT)
    except SnapshotError:
        raise  # the hardened filter's own caps — already this module's vocabulary
    except subprocess.TimeoutExpired as exc:
        # Neither an OSError nor a CalledProcessError, so it would otherwise escape every
        # caller's except tuple carrying a `user:token@host` cmd (bug 77e1).
        raise SnapshotError(
            f"git archive timed out after {_GIT_TIMEOUT} seconds: {_redact_cmd(exc.cmd)}"
        ) from exc
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise _fail(f"{type(exc).__name__}: {_redact_cmd(str(exc))}") from exc
    if rc != 0:
        raise _fail(f"exit status {rc}")


def snapshot_at_ref(
    ref: str,
    repo_root: str | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
) -> Path:
    """Return a read-only snapshot directory of the repo at ``ref`` (cached by SHA).

    Resolves ``ref`` to a SHA, and if ``.rebar/run_snapshots/<sha>`` already exists
    returns it (cache hit). Otherwise streams ``git archive <sha>`` through the
    hardened extractor into a temp dir, makes it read-only, and atomically renames
    it into place. Raises :class:`SnapshotError` on any failure (the partial temp
    dir is cleaned up). The caller never tears the snapshot down — the WS-C3 TTL
    sweep does — so re-runs at the same SHA are free.
    """
    sha = resolve_sha(ref, repo_root)
    root = str(repo_root) if repo_root else "."
    dest = _snapshot_root(repo_root) / sha
    if dest.is_dir():
        return dest  # cache hit (immutable by SHA)

    _require_safe_extraction()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-snap-{sha[:8]}-", dir=str(dest.parent)))
    try:
        _stream_archive(root, sha, tmp, max_bytes)
        # Atomic publish FIRST, THEN make the published tree read-only. On macOS/BSD,
        # renaming a directory requires write permission on the directory itself (to
        # update its ``..`` entry), so chmod-readonly-before-rename raises EACCES there;
        # rename-then-chmod is portable. If another run won the race, keep theirs and
        # drop ours (ours is still writable here, so the cleanup succeeds).
        try:
            os.rename(tmp, dest)
        except OSError:
            if dest.is_dir():
                _rmtree_writable(tmp)
                return dest
            raise
        _chmod_readonly(dest)
        return dest
    except BaseException:
        # ``_stream_archive`` owns the child and always reaps it; only the partial temp
        # tree is this frame's to clean up.
        if tmp.exists():
            _rmtree_writable(tmp)
        raise
