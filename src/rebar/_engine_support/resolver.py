"""Shared Python ticket-ID resolver.

Mirrors ``ticket-lib.sh::resolve_ticket_id`` so Python
CLIs accept the same ID forms (full 16-hex, 8-hex short, alias, jira_key,
unique prefix >= 4 chars) as the bash dispatcher does.

Delegates alias/jira_key lookup to ``ticket-alias-resolve.py`` — the same
helper the bash resolver uses — so a stored ``data.alias`` or
``data.jira_key`` in any CREATE event is reachable from both sides without
drift.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from rebar._engine import engine_dir as _engine_dir

# The alias/jira_key resolver helper (``ticket-alias-resolve.py``) lives in the
# bundled engine dir, not next to this module (which moved to rebar._engine_support
# in the fare-rant-clasp repackage).
_SCRIPT_DIR = _engine_dir()

_FULL_ID_RE = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$")
_SHORT_ID_RE = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$")


def resolve_ticket_id(ticket_id: str, tracker_dir: str) -> str | None:
    """Return the canonical ticket directory name for ``ticket_id``, or None.

    Ambiguous matches and ticket-alias-resolve.py subprocess failures are
    surfaced via stderr to match the bash side's diagnostics; the function
    still returns None so callers can pick their own error vs graceful path.
    """
    # Fast path: if the input is already an exact, canonical ticket directory
    # name, use it directly. This avoids a per-call alias-resolver subprocess
    # for inputs that are already resolved (e.g. dependency-graph BFS over known
    # directory names), and is unambiguous — a directory matching the input name
    # exactly is that ticket.
    if os.path.isdir(os.path.join(tracker_dir, ticket_id)):
        return ticket_id

    if _FULL_ID_RE.match(ticket_id):
        return (
            ticket_id if os.path.isdir(os.path.join(tracker_dir, ticket_id)) else None
        )

    if _SHORT_ID_RE.match(ticket_id):
        if os.path.isdir(os.path.join(tracker_dir, ticket_id)):
            return ticket_id
        try:
            matches = [
                n
                for n in os.listdir(tracker_dir)
                if not n.startswith(".")
                and n[:9] == ticket_id
                and os.path.isdir(os.path.join(tracker_dir, n))
            ]
        except OSError:
            return None
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(
                f"Error: Ambiguous 8-hex ID '{ticket_id}' matches: {' '.join(sorted(matches))}",
                file=sys.stderr,
            )
        return None

    resolver = _SCRIPT_DIR / "ticket-alias-resolve.py"
    if resolver.is_file():
        try:
            result = subprocess.run(
                [sys.executable, str(resolver), ticket_id, tracker_dir],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            print(
                f"Error: alias resolver invocation failed for '{ticket_id}': {exc}",
                file=sys.stderr,
            )
            result = None
        if result is not None and result.returncode != 0:
            # Mirror ticket-lib.sh resolve_ticket_id: a non-zero exit from the
            # helper is a hard failure rather than a silent "no match", so
            # I/O failures don't masquerade as lookup misses.
            stderr_tail = (result.stderr or "").strip()
            print(
                f"Error: alias resolver exited {result.returncode} for input '{ticket_id}'"
                + (f": {stderr_tail}" if stderr_tail else ""),
                file=sys.stderr,
            )
            return None
        if result is not None and result.returncode == 0:
            alias_matches: list[str] = []
            jira_matches: list[str] = []
            for line in result.stdout.splitlines():
                if "\t" in line:
                    kind, tid = line.split("\t", 1)
                    if kind == "jira":
                        jira_matches.append(tid)
                    elif kind == "alias":
                        alias_matches.append(tid)
            if len(jira_matches) == 1:
                return jira_matches[0]
            if len(jira_matches) > 1:
                print(
                    f"Error: Ambiguous jira_key '{ticket_id}' matches multiple tickets: "
                    f"{' '.join(sorted(jira_matches))}",
                    file=sys.stderr,
                )
                return None
            if len(alias_matches) == 1:
                return alias_matches[0]
            if len(alias_matches) > 1:
                print(
                    f"Error: Ambiguous alias '{ticket_id}' matches multiple tickets: "
                    f"{' '.join(sorted(alias_matches))}",
                    file=sys.stderr,
                )
                return None

    if len(ticket_id) >= 4:
        try:
            matches = [
                n
                for n in os.listdir(tracker_dir)
                if not n.startswith(".")
                and n.startswith(ticket_id)
                and os.path.isdir(os.path.join(tracker_dir, n))
            ]
        except OSError:
            return None
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(
                f"Error: Ambiguous prefix '{ticket_id}' matches multiple tickets: "
                f"{' '.join(sorted(matches))}",
                file=sys.stderr,
            )

    return None
