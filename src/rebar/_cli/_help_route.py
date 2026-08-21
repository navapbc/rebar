"""Top-level help / overview / unknown pre-scan for the ``rebar`` CLI (RP-05 S2d).

The canonical package help lives under ``rebar/_cli/help/*.txt`` (generated from the parser
factories by ``scripts/gen_cli_help.py`` and served verbatim by :mod:`rebar._cli._help`).
This module owns the LEXICAL pre-scan that lets ``main()`` answer a help/overview/unknown
request from those committed bytes **before** any RP-04 operation snapshot, config
parsing/materialization, store mount, handler/factory resolution, or optional import — a help
request must never compose an operation snapshot or leak a ``.tickets-tracker`` into the repo
(bug dd62).

The pre-scan is deliberately narrow. It recognizes:

* the leading global config-prefix token shape (``-c SECTION.KEY=VALUE`` / ``--config=…``),
  which it skips lexically (never materializing config) to find the command spelling;
* the top-level help words ``help`` / ``--help`` / ``-h`` (and ``help <command>``);
* a help request (``--help`` / ``-h``) for a help-backed subcommand — honoring the nested
  ``bridge`` family, whose children own their own help; and
* an unknown subcommand.

Everything else — a real command invocation, and the advanced intercept commands that own
their own ``--help`` (``review-plan --help`` stays argparse-owned) — falls through unchanged
by returning ``None``.
"""

from __future__ import annotations

import sys

from rebar._cli import _help
from rebar._cli._registry import ROUTES, Route, route_for

# The nested-dispatch family whose children own their own help: only a LEADING help flag asks
# for the family's own usage (``bridge preview --help`` belongs to the preview child). This is
# ONLY the routes that share ``bridge``'s nested-dispatch parser (``bridge`` and its hidden
# alias) — NOT every route in the ``bridge`` group. The flat compatibility arms in that group
# own no children, so a non-leading ``--help`` is THEIR own usage request and must be served
# (deriving the set from ``group == "bridge"`` wrongly suppressed that). Derived from the
# registry so it never drifts from the route table.
_BRIDGE_FACTORY: str | None = next((r.parser_factory for r in ROUTES if r.name == "bridge"), None)
_NESTED_FAMILY: frozenset[str] = frozenset(
    r.name for r in ROUTES if _BRIDGE_FACTORY is not None and r.parser_factory == _BRIDGE_FACTORY
)
# Hidden alias spellings (e.g. ``bridge-status``) are neither advertised nor help-served here.
_HIDDEN_ALIASES: frozenset[str] = frozenset(r.name for r in ROUTES if r.hidden)


def _help_backed(route: Route) -> bool:
    """Whether ``route`` carries a pinned help artifact (mirrors the generator's census)."""
    return route.group != "intercept" and not route.hidden and not route.retired


def wants_help(rest: list[str]) -> bool:
    """True if a bare ``--help``/``-h`` appears before any ``--`` terminator.

    The dispatcher must honour a help flag in ANY position, not only ``rest[0]``:
    ``rebar create task --help`` used to fall through to the create handler, which consumed
    ``--help`` as the positional title and created a placeholder ticket (bug b8de). Scanning
    stops at the first ``--`` so a caller can suppress the help intercept at the dispatcher.
    """
    for tok in rest:
        if tok == "--":
            return False
        if tok in ("--help", "-h"):
            return True
    return False


def help_requested(sub: str, rest: list[str]) -> bool:
    """Whether ``rebar <sub> …`` is a help request the pre-scan should serve itself.

    Nested-dispatch families (the ``bridge`` group) own their children's help, so for them
    only a LEADING ``--help``/``-h`` asks for the family's own usage. Every other command has
    no nested help, so a ``--help``/``-h`` in any position before a ``--`` is a usage request.
    """
    if sub in _NESTED_FAMILY:
        return bool(rest) and rest[0] in ("--help", "-h")
    return wants_help(rest)


def emit_subcommand_help(sub: str) -> int:
    """Print ``sub``'s pinned usage.

    Known (help-backed) subcommand → stdout, exit 0. Otherwise → error + blank + overview all
    to stderr, exit 1.
    """
    text = _help.subcommand_help(sub)
    if text is not None:
        sys.stdout.write(text)
        return 0
    sys.stderr.write(f"Error: unknown subcommand '{sub}'\n\n")
    sys.stderr.write(_help.overview())
    return 1


def _valid_override(value: str) -> bool:
    """Whether a config-prefix value has the ``SECTION.KEY=VALUE`` shape (lexically)."""
    return "=" in value and not value.startswith("-")


def _strip_config_prefix(argv: list[str]) -> list[str] | None:
    """Skip WELL-FORMED leading ``-c``/``--config`` tokens, returning the residual argv.

    Returns ``None`` when the prefix is malformed (a missing or non ``SECTION.KEY=VALUE``
    value) so the real config parser in ``_main_dispatch`` produces its exact error rather
    than the pre-scan guessing at a help form.
    """
    out = list(argv)
    while out and (out[0] in ("-c", "--config") or out[0].startswith("--config=")):
        tok = out.pop(0)
        if tok.startswith("--config="):
            value = tok[len("--config=") :]
        elif out:
            value = out.pop(0)
        else:
            return None
        if not _valid_override(value):
            return None
    return out


def _emit_unknown(sub: str) -> int:
    """The bare unknown-subcommand contract: error to stderr + overview to stdout, exit 1."""
    sys.stderr.write(f"Error: unknown subcommand '{sub}'\n")
    sys.stdout.write(_help.overview())
    return 1


def pre_scan(argv: list[str]) -> int | None:
    """Serve a help/overview/unknown request from committed bytes, or ``None`` to fall through.

    Runs BEFORE any operation snapshot, config materialization, store mount, handler/factory
    resolution, or optional import. Returns the process exit code when it handled the request,
    else ``None`` (a real command, or an intercept command that owns its own ``--help``).
    """
    residual = _strip_config_prefix(argv)
    if residual is None:
        return None
    if not residual:
        sys.stdout.write(_help.overview())
        return 1

    first = residual[0]
    if first in ("help", "--help", "-h"):
        if len(residual) >= 2:
            return emit_subcommand_help(residual[1])
        sys.stdout.write(_help.overview())
        return 0

    sub, rest = first, residual[1:]
    route = route_for(sub)
    if route is not None and _help_backed(route) and sub not in _HIDDEN_ALIASES:
        if help_requested(sub, rest):
            return emit_subcommand_help(sub)
        return None
    if route is None and sub not in _HIDDEN_ALIASES:
        return _emit_unknown(sub)
    return None
