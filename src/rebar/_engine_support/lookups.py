"""In-process ``exists`` / ``resolve`` / ``format`` (Tier E E2).

Ports the three resolution/display arms the dispatcher reached via
``ticket-exists.sh`` (exists) and ``ticket-lib.sh`` ``resolve_ticket_id`` /
``format_ticket_id`` (resolve / format). All three reuse the shared resolver; the
library never exposed them, so this is CLI-only, byte-parity with the dispatcher.
"""

from __future__ import annotations

import glob
import json
import os
import sys

from rebar._engine_support.resolver import resolve_ticket_id


def _has_ticket_events(ticket_dir: str) -> bool:
    """True when ``ticket_dir`` is a directory with a CREATE or SNAPSHOT event."""
    if not os.path.isdir(ticket_dir):
        return False
    return bool(
        glob.glob(os.path.join(ticket_dir, "*-CREATE.json"))
        or glob.glob(os.path.join(ticket_dir, "*-SNAPSHOT.json"))
    )


def _create_data(ticket_dir: str) -> dict:
    """``data`` of the first CREATE event in ``ticket_dir`` ({} if none/unreadable).

    Matches the dispatcher's format/auto/alias paths, which read ``data.alias`` /
    ``data.jira_key`` from the CREATE event directly (not the reduced state, so a
    bridge-added jira_key post-CREATE is intentionally not reflected here).
    """
    patterns = ("*-CREATE.json", "CREATE-*.json")
    for pat in patterns:
        for path in sorted(glob.glob(os.path.join(ticket_dir, pat))):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh).get("data", {}) or {}
            except (OSError, json.JSONDecodeError):
                continue
    return {}


def _short_prefix(ticket_id: str, tracker: str) -> str:
    """Shortest unambiguous dash-stripped prefix (min 4 chars), else ``ticket_id``."""
    nodash = ticket_id.replace("-", "")
    try:
        bases = [
            os.path.basename(e).replace("-", "")
            for e in glob.glob(os.path.join(tracker, "*"))
            if os.path.isdir(e) and not os.path.basename(e).startswith(".")
        ]
    except OSError:
        bases = []
    for plen in range(4, len(nodash) + 1):
        candidate = nodash[:plen]
        if sum(1 for b in bases if b.startswith(candidate)) == 1:
            return candidate
    return ticket_id


def _display_mode(repo_root: str | None) -> str:
    """Resolve ``ticket.display_mode`` through the unified typed config (all layers),
    default ``auto``. Display is non-critical, so an unreadable config degrades to
    ``auto`` rather than failing. (The legacy WORKFLOW_CONFIG_FILE pointer is gone —
    config discovery is now $REBAR_CONFIG / rebar.toml / pyproject / legacy conf.)"""
    from rebar.config import ConfigError, load_config

    try:
        return load_config(repo_root).ticket.display_mode
    except ConfigError:
        return "auto"


# ── CLI arms (byte-parity with the dispatcher) ────────────────────────────────
def exists_cli(argv: list[str], tracker: str) -> int:
    if not argv or not argv[0]:
        sys.stderr.write("Usage: ticket exists <ticket_id>\n")
        return 1
    raw = argv[0]
    if _has_ticket_events(os.path.join(tracker, raw)):  # fast exact-dir path
        return 0
    resolved = resolve_ticket_id(raw, tracker)
    if not resolved:
        return 1
    return 0 if _has_ticket_events(os.path.join(tracker, resolved)) else 1


def resolve_cli(argv: list[str], tracker: str) -> int:
    if len(argv) < 1:
        sys.stderr.write("Usage: ticket resolve <id_or_alias_or_prefix>\n")
        return 1
    resolved = resolve_ticket_id(argv[0], tracker)
    if not resolved:  # None or "" (the resolver returns "" for an empty input)
        sys.stderr.write(f"Error: ticket '{argv[0]}' not found\n")
        return 1
    sys.stdout.write(resolved + "\n")
    return 0


def format_cli(argv: list[str], tracker: str, repo_root: str | None) -> int:
    if len(argv) < 1:
        sys.stderr.write("Usage: ticket format <ticket_id> [mode]\n")
        return 1
    ticket_id = argv[0]
    mode = argv[1] if len(argv) > 1 else ""
    if not mode:
        mode = _display_mode(repo_root)

    def _auto() -> str:
        data = _create_data(os.path.join(tracker, ticket_id))
        if data.get("jira_key"):
            return data["jira_key"]
        if data.get("alias"):
            return data["alias"]
        return _short_prefix(ticket_id, tracker)

    if mode == "auto":
        out = _auto()
    elif mode == "canonical":
        out = ticket_id
    elif mode == "alias":
        out = _create_data(os.path.join(tracker, ticket_id)).get("alias") or ticket_id
    elif mode == "short":
        out = _short_prefix(ticket_id, tracker)
    else:
        sys.stderr.write(f"WARN: unknown ticket.display_mode '{mode}' — falling back to auto\n")
        out = _auto()
    sys.stdout.write(out + "\n")
    return 0
