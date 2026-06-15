#!/usr/bin/env python3
"""Canonical structured-output flag parsing for rebar commands.

Single source of truth for rebar's ``--output`` / ``-o`` flag (the only
structured-output selector; the legacy ``--json`` and ``--format=*`` flags were
removed). Every interface resolves the output format through this one module so
the accepted spellings, the per-command allowed values, the default, and the
"unsupported output format" error text exist in exactly ONE place — never
duplicated across the bash command scripts and the Python read path.

Accepted spellings (kubectl/helm/aws style; the flag is named ``--output`` so
``-o`` is its short alias)::

    -o json        --output json        # space form
    -o=json        --output=json        # equals form

Per-command behaviour is selected by a *profile* (default + allowed set):

    reader  show / list / search   default json   {json, llm}
    ready   ready                   default text   {text, llm, json}
    report  validate / next-batch / bridge-status / list-epics /
            summary / check-ac / quality-check / fsck / bridge-fsck
                                    default text   {text, json}

Here ``text`` is each command's human/default rendering (for ``ready`` that is
the one-id-per-line list; for the report commands it is the human text report),
``json`` is the structured machine shape, and ``llm`` is the minified short-key
NDJSON variant.

Used in-process by the read/report entry points::

    from rebar._engine_support.output import parse_output
    fmt, rest = parse_output(argv, "reader")
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "PROFILES",
    "OutputFormatError",
    "allowed_for",
    "default_for",
    "parse_output",
    "error_envelope",
]

# profile -> (default, allowed-tuple). The default is what an absent flag means.
PROFILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "reader": ("json", ("json", "llm")),
    "ready": ("text", ("text", "llm", "json")),
    "report": ("text", ("text", "json")),
}

_SHORT = "-o"
_LONG = "--output"


class OutputFormatError(ValueError):
    """An invalid or missing ``--output`` value."""


def _resolve_profile(profile: str | None, allowed, default):
    """Return (default, allowed-tuple) from a named profile or explicit args."""
    if profile is not None:
        try:
            return PROFILES[profile]
        except KeyError:
            raise OutputFormatError(f"unknown output profile '{profile}'") from None
    if allowed is None or default is None:
        raise OutputFormatError("parse_output requires either a profile or allowed+default")
    return default, tuple(allowed)


def default_for(profile: str) -> str:
    """The default format for a named profile."""
    return _resolve_profile(profile, None, None)[0]


def allowed_for(profile: str) -> tuple[str, ...]:
    """The allowed formats for a named profile."""
    return _resolve_profile(profile, None, None)[1]


def parse_output(
    argv: Iterable[str],
    profile: str | None = None,
    *,
    allowed: Iterable[str] | None = None,
    default: str | None = None,
) -> tuple[str, list[str]]:
    """Extract the ``--output``/``-o`` format from ``argv``.

    Returns ``(fmt, remaining_args)`` where ``remaining_args`` is ``argv`` with
    the output flag (and its value, for the space form) removed — so callers can
    parse their own flags from what's left. ``fmt`` is the resolved, validated
    format, or the profile default when the flag is absent. A repeated flag uses
    the last occurrence (conventional CLI behaviour).

    Raises :class:`OutputFormatError` on an unknown value or a value-less flag.
    """
    default, allowed_t = _resolve_profile(profile, allowed, default)
    argv = list(argv)
    fmt = default
    rest: list[str] = []
    i, n = 0, len(argv)
    while i < n:
        arg = argv[i]
        if arg in (_SHORT, _LONG):
            if i + 1 >= n:
                raise OutputFormatError(
                    f"{arg} requires a value. Supported: {', '.join(allowed_t)}"
                )
            value = argv[i + 1]
            i += 2
        elif arg.startswith(_SHORT + "=") or arg.startswith(_LONG + "="):
            value = arg.split("=", 1)[1]
            i += 1
        else:
            rest.append(arg)
            i += 1
            continue
        if value not in allowed_t:
            raise OutputFormatError(
                f"unsupported output format '{value}'. Supported: {', '.join(allowed_t)}"
            )
        fmt = value
    return fmt, rest


def error_envelope(error: str, input_str: str, message: str, exit_code=None) -> dict:
    """Build the canonical machine-readable error envelope (common.schema.json
    error_envelope). ``exit_code`` is optional (see docs/exit-codes.md). Single
    source of truth for the failure shape every ``--output json`` command emits on
    stdout, so agents never have to parse stderr prose."""
    env = {"error": error, "input": input_str, "message": message}
    if exit_code is not None and exit_code != "":
        env["exit_code"] = int(exit_code)
    return env
