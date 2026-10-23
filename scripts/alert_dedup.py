#!/usr/bin/env python3
"""Shared dedup for automated CI alert lanes.

Both canary and dependency-advisory filers search before filing: each alert tag has
at most one open bug. A lane adds at most one marker comment per 24 hours; the
marker is a parameter so lanes stay independent. List/show failures return no
match rather than raising, allowing the next cycle to converge. See
docs/bug-creation-contract.md (ticket 4527)."""

from __future__ import annotations

import json
from collections.abc import Callable

# (argv) -> (returncode, stdout, stderr) — the seam unit tests replace.
Runner = Callable[[list[str]], tuple[int, str, str]]

#: Accumulation cap: at most one marker comment per alert ticket per 24h.
ACCUMULATION_WINDOW_SECS = 24 * 3600


def find_alert_ticket(runner: Runner, tag: str) -> str:
    """Return the one open bug ID for ``tag``, or '' on no match, list error, or parse error."""
    rc, stdout, _stderr = runner(
        ["rebar", "list", "--type=bug", "--status=open", f"--has-tag={tag}", "--output", "json"]
    )
    if rc != 0:
        return ""
    try:
        tickets = json.loads(stdout)
        return tickets[0]["ticket_id"] if tickets else ""
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return ""


def recent_marker_comment(
    runner: Runner,
    tid: str,
    marker: str,
    now_epoch: int,
    window_secs: int = ACCUMULATION_WINDOW_SECS,
) -> bool:
    """Return whether ``tid`` has this lane's ``marker`` within ``window_secs``; show/parse
    errors return False."""
    rc, stdout, _stderr = runner(["rebar", "show", tid, "--output", "json"])
    if rc != 0:
        return False
    try:
        data = json.loads(stdout[stdout.find("{") :])
        comments = data.get("comments") or []
    except (json.JSONDecodeError, ValueError, AttributeError):
        return False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if not str(comment.get("body", "")).startswith(marker):
            continue
        ts = comment.get("timestamp")
        if isinstance(ts, str):
            # JSON output carries nanosecond timestamps as exact decimal strings (bug e127).
            # Coerce before type checking or recent markers are missed and both alert
            # lanes can append a comment on every cycle.
            try:
                ts = int(ts)
            except ValueError:
                continue
        if not isinstance(ts, (int, float)):
            continue
        secs = ts / 1e9 if ts > 1e12 else ts  # store timestamps are ns
        if now_epoch - secs < window_secs:
            return True
    return False
