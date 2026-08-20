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


def _base(prog: str) -> argparse.ArgumentParser:
    return build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)


def build_doctor(*, prog: str) -> argparse.ArgumentParser:
    """``rebar doctor [--repair] [--dry-run] [--output json]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def build_fsck(*, prog: str) -> argparse.ArgumentParser:
    """``rebar fsck [--repair] [--dry-run] [--include-archived] [--only=...] [--limit=N]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--repair-snapshots", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--only")
    parser.add_argument("--limit", metavar="N")
    return parser


def build_fsck_recover(*, prog: str) -> argparse.ArgumentParser:
    """``rebar fsck-recover [--tracker-dir <path>] [--detect-only] ...``."""
    parser = _base(prog)
    parser.add_argument("--tracker-dir")
    parser.add_argument("--detect-only", action="store_true")
    parser.add_argument("--recover-dangling", action="store_true")
    parser.add_argument("--timeout", metavar="SECONDS")
    return parser


def build_tracker_maintenance(*, prog: str) -> argparse.ArgumentParser:
    """``rebar tracker-maintenance [--status] [--clean] [--force=<reason>]``."""
    parser = _base(prog)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--force")
    return parser
