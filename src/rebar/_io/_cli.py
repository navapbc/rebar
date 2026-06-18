"""CLI handlers for ``rebar export`` (and, later, ``rebar import``).

Mirrors the manual flag-parsing idiom used across rebar's command impls
(``--flag value`` and ``--flag=value`` both accepted). Export always emits NDJSON
to a sink (stdout or ``-o FILE``); run metadata goes to stderr so every stdout
line stays a clean ticket object.
"""

from __future__ import annotations

import sys

from .export_ndjson import export_tickets
from .import_ndjson import import_tickets

_EXPORT_USAGE = (
    "Usage: rebar export [-o FILE] [--status S[,S]] [--type T[,T]] [--parent ID] "
    "[--strip-external|--no-jira] [--include-session-logs] [--exclude-archived] "
    "[--include-deleted]"
)


def export_cli(argv: list[str], *, repo_root=None) -> int:
    """Parse flags, stream NDJSON, print run metadata to stderr. Returns exit code."""
    out_file = None
    status = ticket_type = parent = None
    strip_external = include_session_logs = exclude_archived = include_deleted = False

    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a in ("-o", "--out") and i + 1 < n:
            out_file = argv[i + 1]
            i += 2
        elif a.startswith("--out="):
            out_file = a[len("--out=") :]
            i += 1
        elif a.startswith("-o="):
            out_file = a[len("-o=") :]
            i += 1
        elif a in ("--status",) and i + 1 < n:
            status = argv[i + 1]
            i += 2
        elif a.startswith("--status="):
            status = a[len("--status=") :]
            i += 1
        elif a in ("--type",) and i + 1 < n:
            ticket_type = argv[i + 1]
            i += 2
        elif a.startswith("--type="):
            ticket_type = a[len("--type=") :]
            i += 1
        elif a in ("--parent",) and i + 1 < n:
            parent = argv[i + 1]
            i += 2
        elif a.startswith("--parent="):
            parent = a[len("--parent=") :]
            i += 1
        elif a in ("--strip-external", "--no-jira"):
            strip_external = True
            i += 1
        elif a == "--include-session-logs":
            include_session_logs = True
            i += 1
        elif a == "--exclude-archived":
            exclude_archived = True
            i += 1
        elif a == "--include-deleted":
            include_deleted = True
            i += 1
        else:
            print(f"Error: unknown option '{a}'", file=sys.stderr)
            print(_EXPORT_USAGE, file=sys.stderr)
            return 2

    meta = export_tickets(
        out=out_file if out_file is not None else sys.stdout,
        status=status,
        ticket_type=ticket_type,
        parent=parent,
        strip_external=strip_external,
        include_session_logs=include_session_logs,
        exclude_archived=exclude_archived,
        include_deleted=include_deleted,
        repo_root=repo_root,
    )
    print(
        f"exported {meta['exported']} ticket(s) "
        f"(schema_version={meta['schema_version']}, source_env={meta['source_env']})",
        file=sys.stderr,
    )
    return 0


_IMPORT_USAGE = "Usage: rebar import [FILE] [--dry-run]   (reads stdin if FILE omitted)"


def import_cli(argv: list[str], *, repo_root=None) -> int:
    """Parse flags, import NDJSON (FILE or stdin), print a summary. Returns exit code."""
    in_file = None
    dry_run = False

    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--dry-run":
            dry_run = True
            i += 1
        elif a.startswith("-") and a != "-":
            print(f"Error: unknown option '{a}'", file=sys.stderr)
            print(_IMPORT_USAGE, file=sys.stderr)
            return 2
        elif in_file is None:
            in_file = a
            i += 1
        else:
            print(f"Error: unexpected argument '{a}'", file=sys.stderr)
            print(_IMPORT_USAGE, file=sys.stderr)
            return 2

    source = in_file if in_file is not None else sys.stdin
    meta = import_tickets(source, dry_run=dry_run, repo_root=repo_root)
    if dry_run:
        print(f"[dry-run] would create {meta['would_create']} ticket(s)", file=sys.stderr)
    else:
        print(
            f"imported {meta['created']} ticket(s), {meta['links']} link(s), "
            f"{meta['comments']} comment(s); {len(meta['warnings'])} warning(s)",
            file=sys.stderr,
        )
    return 0
