"""``rebar`` read-command parser factories (RP-05 S2b).

Prog-bound argparse renderings of the read arms owned by the hand-rolled dispatch
in :mod:`rebar._engine_support.reads_cli` (plus ``validate``, the repo-wide health
read in :mod:`rebar._engine_support.validate`). Each models the ACCEPTED grammar —
the ``--output`` profile spelling, the family-specific filters, the positional
ticket id(s) — while the runtime handlers keep their bespoke unknown-option
diagnostics (``Error: unknown option``, next-batch's ``Unknown flag``, validate's
``Unknown option``) and exit codes. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser

# The read arms hand-handle ``--help``/``-h`` (print usage, exit 0) rather than
# using argparse's auto-help action, so every factory is built with
# ``add_help=False`` and ``allow_abbrev=False`` (no prefix matching — a truncated
# flag stays an unknown token, matching the loops' exact-token comparison).
_SORT_METAVAR = "<priority|created|updated|id|status>"


def _base(prog: str) -> argparse.ArgumentParser:
    return build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)


def build_show(*, prog: str) -> argparse.ArgumentParser:
    """``rebar show [--output llm] [--include-scratch] <ticket_id>...``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("json", "llm"), default="json")
    parser.add_argument("--include-scratch", action="store_true")
    # Reconciler-only, intentionally undocumented (kept out of usage).
    parser.add_argument("--include-provenance", action="store_true")
    parser.add_argument("ticket_id", nargs="*")
    return parser


def build_list(*, prog: str) -> argparse.ArgumentParser:
    """``rebar list`` — the full filter surface (all optional)."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("json", "llm"), default="json")
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--exclude-deleted", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--with-children-count", action="store_true")
    parser.add_argument("--unblocked", action="store_true")
    parser.add_argument("--blocked", action="store_true")
    parser.add_argument("--type")
    parser.add_argument("--status")
    parser.add_argument("--priority")
    parser.add_argument("--parent")
    parser.add_argument("--has-tag")
    parser.add_argument("--without-tag")
    parser.add_argument("--min-children")
    parser.add_argument("--sort", metavar=_SORT_METAVAR)
    return parser


def build_next_batch(*, prog: str) -> argparse.ArgumentParser:
    """``rebar next-batch <epic-id> [--limit=N|unlimited] [--output json]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("json", "text", "report"), default="text")
    parser.add_argument("--limit", metavar="N")
    parser.add_argument("epic", nargs="?")
    return parser


def build_deps(*, prog: str) -> argparse.ArgumentParser:
    """``rebar deps <ticket_id> [--include-archived]`` (no ``--output``)."""
    parser = _base(prog)
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("ticket_id", nargs="?")
    return parser


def build_ready(*, prog: str) -> argparse.ArgumentParser:
    """``rebar ready [--output json|llm] [--epic <id>] [--sort=...]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "llm", "json"), default="text")
    parser.add_argument("--epic")
    parser.add_argument("--sort", metavar=_SORT_METAVAR)
    return parser


def build_search(*, prog: str) -> argparse.ArgumentParser:
    """``rebar search <query> [--output json|llm] [--full] [filters...]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("json", "llm"), default="json")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--status")
    parser.add_argument("--type")
    parser.add_argument("--has-tag")
    parser.add_argument("--sort", metavar=_SORT_METAVAR)
    parser.add_argument("query", nargs="?")
    return parser


def build_session_logs(*, prog: str) -> argparse.ArgumentParser:
    """``rebar session-logs [--output json|llm] [--limit=<n>]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("json", "llm"), default="json")
    parser.add_argument("--limit", metavar="N")
    return parser


def build_validate(*, prog: str) -> argparse.ArgumentParser:
    """``rebar validate [--quick] [--full] [--verbose] [--output json] [--terse]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--terse", action="store_true")
    return parser
