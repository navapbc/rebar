"""Long-lived git processes for the reclaim collapse (story silk-unpleasant-jackal).

The collapse writes thousands of commits over a tree of about 221k files. Starting a
``git fast-import`` per commit and a ``git cat-file`` per blob made a real-size store
impossible to rewrite. This module holds the two long-lived processes that replace them:

* :class:`FastImportSession` streams every commit into one ``git fast-import`` and reads
  each new commit's SHA back mid-stream with ``get-mark`` over ``--cat-blob-fd``. The
  collapse needs real SHAs while it is still writing, because later SNAPSHOT ledger
  rewrites embed them as text.
* :class:`ObjectReader` serves raw objects from one ``git cat-file --batch`` process,
  caching each object by id.

Both run only inside the shadow clone the collapse has already safety-checked.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path


class StreamError(RuntimeError):
    """A long-lived git process failed; the collapse turns this into its own error."""


class FastImportSession:
    """One ``git fast-import`` process that returns each commit's SHA as it is written."""

    # raw-git-ok: writes objects and a scratch ref only in the safety-checked shadow clone
    def __init__(self, tracker: Path) -> None:
        read_fd, write_fd = os.pipe()
        try:
            self._proc = subprocess.Popen(
                [
                    "git",
                    "-C",
                    str(tracker),
                    "fast-import",
                    "--quiet",
                    "--date-format=raw",
                    f"--cat-blob-fd={write_fd}",
                ],
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(write_fd,),
            )
        finally:
            os.close(write_fd)
        self._marks = read_fd
        self._next_mark = 1
        self._mark_of: dict[str, int] = {}
        self.commits_written = 0

    def commit(
        self,
        ref: str,
        *,
        author: bytes,
        committer: bytes,
        message: bytes,
        parents: Sequence[str],
        lines: Sequence[bytes],
    ) -> str:
        """Write one commit and return its SHA.

        ``author``/``committer`` are raw identity lines without the keyword
        (``Name <email> <epoch> <tz>``). ``lines`` are file commands (``M``/``D``).
        """
        mark = self._next_mark
        self._next_mark += 1
        chunks: list[bytes] = [
            f"commit {ref}\nmark :{mark}\n".encode(),
            b"author " + author + b"\n",
            b"committer " + committer + b"\n",
            f"data {len(message)}\n".encode(),
            message,
        ]
        if not message.endswith(b"\n"):
            chunks.append(b"\n")
        if parents:
            chunks.append(f"from {self._ref(parents[0])}\n".encode())
            chunks.extend(f"merge {self._ref(parent)}\n".encode() for parent in parents[1:])
        chunks.extend(lines)
        chunks.append(f"\nget-mark :{mark}\n".encode())
        self._send(b"".join(chunks))
        self.commits_written += 1
        sha = self._read_sha()
        self._mark_of[sha] = mark
        return sha

    def _ref(self, sha: str) -> str:
        """A commit written in this stream is named by mark: fast-import cannot look up
        its own not-yet-packed commits by SHA."""
        mark = self._mark_of.get(sha)
        return f":{mark}" if mark is not None else sha

    def close(self) -> None:
        """Finish the stream; raise :class:`StreamError` if fast-import failed."""
        try:
            if self._proc.stdin is not None and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except BrokenPipeError:
            pass
        stderr = self._proc.stderr.read() if self._proc.stderr is not None else b""
        code = self._proc.wait()
        os.close(self._marks)
        if code != 0:
            raise StreamError(f"git fast-import failed: {stderr.decode('utf-8', 'replace')}")

    def abort(self) -> None:
        """Stop the process without checking its result (used on error paths)."""
        try:
            if self._proc.stdin is not None and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except BrokenPipeError:
            pass
        self._proc.kill()
        self._proc.wait()
        try:
            os.close(self._marks)
        except OSError:
            pass

    def _send(self, data: bytes) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            raise StreamError("git fast-import has no stdin")
        try:
            stdin.write(data)
            stdin.flush()
        except BrokenPipeError as err:
            raise StreamError(self._failure("git fast-import stopped accepting input")) from err

    def _read_sha(self) -> str:
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = os.read(self._marks, 41 - len(buf))
            if not chunk:
                raise StreamError(self._failure("git fast-import closed before returning a mark"))
            buf += chunk
        sha = buf.strip().decode()
        if len(sha) != 40:
            raise StreamError(f"git fast-import returned an unexpected mark: {sha!r}")
        return sha

    def _failure(self, summary: str) -> str:
        self._proc.wait()
        stderr = self._proc.stderr.read() if self._proc.stderr is not None else b""
        return f"{summary}: {stderr.decode('utf-8', 'replace')}"


class ObjectReader:
    """Raw objects from one ``git cat-file --batch`` process, cached by object id."""

    def __init__(self, tracker: Path) -> None:
        self._proc = subprocess.Popen(
            ["git", "-C", str(tracker), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self._cache: dict[str, bytes] = {}

    def read(self, oid: str) -> bytes:
        cached = self._cache.get(oid)
        if cached is not None:
            return cached
        stdin, stdout = self._proc.stdin, self._proc.stdout
        if stdin is None or stdout is None:
            raise StreamError("git cat-file --batch has no pipes")
        stdin.write(oid.encode() + b"\n")
        stdin.flush()
        header = stdout.readline()
        parts = header.split()
        if len(parts) != 3:
            raise StreamError(f"git cat-file --batch could not read {oid}: {header!r}")
        size = int(parts[2])
        body = stdout.read(size)
        stdout.read(1)  # trailing newline
        self._cache[oid] = body
        return body

    def close(self) -> None:
        if self._proc.stdin is not None:
            self._proc.stdin.close()
        self._proc.wait()
