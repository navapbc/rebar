#!/usr/bin/env python3
"""Amend HEAD's commit message from a file WITHOUT dropping its ``Change-Id``.

Run it as ``make amend-msg FILE=<path>``.

WHY THIS EXISTS
---------------
``git commit --amend -F <file>`` (and ``-m``) REPLACES the whole commit message, so the
``Change-Id`` trailer goes with it. Gerrit's ``commit-msg`` hook only ever ADDS a
``Change-Id`` when none is present, so it then stamps a FRESH one — and the next
``git push gerrit HEAD:refs/for/main`` opens a SECOND Gerrit change instead of adding a
patchset to the existing one. The original is orphaned and has to be abandoned by hand.
It happened three times in a single session (Gerrit 1921, 1926, 1931).

Nothing downstream can catch it. ``git commit --amend -F <file>`` hands
``prepare-commit-msg`` ``source='message'`` with an EMPTY sha — byte-identical to what a
fresh ``-F`` commit produces — so a hook cannot tell "amending, keep the Change-Id" from
"new commit, stamp one" (measured, not assumed). Neither can the server: a commit
carrying a fresh Change-Id is indistinguishable from a legitimate new change.

So this follows the Gerrit ecosystem's actual remedy — Go's ``git codereview change``,
OpenStack's ``git-review``, Android's ``repo upload``, Chromium's ``git cl upload`` —
which make the failure UNREACHABLE by wrapping the workflow rather than DETECTABLE by
adding a guard. Read HEAD's ``Change-Id``, put it into the new message, amend.

BEHAVIOUR
---------
* HEAD carries no ``Change-Id`` -> refuse, loudly and non-zero. A missing trailer means
  the Gerrit ``commit-msg`` hook is not installed, which is its own problem (``make
  hooks``); amending anyway would just hand the freshly-stamped-id failure right back.
* FILE carries its own ``Change-Id`` -> dropped, and HEAD's carried forward instead.
  Gerrit rejects a commit with two ``Change-Id`` lines, and the one that matters is the
  one already published on the change.
* HEAD carries ``Signed-off-by`` trailer(s) and FILE omits them -> carried forward, all
  of them, in order. ``--file`` replaces the message wholesale, so without this the DCO
  attestation would be lost as silently as the Change-Id — the very failure this wrapper
  exists to prevent. A FILE that supplies its own sign-off is authoritative: nothing is
  carried and nothing duplicated. A HEAD without one gets none invented.
* Everything else is plain ``git commit --amend``: hooks are NOT bypassed (no
  ``--no-verify``, so the commit-message gates still fire), and anything staged is
  folded into the amended commit exactly as ``git commit --amend --no-edit`` would.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

_TIMEOUT = 120

# Gerrit's own hook matches ``^Change-Id: I[0-9a-f]{40}$``. This is deliberately looser
# (any token, any case) so a hand-written or malformed trailer is still recognised as
# one rather than being silently duplicated by the composition below.
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


def extract_change_id(message: str) -> str | None:
    """Return the last ``Change-Id`` value in *message*, or ``None`` if it carries none.

    The last one wins for the same reason Gerrit reads it that way: when a message has
    somehow accumulated more than one, the trailing trailer block is the authoritative
    footer.
    """
    found = [m.group(1) for line in message.splitlines() if (m := _CHANGE_ID_LINE.match(line))]
    return found[-1] if found else None


def strip_change_id_lines(message: str) -> str:
    """Return *message* with every ``Change-Id:`` trailer line removed."""
    kept = [line for line in message.splitlines() if not _CHANGE_ID_LINE.match(line)]
    return "\n".join(kept) + "\n"


# The subcommand is a caller-supplied parameter, so the raw-git-write lint resolves this
# argv as opaque and fires fail-closed. Every caller in this module passes a READ (`log`,
# `interpret-trailers`), and the target is the code checkout, never the ticket store.
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
    """Return every ``Signed-off-by`` value in *message*, in order, possibly empty.

    Unlike ``Change-Id`` — where only the last one is authoritative — multiple sign-offs
    are legitimate (each is a distinct DCO attestation), so all of them are kept.
    """
    return [m.group(1) for line in message.splitlines() if (m := _SIGN_OFF_LINE.match(line))]


def compose_message(new_message: str, change_id: str, sign_offs: Sequence[str] = ()) -> str:
    """Return *new_message* carrying *change_id* and, when it omits one, *sign_offs*.

    ``Change-Id`` handling is unchanged: any in *new_message* is stripped and HEAD's
    re-attached as the sole trailer. *sign_offs* (HEAD's, in order) are appended only
    when *new_message* itself carries no ``Signed-off-by`` — a file that supplies its
    own is authoritative, so nothing is carried and nothing duplicated.

    Placement is delegated to ``git interpret-trailers`` rather than reimplemented: it
    already knows whether the final paragraph is a trailer block (append into it, next to
    ``Signed-off-by``) or prose (open a new one).
    """
    stripped = strip_change_id_lines(new_message)
    trailers: list[str] = []
    if not extract_sign_offs(stripped):
        trailers.extend(f"Signed-off-by: {sign_off}" for sign_off in sign_offs)
    trailers.append(f"Change-Id: {change_id}")
    args: list[str] = []
    for trailer in trailers:
        args.extend(("--trailer", trailer))
    return _git_stdout("interpret-trailers", *args, stdin=stripped)


# This IS the sanctioned amend path: the whole point of the wrapper is that a raw
# `git commit --amend -F` drops the Change-Id, so the remedy has to OWN the amend rather
# than forbid it. Ticket-store writes are unaffected -- they stay on the rebar seams.
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
