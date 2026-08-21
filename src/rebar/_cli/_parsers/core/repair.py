"""``rebar`` store-repair parser factories (RP-05 S2b).

Prog-bound argparse renderings of the four repair/maintenance arms:
``doctor`` (:mod:`rebar._commands.doctor`), ``fsck``
(:mod:`rebar._commands.fsck`), ``fsck-recover``
(:mod:`rebar._commands.fsck_recover`), and ``tracker-maintenance``
(:mod:`rebar._commands.tracker_maintenance`). ``doctor``/``fsck`` honour the
``--output`` profile; ``fsck-recover``/``tracker-maintenance`` do not. Each keeps
its bespoke ``Usage:`` / ``Error: unknown option`` diagnostics and exit codes.
Only the stdlib and :mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def _base(prog: str, description: str | None = None) -> argparse.ArgumentParser:
    return build_argument_parser(
        prog=prog, description=description, add_help=False, allow_abbrev=False
    )


def build_doctor(*, prog: str) -> argparse.ArgumentParser:
    """``rebar doctor [--repair] [--dry-run] [--output json]``."""
    parser = _base(prog, "Diagnose the store and heal what is safe to fix (--repair).")
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text", help="output format"
    )
    parser.add_argument("--repair", action="store_true", help="apply the safe fixes")
    parser.add_argument("--dry-run", action="store_true", help="report fixes without applying")
    return parser


def build_fsck(*, prog: str) -> argparse.ArgumentParser:
    """``rebar fsck [--repair] [--dry-run] [--include-archived] [--only=...] [--limit=N]``."""
    parser = _base(prog, "Check store integrity (JSON validity, CREATE presence, index.lock).")
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text", help="output format"
    )
    parser.add_argument("--repair", action="store_true", help="repair what is safe to fix")
    parser.add_argument("--repair-snapshots", action="store_true", help="rebuild snapshot state")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument(
        "--include-archived", action="store_true", help="also check archived tickets"
    )
    parser.add_argument("--only", help="check only the named ticket(s)")
    parser.add_argument("--limit", metavar="N", help="check at most N tickets")
    return parser


def build_fsck_recover(*, prog: str) -> argparse.ArgumentParser:
    """``rebar fsck-recover [--tracker-dir <path>] [--detect-only] ...``."""
    parser = _base(prog, "Recover the tracker worktree (dangling commits, interrupted rebases).")
    parser.add_argument("--tracker-dir", help="path to the tracker worktree")
    parser.add_argument("--detect-only", action="store_true", help="detect without recovering")
    parser.add_argument("--recover-dangling", action="store_true", help="recover dangling commits")
    parser.add_argument("--timeout", metavar="SECONDS", help="git operation timeout")
    return parser


def build_tracker_maintenance(*, prog: str) -> argparse.ArgumentParser:
    """``rebar tracker-maintenance [--status] [--clean] [--force=<reason>]``."""
    parser = _base(prog, "Supported door for raw git in the tracker; backup ref + refusal + audit.")
    parser.add_argument("--status", action="store_true", help="report tracker maintenance status")
    parser.add_argument("--clean", action="store_true", help="clean the tracker worktree")
    parser.add_argument("--force", help="break-glass override (--force=<reason>)")
    return parser
