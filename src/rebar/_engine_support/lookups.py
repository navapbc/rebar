"""In-process ``resolve`` (with ``--display[=MODE]``).

The resolution/display arm: ``resolve`` resolves an id/alias/prefix through the shared
resolver and, with ``--display``, renders it in a display mode. It replaced the removed
``exists`` and ``format`` verbs. The library never exposed it, so this is CLI-only.
"""

from __future__ import annotations

import glob
import json
import os
import sys

from rebar._engine_support.resolver import resolve_ticket_id


def _create_data(ticket_dir: str) -> dict:
    """``data`` of the first CREATE event in ``ticket_dir`` ({} if none/unreadable).

    The format/auto/alias paths read ``data.alias`` /
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
    ``auto`` rather than failing. (Config discovery is $REBAR_CONFIG / rebar.toml /
    pyproject / legacy conf.)"""
    from rebar.config import ConfigError, compose_config

    try:
        return compose_config(repo_root).ticket.display_mode
    except ConfigError:
        return "auto"


# ── CLI arm ───────────────────────────────────────────────────────────────
def _render_display(ticket_id: str, tracker: str, mode: str) -> str:
    """Render a resolved ``ticket_id`` in display ``mode`` (``auto``/``canonical``/
    ``alias``/``short``); an unknown mode warns and falls back to ``auto``."""

    def _auto() -> str:
        data = _create_data(os.path.join(tracker, ticket_id))
        if data.get("jira_key"):
            return data["jira_key"]
        if data.get("alias"):
            return data["alias"]
        return _short_prefix(ticket_id, tracker)

    if mode == "auto":
        return _auto()
    if mode == "canonical":
        return ticket_id
    if mode == "alias":
        return _create_data(os.path.join(tracker, ticket_id)).get("alias") or ticket_id
    if mode == "short":
        return _short_prefix(ticket_id, tracker)
    sys.stderr.write(f"WARN: unknown ticket.display_mode '{mode}' — falling back to auto\n")
    return _auto()


def _pop_display(argv: list[str]) -> tuple[list[str], str | None]:
    """Remove ``--display[=MODE]`` / ``--display MODE`` from ``argv``; return the remaining
    tokens and the mode (``None`` when absent, ``""`` for a bare ``--display``)."""
    rest: list[str] = []
    mode: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--display="):
            mode = tok.split("=", 1)[1]
        elif tok == "--display":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            # A following non-option token is the MODE only when a ticket id already
            # precedes it, so `--display <id>` still treats <id> as the ticket.
            if nxt is not None and not nxt.startswith("-") and rest:
                mode = nxt
                i += 1
            else:
                mode = ""
        else:
            rest.append(tok)
        i += 1
    return rest, mode


def resolve_cli(argv: list[str], tracker: str, repo_root: str | None) -> int:
    """``rebar resolve <id_or_alias_or_prefix> [--display[=MODE]]``.

    Resolve the id to its canonical form (exit 1 when not found). With ``--display`` the
    resolved ticket is rendered in ``MODE`` (default: the configured
    ``ticket.display_mode``) instead of printing the canonical id; this replaced the
    removed ``exists`` and ``format`` verbs.
    """
    args, mode = _pop_display(argv)
    if len(args) < 1:
        sys.stderr.write("Usage: rebar resolve <id_or_alias_or_prefix> [--display[=MODE]]\n")
        return 1
    # Positional-only lookup: the permissive raw/dash-leading input is genuinely
    # argparse-inexpressible, so the first token is read raw. The registry declares
    # ``build_resolve`` for lazy census + canonical help (RP-05 S2d).
    raw = args[0]
    resolved = resolve_ticket_id(raw, tracker)
    if not resolved:  # None or "" (the resolver returns "" for an empty input)
        sys.stderr.write(f"Error: ticket '{raw}' not found\n")
        return 1
    if mode is None:
        sys.stdout.write(resolved + "\n")
        return 0
    sys.stdout.write(_render_display(resolved, tracker, mode or _display_mode(repo_root)) + "\n")
    return 0
