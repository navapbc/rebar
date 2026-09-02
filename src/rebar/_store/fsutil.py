"""Atomic file writes: temp-in-same-dir + ``os.replace`` (crash-atomic on one filesystem).

Leaf module — **stdlib only, NO ``rebar.*`` imports** — so any layer can depend on it
without an import cycle. It consolidates the many inline "write a temp file, then
rename it over the target" sites (the HLC cache, ``rebar.toml``, the ticket-event
staging in ``txn``/``compact``, the reducer/graph caches, the snapshot sidecars, prompt
authoring, agent scratch) behind one call, so the crash-atomicity contract lives in
exactly one place.

Guarantees
----------
* **Crash-atomic on the same filesystem.** The temp is created (via ``mkstemp`` — a
  unique, ``O_EXCL`` name) in the SAME directory as ``path`` and published with
  ``os.replace``, an atomic rename on one filesystem — so a concurrent reader / the
  replayer never observes a torn or partial ``path``: it sees either the old file or
  the new one, whole. A failure before the rename leaves ``path`` untouched and the
  temp is removed.
* **Text or bytes.** ``mode="w"`` writes ``str`` (encoded with ``encoding``, and with
  newline translation DISABLED so the on-disk bytes equal ``data`` exactly, even on
  Windows); ``mode="wb"`` writes ``bytes`` verbatim.
* **Durability (opt-in).** ``fsync=True`` fsyncs the file before the rename AND the
  containing directory after it, so both the data and the rename survive a power loss
  (this is what the agent-scratch writer needs). Default OFF — the crash-ATOMICITY
  above holds without it, and the event log / HLC cache never paid for an fsync.
* **Permissions.** The published file's mode is ``permissions`` when given, else the
  umask-derived mode a plain ``open(path, "w")`` would yield (``mkstemp``'s 0o600 is
  overridden, so a migrated ``open``-based site keeps its usual 0o644).

The parent directory must already exist (callers that need it created still do so).

Why the temp name must NOT come from the target (ticket b0ac-3c0f-3f64-4344)
---------------------------------------------------------------------------
A temp whose name is derived from the TARGET (``<target>.tmp``) — or from the PID
(``<target>.<pid>.tmp``, unique across processes but SHARED by every thread of one
process, and the MCP server is threaded) — is the SAME pathname for every concurrent
writer of that target. The first ``os.replace`` consumes it; the second raises
``FileNotFoundError``, which a best-effort ``except`` swallows, and that writer is
**silently lost**. ``mkstemp`` gives each writer its own ``O_EXCL`` name, so both land.
The audited sites, all now routed here:

===================================================  ============================
Site                                                 Was
===================================================  ============================
``llm/workflow/completion_verdict_cache``            target-derived (fixed 89981d8e)
``review_bot/reconcile._write_cursor``               target-derived ``.tmp``
``_config_writer.write_jira_config``                 target-derived ``.tmp``
``_cli._suggest_mapping_write``                      target-derived ``.tmp``
``_opcert_signing._derive_opcert_pub``               pid-derived ``.tmp``
``_snapshot/janitor.reverify_entry``                 no temp (torn reads)
``llm/workflow/completion_banking`` (bank upsert)    no temp (torn reads)
===================================================  ============================

The audit's negative result is worth keeping: every module-level cache and
``functools.lru_cache`` under ``src/rebar`` was checked and **none is root-unkeyed** —
they key on an absolute path, the repo root, or a stat token, and the two store-state
``lru_cache``\\ s are explicitly repo-root + overlay-signature keyed. So the defect class
is confined to temp-file NAMING; no cache needed re-keying.
"""

from __future__ import annotations

import os
import tempfile

__all__ = ["atomic_write"]


def _umask_mode() -> int:
    """The mode a fresh ``open(path, "w")`` produces: ``0o666 & ~umask``. There is no
    ``os.getumask``; the read-and-restore dance is the standard idiom."""
    m = os.umask(0)
    os.umask(m)
    return 0o666 & ~m


def atomic_write(
    path: str | os.PathLike[str],
    data: str | bytes,
    *,
    mode: str = "w",
    encoding: str = "utf-8",
    fsync: bool = False,
    permissions: int | None = None,
) -> None:
    """Atomically write ``data`` to ``path`` (temp-in-same-dir + ``os.replace``).

    ``mode`` is ``"w"`` (text — ``data`` must be ``str``) or ``"wb"`` (bytes — ``data``
    must be ``bytes``). ``encoding`` applies to text. ``fsync=True`` opts into the
    file+dir fsync durability guarantee. ``permissions`` sets the final file mode
    (default: the umask-derived mode ``open`` would give).

    Raises the underlying ``OSError`` on failure (after removing the temp); callers
    that treat the write as best-effort keep their own ``try/except`` around the call,
    exactly as before.
    """
    path = os.fspath(path)
    binary = "b" in mode
    if binary and not isinstance(data, (bytes, bytearray)):
        raise TypeError("atomic_write(mode='wb') requires bytes data")
    if not binary and not isinstance(data, str):
        raise TypeError("atomic_write(mode='w') requires str data")

    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{base}.", suffix=".tmp")
    try:
        if binary:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)  # type: ignore[arg-type]
                if fsync:
                    fh.flush()
                    os.fsync(fh.fileno())
        else:
            # newline="" — no newline translation, so the on-disk bytes equal `data`.
            with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
                fh.write(data)  # type: ignore[arg-type]
                if fsync:
                    fh.flush()
                    os.fsync(fh.fileno())
        os.chmod(tmp, permissions if permissions is not None else _umask_mode())
        os.replace(tmp, path)  # atomic on the same filesystem (same dir = same fs)
    except BaseException:
        # The publish never happened → drop the temp and re-raise (incl. Keyboard-
        # Interrupt / SystemExit): the target is left untouched, never half-written.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if fsync:
        _fsync_dir(directory)


def _fsync_dir(directory: str) -> None:
    """fsync a directory so a just-published rename is itself durable (best-effort —
    some platforms disallow opening a directory for fsync)."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
