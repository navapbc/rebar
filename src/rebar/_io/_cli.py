"""CLI handlers for ``rebar export`` (and, later, ``rebar import``).

Mirrors the manual flag-parsing idiom used across rebar's command impls
(``--flag value`` and ``--flag=value`` both accepted). Export always emits NDJSON
to a sink (stdout or ``-o FILE``); run metadata goes to stderr so every stdout
line stays a clean ticket object.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .export_ndjson import export_tickets
from .import_ndjson import import_tickets

_EXPORT_USAGE = (
    "Usage: rebar export [-o FILE] [--status S[,S]] [--type T[,T]] [--parent ID] "
    "[--strip-external|--no-jira] [--include-session-logs] [--exclude-archived] "
    "[--include-deleted]"
)


def export_cli(argv: list[str], *, repo_root: str | Path | None = None) -> int:
    """Parse flags, stream NDJSON, print run metadata to stderr. Returns exit code."""
    from rebar._cli._parser import ParseError, render_parse_error
    from rebar._cli._parsers.core.io import build_export

    # The factory is the parser of record: it produces the namespace that drives
    # execution below. The one thin loop here only handles the two slivers argparse
    # cannot express byte-exactly and then hands a canonical ``--flag=value`` argv to
    # the factory: (1) export consumes the token after a value flag VERBATIM — even an
    # option-looking one (``--status --type`` sets status to ``--type``), which argparse
    # would reject; and (2) the bespoke ``unknown option`` reject (a bare word, an
    # unknown flag, or a value flag missing its value) with exit 2, plus the ``-o=``
    # short-equals spelling. The accepted values themselves come from ``ns``, not from a
    # second imperative pass.
    _value_flags = {
        "-o": "--out",
        "--out": "--out",
        "--status": "--status",
        "--type": "--type",
        "--parent": "--parent",
    }
    _toggles = (
        "--strip-external",
        "--no-jira",
        "--include-session-logs",
        "--exclude-archived",
        "--include-deleted",
    )
    norm: list[str] = []
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a.startswith("-o="):
            norm.append("--out=" + a[len("-o=") :])
            i += 1
        elif a in _value_flags and i + 1 < n:
            norm.append(f"{_value_flags[a]}={argv[i + 1]}")
            i += 2
        elif a.startswith(("--out=", "--status=", "--type=", "--parent=")) or a in _toggles:
            norm.append(a)
            i += 1
        else:
            print(f"Error: unknown option '{a}'", file=sys.stderr)  # noqa: T201 — CLI presentation: export/import command status/usage output
            print(_EXPORT_USAGE, file=sys.stderr)  # noqa: T201 — CLI presentation: export/import command status/usage output
            return 2

    try:
        ns = build_export(prog="rebar export").parse_args(norm)
    except ParseError as exc:
        return render_parse_error(exc)

    meta = export_tickets(
        out=ns.out if ns.out is not None else sys.stdout,
        status=ns.status,
        ticket_type=ns.ticket_type,
        parent=ns.parent,
        strip_external=ns.strip_external,
        include_session_logs=ns.include_session_logs,
        exclude_archived=ns.exclude_archived,
        include_deleted=ns.include_deleted,
        repo_root=repo_root,
    )
    print(  # noqa: T201 — CLI presentation: export/import command status/usage output
        f"exported {meta['exported']} ticket(s) "
        f"(schema_version={meta['schema_version']}, source_env={meta['source_env']})",
        file=sys.stderr,
    )
    return 0


_IMPORT_USAGE = "Usage: rebar import [FILE] [--dry-run]   (reads stdin if FILE omitted)"


def import_cli(argv: list[str], *, repo_root: str | Path | None = None) -> int:
    """Parse flags, import NDJSON (FILE or stdin), print a summary. Returns exit code."""
    from rebar._cli._parser import ParseError, render_parse_error
    from rebar._cli._parsers.core.io import build_import

    # Factory is the parser of record: its namespace drives the import below. The one
    # loop here owns only import's argparse-inexpressible rejects — an option-looking
    # token (INCLUDING a bare ``--``) is a hard ``unknown option`` rather than an
    # argparse end-of-options separator, and a second positional is ``unexpected
    # argument`` — then hands a canonical argv to the factory.
    dry_run = False
    in_file: str | None = None
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--dry-run":
            dry_run = True
            i += 1
        elif a.startswith("-") and a != "-":
            print(f"Error: unknown option '{a}'", file=sys.stderr)  # noqa: T201 — CLI presentation: export/import command status/usage output
            print(_IMPORT_USAGE, file=sys.stderr)  # noqa: T201 — CLI presentation: export/import command status/usage output
            return 2
        elif in_file is None:
            in_file = a
            i += 1
        else:
            print(f"Error: unexpected argument '{a}'", file=sys.stderr)  # noqa: T201 — CLI presentation: export/import command status/usage output
            print(_IMPORT_USAGE, file=sys.stderr)  # noqa: T201 — CLI presentation: export/import command status/usage output
            return 2

    try:
        ns = build_import(prog="rebar import").parse_args(
            [*(["--dry-run"] if dry_run else []), *(["--", in_file] if in_file is not None else [])]
        )
    except ParseError as exc:
        return render_parse_error(exc)

    source = ns.file if ns.file is not None else sys.stdin
    meta = import_tickets(source, dry_run=ns.dry_run, repo_root=repo_root)
    if ns.dry_run:
        print(f"[dry-run] would create {meta['would_create']} ticket(s)", file=sys.stderr)  # noqa: T201 — CLI presentation: export/import command status/usage output
    else:
        print(  # noqa: T201 — CLI presentation: export/import command status/usage output
            f"imported {meta['created']} ticket(s), {meta['links']} link(s), "
            f"{meta['comments']} comment(s); {len(meta['warnings'])} warning(s)",
            file=sys.stderr,
        )
    return 0
