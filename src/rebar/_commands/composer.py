"""Tier B event-composer commands (docs/bash-migration.md §4): create (+ edit/link/
unlink/revert as they land).

These are the heavier leaf writes — multi-flag arg parsing, validation with
``--output json`` error envelopes, alias generation, and structured output. Each
splits into a ``*_core`` (validation + event compose + append through the seam,
returning structured data) shared by the library, and a ``*_cli`` (output-format
parsing + text/json formatting) used by the bash dispatcher's Python route. The
core reuses the same Python helpers the bash already delegated to (alias compute,
the shared reducer, ``rebar._engine_support.output``) so behaviour matches.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid as _uuid

from rebar import _engine
from rebar._commands._seam import append_event, CommandError, tracker_dir
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
from rebar._engine_support.resolver import resolve_ticket_id

_TYPES = ("bug", "epic", "story", "task")

_USAGE = (
    "Usage: ticket create <ticket_type> <title> [--parent <id>] [--priority <n>] "
    "[--assignee <name>] [--description <text>] [--tags <tag1,tag2>]\n"
    "  ticket_type: bug | epic | story | task\n"
    "  --priority, -p: 0-4 (0=critical, 4=backlog; default: 2)"
)


def _new_ticket_id() -> str:
    """Fresh 16-hex canonical ticket id (``xxxx-xxxx-xxxx-xxxx``), as bash generates."""
    u = _uuid.uuid4().hex
    return f"{u[:4]}-{u[4:8]}-{u[8:12]}-{u[12:16]}"


def _compute_alias(ticket_id: str) -> str:
    """Human alias via the shared ticket-alias-compute.py (same script bash calls).

    Honors TICKET_WORDLIST_PATH (engine_env sets it), else the bundled wordlist.
    Emits the same ``WARN: ...hex fallback...`` line bash does when the wordlist is
    missing. Returns the alias (or the hex fallback the script prints).
    """
    script = _engine.engine_dir() / "ticket-alias-compute.py"
    wordlist = os.environ.get("TICKET_WORDLIST_PATH") or str(_engine.wordlist_path())
    proc = subprocess.run(
        [sys.executable, str(script), ticket_id, wordlist],
        capture_output=True,
        text=True,
        check=False,
    )
    if "FALLBACK" in proc.stderr:
        print("WARN: ticket-wordlist.txt not found — using hex fallback alias", file=sys.stderr)
    return proc.stdout.strip()


def create_core(
    ticket_type: str,
    title: str,
    *,
    parent: str | None = None,
    priority: int | str | None = None,
    assignee: str | None = None,
    description: str | None = None,
    tags=None,
    repo_root=None,
) -> dict:
    """Validate, compose, and append a CREATE event; return ``{id, alias, title}``.

    Mirrors ``ticket_create``'s validation order and messages: ticket_type enum
    (carries the invalid_ticket_type envelope), non-empty title, title ≤ 255, the
    U+2192→``->`` normalisation, priority 0-4, init check, and parent resolution
    (exists / has CREATE-or-SNAPSHOT / not closed). Raises :class:`CommandError` on
    any failure.
    """
    from rebar.reducer import reduce_ticket

    tracker = tracker_dir(repo_root)

    if ticket_type not in _TYPES:
        raise CommandError(
            f"Error: invalid ticket type '{ticket_type}'. Must be one of: bug, epic, story, task",
            error_code="invalid_ticket_type",
            input_str=ticket_type,
        )
    if not title:
        raise CommandError("Error: title must be non-empty")
    if len(title) > 255:
        raise CommandError(f"Error: title exceeds 255 characters ({len(title)} chars)")

    prio = "2" if priority is None or priority == "" else str(priority)
    if prio not in ("0", "1", "2", "3", "4"):
        raise CommandError(f"Error: invalid priority '{prio}'. Must be 0-4")

    title = title.replace("→", "->")

    if not (tracker / ".env-id").is_file():
        raise CommandError("Error: ticket system not initialized. Run 'ticket init' first.")

    parent_id = ""
    if parent:
        resolved = resolve_ticket_id(parent, str(tracker)) or parent
        if not (tracker / resolved).is_dir():
            raise CommandError(f"Error: parent ticket '{resolved}' does not exist")
        pdir = tracker / resolved
        if not any(
            p.name.endswith(("-CREATE.json", "-SNAPSHOT.json")) and not p.name.startswith(".")
            for p in pdir.iterdir()
        ):
            raise CommandError(
                f"Error: parent ticket '{resolved}' has no CREATE or SNAPSHOT event"
            )
        if (reduce_ticket(str(pdir)) or {}).get("status") == "closed":
            raise CommandError(
                f"Error: cannot create child of closed ticket '{resolved}'. "
                f"Reopen the parent first with: ticket transition {resolved} closed open"
            )
        parent_id = resolved

    tags_list = (
        [t.strip() for t in tags.split(",") if t.strip()]
        if isinstance(tags, str)
        else [t for t in (tags or []) if t]
    )

    ticket_id = _new_ticket_id()
    alias = _compute_alias(ticket_id)

    data = {
        "ticket_type": ticket_type,
        "title": title,
        "parent_id": parent_id,
        "description": description or "",
        "tags": tags_list,
        "priority": int(prio),
        "id": ticket_id,
    }
    if assignee:
        data["assignee"] = assignee
    if alias:
        data["alias"] = alias

    append_event(ticket_id, "CREATE", data, tracker, repo_root=repo_root)
    return {"id": ticket_id, "alias": alias or None, "title": title}


def create_cli(argv: list[str], *, repo_root=None) -> int:
    """Dispatcher Python route for ``create``: parse --output + flags, format output.

    Returns the process exit code; reproduces the bash text/json output and the
    json error envelope on validation failure.
    """
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if len(rest) < 2:
        print(_USAGE, file=sys.stderr)
        return 1

    ticket_type, title = rest[0], rest[1]
    parent = priority = assignee = description = None
    tags = ""
    i, args = 2, rest
    n = len(args)
    while i < n:
        a = args[i]
        if a in ("--parent",) and i + 1 < n:
            parent = args[i + 1]; i += 2
        elif a.startswith("--parent="):
            parent = a[len("--parent="):]; i += 1
        elif a in ("--priority", "-p") and i + 1 < n:
            priority = args[i + 1]; i += 2
        elif a.startswith("--priority="):
            priority = a[len("--priority="):]; i += 1
        elif a in ("--assignee",) and i + 1 < n:
            assignee = args[i + 1]; i += 2
        elif a.startswith("--assignee="):
            assignee = a[len("--assignee="):]; i += 1
        elif a in ("--description", "-d") and i + 1 < n:
            description = args[i + 1]; i += 2
        elif a.startswith("--description="):
            description = a[len("--description="):]; i += 1
        elif a in ("--tags",) and i + 1 < n:
            tags = f"{tags},{args[i + 1]}" if tags else args[i + 1]; i += 2
        elif a.startswith("--tags="):
            v = a[len("--tags="):]
            tags = f"{tags},{v}" if tags else v; i += 1
        else:
            parent = a; i += 1  # bare positional → parent (backward-compatible)

    try:
        res = create_core(
            ticket_type, title, parent=parent, priority=priority, assignee=assignee,
            description=description, tags=tags, repo_root=repo_root,
        )
    except CommandError as exc:
        if fmt == "json" and exc.error_code:
            print(json.dumps(error_envelope(exc.error_code, exc.input_str, exc.message, exc.returncode)))
        print(exc.message, file=sys.stderr)
        return exc.returncode

    if fmt == "json":
        print(json.dumps({"id": res["id"], "alias": res["alias"], "title": res["title"]}))
    else:
        alias, tid = res["alias"], res["id"]
        if alias and alias != tid:
            print(f"Created ticket {alias} ({tid}): {res['title']}")
        else:
            print(f"Created ticket {tid}: {res['title']}")
        print(tid)
    return 0
