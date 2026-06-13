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
    ``error_code``/``input_str`` are set when the bash counterpart also emits a
    ``--output json`` error envelope (e.g. invalid_ticket_type), so the CLI path can
    reproduce that envelope before the stderr prose.
    """

    def __init__(
        self,
        message: str,
        returncode: int = 1,
        *,
        error_code: str | None = None,
        input_str: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode
        self.error_code = error_code
        self.input_str = input_str


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


def author(fallback: str = "Unknown") -> str:
    """Commit author name from git config, falling back to ``fallback`` (bash parity).

    The fallback string differs by command: comment / file-impact / verify-commands
    use ``Unknown``; the tag helpers use lowercase ``unknown``. Callers pass the
    value their bash counterpart uses so a git-config-less environment matches.
    """
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
    return fallback


def current_tags(ticket_id: str, tracker: Path) -> list[str]:
    """The compiled ``tags`` list for a ticket via the shared reducer (single source).

    Mirrors the bash tag helpers, which reduce the ticket (``ticket_show``) to read
    current tags before composing the next EDIT. Returns ``[]`` when the ticket has
    no tags or cannot be reduced (the bash helpers swallow show failures too).
    """
    from rebar.reducer import reduce_ticket

    try:
        return list(reduce_ticket(str(tracker / ticket_id)).get("tags") or [])
    except Exception:
        return []


def append_event(
    ticket_id: str,
    event_type: str,
    data: dict,
    tracker: Path,
    *,
    repo_root=None,
    author_fallback: str = "Unknown",
) -> None:
    """Compose an event and append it through the single locked write path.

    Builds the canonical event envelope (``{timestamp, uuid, event_type, env_id,
    author, data}``). Under ``REBAR_WRITE_CORE=python`` (Tier D) the locked
    commit + push runs IN-PROCESS via ``rebar._store.event_append.write_and_push``;
    otherwise it stages to a temp file and delegates to the bash seam
    ``ticket-append-event.sh`` → ``write_commit_event`` (which re-canonicalises via
    ``jq -S -c`` to the same bytes). Either way raises :class:`CommandError`
    carrying the exit code on failure (e.g. 75 = rebase/merge guard).
    """
    from rebar._switch import uses_python

    timestamp, uuid_str = time.time_ns(), str(_uuid.uuid4())
    event = {
        "timestamp": timestamp,
        "uuid": uuid_str,
        "event_type": event_type,
        "env_id": env_id(tracker),
        "author": author(author_fallback),
        "data": data,
    }

    if uses_python("REBAR_WRITE_CORE"):
        # In-process locked commit + best-effort push (the canonical committer owns
        # serialisation; it never re-derives the envelope fields composed above).
        from rebar._store import event_append as _store_append
        from rebar._store.event_append import StoreError
        from rebar._store.lock import LockTimeout, RebaseGuard

        try:
            _store_append.write_and_push(str(tracker), ticket_id, event)
        except (StoreError, RebaseGuard, LockTimeout) as exc:
            raise CommandError(str(exc), returncode=getattr(exc, "returncode", 1)) from None
        return

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
