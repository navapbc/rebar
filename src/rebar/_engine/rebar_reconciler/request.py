"""Normalize canonical bridge verbs and retained reconciler flags into one request.

The parser is intentionally separate from ``__main__``: both the new
``preview``/``sync`` vocabulary and the retained direct-engine ``--mode`` adapter
enter the same lock, gate, pass, and exit-policy spine after this module returns.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from rebar._cli._parsers._common import _positive_int


class RequestError(ValueError):
    """An invocation error that the process boundary reports as exit 2."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise RequestError(f"{self.prog}: error: {message}")


def _tokens(value: str | None, option: str) -> tuple[str, ...]:
    if value is None:
        return ()
    tokens = tuple(part.strip() for part in value.split(",") if part.strip())
    if not tokens:
        raise RequestError(f"{option} must contain at least one non-empty identifier")
    return tokens


@dataclass(frozen=True)
class ReconcileRequest:
    """Parser-validated, mode-normalized request consumed by the orchestrator."""

    route: str
    repo_root: Path
    target_mode: Any
    max_changes: int | None = None
    selection_kind: str | None = None
    selection_tokens: tuple[str, ...] = ()
    filter_local_ids: set[str] | None = None
    dry_run_enumerate: bool = False


def _parser() -> _Parser:
    parser = _Parser(prog="rebar_reconciler", allow_abbrev=False)
    parser.add_argument("command", nargs="?", choices=("preview", "sync"))
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root (default: auto-detect from script location)",
    )
    parser.add_argument(
        "--mode",
        default=None,
        help=(
            "Direct-engine mode: dry-run | bootstrap-strict | bootstrap-throttle | "
            "live (default: live)"
        ),
    )
    parser.add_argument(
        "--dry-run-enumerate",
        action="store_true",
        help="List enumerable tracker directories and exit without running a pass.",
    )
    parser.add_argument(
        "--filter-local-ids",
        default=None,
        help="Compatibility write filter applied after the full differ computation.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--only", metavar="IDS")
    selection.add_argument("--except", dest="except_ids", metavar="IDS")
    parser.add_argument("--max-changes", type=_positive_int, metavar="N")
    return parser


def normalize_request(argv: list[str] | None, mode_mod: Any) -> ReconcileRequest:
    """Parse *argv* and preserve the distinct canonical and legacy defaults."""
    parser = _parser()
    args = parser.parse_args(argv)
    route = args.command or "legacy"
    if args.command is not None and args.mode is not None:
        raise RequestError("primary preview/sync commands cannot be combined with --mode")
    if args.command is None and (args.only is not None or args.except_ids is not None):
        raise RequestError("--only and --except require preview or sync")
    if args.command != "sync" and args.max_changes is not None:
        raise RequestError("--max-changes is supported only by sync")
    if args.command is not None and args.filter_local_ids is not None:
        raise RequestError("--filter-local-ids is available only on the legacy route")
    if args.command == "preview":
        target_mode = mode_mod.Mode.DRY_RUN
    elif args.command == "sync":
        target_mode = mode_mod.Mode.LIVE
    else:
        mode_value = args.mode if args.mode is not None else mode_mod.Mode.LIVE.value
        if mode_value == mode_mod.Mode.RECONCILE_CHECK.value:
            raise RequestError(
                "--mode reconcile-check has been removed; use preview for live Jira-vs-local "
                "proposed changes, fsck for offline binding/integrity audit, and status for "
                "operational state"
            )
        target_mode = mode_mod.Mode.from_str(mode_value)

    only = _tokens(args.only, "--only")
    excluded = _tokens(args.except_ids, "--except")
    raw_filter = _tokens(args.filter_local_ids, "--filter-local-ids")
    from rebar.config import reconciler_repo_root as _owned_repo_root

    root = Path(args.repo_root) if args.repo_root else _owned_repo_root()
    return ReconcileRequest(
        route=route,
        repo_root=root,
        target_mode=target_mode,
        max_changes=args.max_changes,
        selection_kind="only" if only else ("except" if excluded else None),
        selection_tokens=only or excluded,
        filter_local_ids=set(raw_filter) or None,
        dry_run_enumerate=args.dry_run_enumerate,
    )
