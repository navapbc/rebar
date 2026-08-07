#!/usr/bin/env python3
"""Shared bug-filer dedup primitives for rebar's automated CI alert lanes.

Extracted from ``scripts/canary_bridge.py`` (ticket 63e8-9235-220f-4201) so a SECOND
scheduled filer — the dependency-advisory lane — reuses the canary's dedup instead of
growing a parallel one. The canary's helpers were **not reusable as-is**: they were
module-private and ``_recent_marker_comment`` hard-coded the canary's own
``BRIDGE_CANARY_ALERT:`` marker, so a second lane sharing them would have collapsed two
independent alert streams onto one marker. The fix is this extraction with the marker
lifted to a parameter; ``canary_bridge`` now delegates here and its behaviour is
unchanged (its own oracle suite still pins it).

The contract both filers share (docs/bug-creation-contract.md, ticket 4527):

* **dedup search first** — one OPEN bug per alert TAG, ever. A lane that stays red for
  weeks updates that one ticket; it never files a second.
* **capped accumulation** — at most one marker comment per 24h, so a long-red lane
  cannot flood the ticket it already filed.
* **fail-soft finds** — a ``rebar list``/``show`` error reports "no existing alert" /
  "no recent marker" rather than raising: a duplicate comment is cheaper than a silent
  gap, and the next cycle re-converges.
"""

from __future__ import annotations

import json
from collections.abc import Callable

# (argv) -> (returncode, stdout, stderr) — the seam unit tests replace.
Runner = Callable[[list[str]], tuple[int, str, str]]

#: Accumulation cap: at most one marker comment per alert ticket per 24h.
ACCUMULATION_WINDOW_SECS = 24 * 3600


def find_alert_ticket(runner: Runner, tag: str) -> str:
    """Return the id of the single OPEN bug carrying ``tag``, or ``''``.

    This is the dedup key: an alert lane files at most one ticket per tag and finds it
    again on every subsequent cycle. Fail-soft — any error or unparseable output returns
    ``''`` ("none found").
    """
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
    """True if ``tid`` already carries a ``marker`` comment younger than ``window_secs``.

    ``marker`` is per-lane (the canary's ``BRIDGE_CANARY_ALERT:`` vs the dependency
    lane's own), so two lanes commenting on tickets never mute each other. Fail-soft:
    any show/parse error means "no recent marker" — comment anyway.
    """
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
        if not isinstance(ts, (int, float)):
            continue
        secs = ts / 1e9 if ts > 1e12 else ts  # store timestamps are ns
        if now_epoch - secs < window_secs:
            return True
    return False
