#!/usr/bin/env python3
"""Ticket-ID resolver helper for resolve_ticket_id (multi-mode).

Single-pass scanner used by ticket-lib.sh resolve_ticket_id.  Avoids the
fork-per-directory overhead of bash basename loops by doing all directory
iteration and string comparison inside one Python process.

Modes:
  --mode=alias   (default; backward-compatible positional invocation)
      For each ticket directory, reads its CREATE event (and the latest
      SNAPSHOT for compacted tickets) once and matches against the input by:
        - data.alias (stored, set at create time for new tickets)
        - data.alias backfilled by computing from ticket_id (legacy tickets)
        - data.jira_key
      Output (one line per match, tab-separated):
        alias\\t<ticket_dir_name>
        jira\\t<ticket_dir_name>

  --mode=8hex
      Scans ticket directories whose first 9 chars (xxxx-xxxx) match the
      input.  Used by the bash 8-hex resolution step.  Output: one full
      16-hex ticket dir name per line.

  --mode=prefix
      Scans ticket directories whose name starts with the input string.
      Used by the bash unique-prefix resolution step.  Output: one full
      16-hex ticket dir name per line.

Exit codes:
  0  Success.  stdout may be empty (no matches) or contain matched IDs.
     Caller distinguishes "exactly one match" vs "ambiguous" by counting.
  1  Invalid arguments OR unexpected I/O failure (stderr is populated).
     This is a HARD failure — must not be confused with "no match" by
     callers.  (Bug 19a3-03ca: silent OSError masquerading as a miss
     turned debuggable I/O into a mysterious lookup miss.)

Usage:
    ticket-alias-resolve.py <input> <tracker_dir>                  # alias mode
    ticket-alias-resolve.py --mode={alias|8hex|prefix} <input> <tracker_dir>
"""

from __future__ import annotations

import json
import os
import sys

# Single source of truth for alias computation lives in
# ticket_reducer/_alias.py. Importing it here keeps stored-at-create-time
# aliases and backfilled-at-resolve-time aliases in lock-step — the same
# wordlist, the same fallback rules, the same env var override.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from ticket_reducer._alias import compute_alias  # noqa: E402


def _parse_args(argv: list[str]) -> tuple[str, str, str] | None:
    """Returns (mode, target, tracker) or None on usage error."""
    args = list(argv[1:])
    mode = "alias"
    if args and args[0].startswith("--mode="):
        mode = args[0].split("=", 1)[1]
        args = args[1:]
    elif args and args[0] == "--mode":
        if len(args) < 2:
            return None
        mode = args[1]
        args = args[2:]
    if len(args) != 2:
        return None
    if mode not in ("alias", "8hex", "prefix"):
        print(
            f"ticket-alias-resolve: invalid --mode={mode!r}; "
            "expected alias|8hex|prefix",
            file=sys.stderr,
        )
        return None
    return mode, args[0], args[1]


def _list_tracker_dirs(tracker: str) -> list[str] | None:
    """Returns sorted directory entries, or None on OSError (after logging)."""
    try:
        return sorted(os.listdir(tracker))
    except OSError as exc:
        # Fail loud — silent OSError here looks identical to "no matches"
        # and turns a debuggable I/O failure into a mysterious lookup miss.
        print(f"ticket-alias-resolve: cannot list {tracker!r}: {exc}", file=sys.stderr)
        return None


def _run_short_scan(mode: str, target: str, tracker: str) -> int:
    """Handles --mode=8hex and --mode=prefix.

    Both modes do a single directory scan with a string comparison.  The
    bash caller still owns the ambiguous/not-found error formatting — this
    helper just emits matching directory names, one per line, on stdout.
    """
    entries = _list_tracker_dirs(tracker)
    if entries is None:
        return 1
    for name in entries:
        if name.startswith("."):
            continue
        full_path = os.path.join(tracker, name)
        # isdir() is a stat call per entry, but on macOS/Linux readdir
        # returns d_type for most filesystems so this is typically free
        # via the OS cache.  Required because find -L was filtering -type d.
        if not os.path.isdir(full_path):
            continue
        if mode == "8hex":
            if name[:9] == target:
                print(name)
        else:  # prefix
            if name.startswith(target):
                print(name)
    return 0


def main() -> int:
    parsed = _parse_args(sys.argv)
    if parsed is None:
        print(
            f"Usage: {sys.argv[0]} [--mode=alias|8hex|prefix] <input> <tracker_dir>",
            file=sys.stderr,
        )
        return 1
    mode, target, tracker = parsed

    if mode in ("8hex", "prefix"):
        return _run_short_scan(mode, target, tracker)

    # mode == "alias" — original behavior preserved verbatim below.
    entries = _list_tracker_dirs(tracker)
    if entries is None:
        return 1

    for name in entries:
        if name.startswith("."):
            continue
        ticket_dir = os.path.join(tracker, name)
        if not os.path.isdir(ticket_dir):
            continue
        # Find the first CREATE event (typically exactly one per ticket;
        # if multiple ever appear, the lexically earliest wins — same
        # ordering rule the rest of the reducer applies).
        # Also locate the lexically-latest SNAPSHOT event (excluding
        # PRECONDITIONS-SNAPSHOT) so that compacted tickets — where the
        # CREATE event has been folded into a SNAPSHOT — still expose the
        # authoritative alias/jira_key from compiled_state.  Bug
        # 9894-a463-090a-43e5: the wordlist evolves over time, so
        # compute_alias(ticket_id) can diverge from the alias that was
        # stored on the original CREATE event; for compacted tickets the
        # SNAPSHOT's compiled_state.alias is the authoritative value and
        # must take precedence over the backfill.
        create_path = None
        snapshot_path = None
        try:
            for fname in sorted(os.listdir(ticket_dir)):
                if fname.endswith("-CREATE.json") and create_path is None:
                    create_path = os.path.join(ticket_dir, fname)
                elif fname.endswith("-SNAPSHOT.json") and not fname.endswith(
                    "-PRECONDITIONS-SNAPSHOT.json"
                ):
                    # Track the lexically-latest SNAPSHOT (sorted() yields
                    # ascending order, so a later filename overwrites earlier).
                    snapshot_path = os.path.join(ticket_dir, fname)
        except OSError:
            continue
        stored_alias = ""
        jira_key = ""
        if create_path:
            try:
                with open(create_path, encoding="utf-8") as f:
                    data = json.load(f).get("data", {}) or {}
                stored_alias = data.get("alias") or ""
                jira_key = data.get("jira_key") or ""
            except (OSError, json.JSONDecodeError):
                pass
        # SNAPSHOT-only path: when no CREATE event provided an alias/jira_key
        # (either because the CREATE is absent — compacted ticket — or its
        # data.alias was empty), read compiled_state from the latest SNAPSHOT.
        # This MUST run before the compute_alias backfill so the wordlist
        # cannot override the authoritative stored value.
        if snapshot_path and (not stored_alias or not jira_key):
            try:
                with open(snapshot_path, encoding="utf-8") as f:
                    snap_data = json.load(f).get("data", {}) or {}
                snap_state = snap_data.get("compiled_state", {}) or {}
                if not stored_alias:
                    stored_alias = snap_state.get("alias") or ""
                if not jira_key:
                    jira_key = snap_state.get("jira_key") or ""
            except (OSError, json.JSONDecodeError):
                pass
        # jira_key match
        if jira_key and jira_key == target:
            print(f"jira\t{name}")
            continue
        # alias match — stored (CREATE or SNAPSHOT) or backfilled.  Backfill
        # only fires when no event has supplied a stored alias; once a
        # SNAPSHOT has recorded compiled_state.alias, that value is the
        # source of truth and compute_alias is irrelevant.
        effective_alias = stored_alias or compute_alias(name) or ""
        if effective_alias and effective_alias == target:
            print(f"alias\t{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
