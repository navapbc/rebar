"""Shared infrastructure for Tier B leaf-write commands (docs/bash-migration.md §4).

A Tier B command is a small function that (1) validates args, (2) resolves the
ticket id, (3) composes the event JSON in Python, and (4) appends it through ONE
narrow seam — the bash ``ticket-append-event.sh`` wrapping ``write_commit_event``
(flock + atomic rename + git commit + best-effort push). This module owns the
pieces every leaf command shares: tracker/id resolution, the ghost check, event
metadata, and the seam subprocess call. The locked write path itself is NOT ported
here (that is Tier D); until then Python writes route through the bash core so
invariant I5 (single locked write path) holds unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid as _uuid
from pathlib import Path

from rebar import _engine, config
from rebar._engine_support.resolver import resolve_ticket_id


class CommandError(Exception):
    """A leaf-command failure with a stderr message and process exit code.

    The CLI entrypoint prints ``message`` to stderr and exits ``returncode``; the
    library facade maps it onto ``RebarError`` so the exit-1 contract is unchanged.
    """

    def __init__(self, message: str, returncode: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode


def tracker_dir(repo_root=None) -> Path:
    """Resolve the tracker dir (honors TICKETS_TRACKER_DIR, then repo-root)."""
    return config.tracker_dir(repo_root)


def require_id(ticket_id: str, tracker: Path) -> str:
    """Resolve any id form (full/short/alias/prefix) to the canonical dir name.

    Raises :class:`CommandError` (exit 1) when the id is empty or unresolvable —
    mirroring the bash ``_ticketlib_resolve_id`` contract (the resolver prints its
    own ambiguity/not-found diagnostics to stderr).
    """
    if not ticket_id:
        raise CommandError("Error: ticket id must be non-empty")
    resolved = resolve_ticket_id(ticket_id, str(tracker))
    if resolved is None:
        raise CommandError(f"Error: ticket '{ticket_id}' not found")
    return resolved


def require_not_ghost(ticket_id: str, tracker: Path) -> None:
    """Ghost check: the ticket must have a CREATE or SNAPSHOT event (else exit 1).

    Mirrors the bash ``find ... -name '*-CREATE.json' -o -name '*-SNAPSHOT.json'``
    guard that prevents writing an event onto a ticket that was never created.
    """
    tdir = tracker / ticket_id
    if tdir.is_dir():
        for entry in os.listdir(tdir):
            if entry.startswith("."):
                continue
            if entry.endswith("-CREATE.json") or entry.endswith("-SNAPSHOT.json"):
                return
    raise CommandError(f"Error: ticket {ticket_id} has no CREATE or SNAPSHOT event")


def env_id(tracker: Path) -> str:
    """The store's environment id (``.env-id``); empty string if absent."""
    try:
        return (tracker / ".env-id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def author() -> str:
    """Commit author name from git config, falling back to ``Unknown`` (bash parity)."""
    try:
        out = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            check=False,
        )
        name = out.stdout.strip()
        if name:
            return name
    except OSError:
        pass
    return "Unknown"


def append_event(
    ticket_id: str,
    event_type: str,
    data: dict,
    tracker: Path,
    *,
    repo_root=None,
) -> None:
    """Compose an event and append it through the bash write seam.

    Builds the same event envelope the bash command path builds
    (``{timestamp, uuid, event_type, env_id, author, data}``), stages it to a temp
    file inside the tracker (same filesystem as the seam's atomic rename), and
    delegates to ``ticket-append-event.sh`` → ``write_commit_event``. The seam
    re-canonicalises via ``jq -S -c`` so the committed bytes are identical to the
    bash path. Raises :class:`CommandError` carrying the seam's exit code on
    failure (e.g. 75 = rebase/merge guard).
    """
    timestamp, uuid_str = time.time_ns(), str(_uuid.uuid4())
    event = {
        "timestamp": timestamp,
        "uuid": uuid_str,
        "event_type": event_type,
        "env_id": env_id(tracker),
        "author": author(),
        "data": data,
    }

    tracker.mkdir(parents=True, exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix=".tmp-event-", dir=str(tracker))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(event, fh, ensure_ascii=False)
        seam = _engine.engine_dir() / "ticket-append-event.sh"
        proc = subprocess.run(
            ["bash", str(seam), ticket_id, staged],
            env=_engine.engine_env(repo_root),
            cwd=str(config.repo_root(repo_root)),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            msg = proc.stderr.strip() or "Error: failed to write and commit event"
            raise CommandError(msg, returncode=proc.returncode)
    finally:
        try:
            os.unlink(staged)
        except OSError:
            pass
