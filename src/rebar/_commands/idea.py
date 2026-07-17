"""``rebar idea`` — capture an undesigned idea in a single genesis CREATE event.

An idea must be captured in one race-free operation. A create-then-transition path
would momentarily leave the ticket in ``open`` (claimable by a parallel agent — the
anti-pattern ``claim`` already forbids), so the idea is born directly in status
``idea`` via a single CREATE event carrying ``status=idea``.

The command is intentionally light: a title plus an optional description only, and it
always creates an ``epic`` (the container an idea is later decomposed into). It shares
``composer.create_core`` with ``rebar create`` but passes ``status="idea"`` and, unlike
``create``, emits no file-impact nudge (an undesigned idea has no file impact yet).
There is deliberately NO general ``create --status`` flag; ``idea`` is the sole producer
of a non-``open`` genesis status.
"""

from __future__ import annotations

import json
import sys

from rebar._commands import composer
from rebar._commands._seam import CommandError
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output

_USAGE = 'Usage: rebar idea "<title>" [--description=<text>] [--output json]'


def idea_cli(argv: list[str], *, repo_root=None) -> int:
    """Create an ``epic`` in status ``idea`` from a title (+ optional description).

    Reproduces ``create``'s text/json output shape (minus the file-impact nudge) and
    its json error envelope on validation failure. Returns the process exit code.
    """
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not rest:
        print(_USAGE, file=sys.stderr)
        return 1

    title = rest[0]
    description = None
    i, n = 1, len(rest)
    while i < n:
        a = rest[i]
        if a in ("--description", "-d") and i + 1 < n:
            description = rest[i + 1]
            i += 2
        elif a.startswith("--description="):
            description = a[len("--description=") :]
            i += 1
        else:
            print(f"Error: unexpected argument '{a}'", file=sys.stderr)
            print(_USAGE, file=sys.stderr)
            return 1

    try:
        res = composer.create_core(
            "epic",
            title,
            description=description,
            status="idea",
            repo_root=repo_root,
            creation_channel="cli",
        )
    except CommandError as exc:
        if fmt == "json" and exc.error_code:
            print(
                json.dumps(
                    error_envelope(exc.error_code, exc.input_str, exc.message, exc.returncode)
                )
            )
        print(exc.message, file=sys.stderr)
        return exc.returncode

    if fmt == "json":
        print(json.dumps({"id": res["id"], "alias": res["alias"], "title": res["title"]}))
    else:
        alias, tid = res["alias"], res["id"]
        if alias and alias != tid:
            print(f"Created idea {alias} ({tid}): {res['title']}")
        else:
            print(f"Created idea {tid}: {res['title']}")
        print(tid)
    return 0
