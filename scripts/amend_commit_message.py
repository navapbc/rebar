#!/usr/bin/env python3
"""Amend HEAD's message from a file while retaining its Gerrit Change-Id.

Run ``make amend-msg FILE=<path>``. Plain ``git commit --amend -F`` replaces the
message and can stamp a new Change-Id, opening a separate Gerrit change. This
wrapper refuses HEAD without a Change-Id (install hooks with ``make hooks``),
replaces any file-supplied Change-Id with HEAD's, and carries HEAD's ordered
sign-offs only when the file supplies none. Hooks still run, and staged changes
are included in the amended commit."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

_TIMEOUT = 120
_SHARED_SCRATCH_ROOTS = {
    root for path in (Path("/tmp"), Path("/var/tmp")) for root in (path, path.resolve(strict=False))
}

# Recognize even malformed or mixed-case Change-Id trailers so composition
# replaces them instead of duplicating them; Gerrit's hook is stricter.
_CHANGE_ID_LINE = re.compile(r"^Change-Id:\s*(\S+)\s*$", re.IGNORECASE)

# Deliberately loose for the same reason as ``_CHANGE_ID_LINE``: a hand-written or
# oddly-cased sign-off in FILE must still be recognised as one, or the carry-forward
# below would duplicate it.
_SIGN_OFF_LINE = re.compile(r"^Signed-off-by:\s*(.+\S)\s*$", re.IGNORECASE)

_NO_CHANGE_ID = """\
ERROR: HEAD carries no Change-Id trailer, so there is nothing to carry forward.

  Refusing to amend. A missing Change-Id means Gerrit's commit-msg hook is not
  installed in this checkout, so the amended commit would be stamped with a FRESH
  Change-Id and the next push would open a duplicate Gerrit change -- exactly the
  failure this wrapper exists to prevent.

  Install the hook, then re-create the commit so it gets stamped:

      make hooks
      git commit --amend --no-edit
"""


class _Failure(Exception):
    """A message printed to stderr before a non-zero exit."""


def _is_direct_shared_scratch_file(path: Path) -> bool:
    """Return whether *path* is a fixed file directly in a host-shared scratch root."""
    try:
        absolute = path.expanduser().resolve(strict=False)
    except RuntimeError:
        absolute = path.expanduser().absolute()
    return absolute.parent in _SHARED_SCRATCH_ROOTS


def extract_change_id(message: str) -> str | None:
    """Return the last Change-Id trailer, the authoritative footer value, or None."""
    found = [m.group(1) for line in message.splitlines() if (m := _CHANGE_ID_LINE.match(line))]
    return found[-1] if found else None


def strip_change_id_lines(message: str) -> str:
    """Return *message* with every ``Change-Id:`` trailer line removed."""
    kept = [line for line in message.splitlines() if not _CHANGE_ID_LINE.match(line)]
    return "\n".join(kept) + "\n"


# This parameter is opaque to the raw-git lint, but callers use read-only
# git log/interpret-trailers on the code checkout.
# raw-git-ok: read-only git helper (git log / interpret-trailers) on the code checkout
def _git_stdout(*args: str, stdin: str | None = None) -> str:
    """Run a READ-ONLY git command and return its stdout, or exit loudly on failure."""
    proc = subprocess.run(
        ["git", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT,
    )
    if proc.returncode != 0:
        raise _Failure(f"ERROR: `git {' '.join(args)}` failed:\n{proc.stderr.strip()}")
    return proc.stdout


def extract_sign_offs(message: str) -> list[str]:
    """Return all Signed-off-by trailer values in order; multiple DCO attestations are valid."""
    return [m.group(1) for line in message.splitlines() if (m := _SIGN_OFF_LINE.match(line))]


def compose_message(new_message: str, change_id: str, sign_offs: Sequence[str] = ()) -> str:
    """Replace file-supplied Change-Ids with HEAD's ``change_id``. Preserve ordered HEAD
    sign-offs only when the new message supplies none. Let ``git interpret-trailers`` place
    the trailers."""
    stripped = strip_change_id_lines(new_message)
    trailers: list[str] = []
    if not extract_sign_offs(stripped):
        trailers.extend(f"Signed-off-by: {sign_off}" for sign_off in sign_offs)
    trailers.append(f"Change-Id: {change_id}")
    args: list[str] = []
    for trailer in trailers:
        args.extend(("--trailer", trailer))
    return _git_stdout("interpret-trailers", *args, stdin=stripped)


# This wrapper owns the code-checkout amend so it retains Change-Id;
# ticket-store writes still use rebar's supported seams.
# raw-git-ok: amends the CODE checkout's own HEAD commit message, never the ticket store
def amend_head(message: str) -> int:
    """Amend HEAD with *message*. Returns git's exit status."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".commit-msg", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(message)
        tmp = Path(handle.name)
    try:
        proc = subprocess.run(
            ["git", "commit", "--amend", "--file", str(tmp)],
            check=False,
            timeout=_TIMEOUT,
        )
        return proc.returncode
    finally:
        tmp.unlink(missing_ok=True)


def _read_message_file(raw: str) -> str:
    path = Path(raw)
    if _is_direct_shared_scratch_file(path):
        raise _Failure(
            "ERROR: refusing commit-message file in shared scratch root: "
            f"{path}\n"
            "A shared scratch file must be under a per-session or per-invocation "
            "unique directory; a fixed file can be overwritten by another agent "
            "before git reads it."
        )
    if not path.is_file():
        raise _Failure(f"ERROR: no such commit-message file: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise _Failure(f"ERROR: {path} is empty -- refusing to amend to an empty message.")
    return text


def _run(message_file: str) -> int:
    new_message = _read_message_file(message_file)
    head_message = _git_stdout("log", "-1", "--format=%B")
    change_id = extract_change_id(head_message)
    if change_id is None:
        raise _Failure(_NO_CHANGE_ID.rstrip("\n"))
    return amend_head(compose_message(new_message, change_id, extract_sign_offs(head_message)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make amend-msg FILE=<path>",
        description=(
            "Amend HEAD with a rewritten commit message, carrying its Change-Id forward. "
            "Use this instead of `git commit --amend -F/-m`, which drops the Change-Id "
            "and opens a duplicate Gerrit change on the next push."
        ),
    )
    parser.add_argument("file", help="path to a file holding the new commit message")
    args = parser.parse_args(argv)
    try:
        return _run(args.file)
    except _Failure as failure:
        print(failure, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
