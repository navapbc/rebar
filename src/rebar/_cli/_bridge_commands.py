"""The staged ``rebar bridge`` command group and reconciler launcher.

``bridge`` intentionally presents only the two operator actions that are safe
to name as a stable command group.  Its parser owns nested-command discovery
and argument validation; it never forwards child arguments to the reconciler.
The launcher is shared with the established ``reconcile`` spelling so that
both entry points continue to use the same interpreter, repository discovery,
and engine environment.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence

_BRIDGE_MODES = {"preview": "dry-run", "sync": "live"}


def launch_reconciler(argv: Sequence[str], *, default_mode: str = "dry-run") -> int:
    """Run ``rebar_reconciler`` with repository defaults when they are absent.

    ``reconcile`` deliberately accepts its historical reconciler arguments, so
    this function retains that behavior.  The nested bridge commands pass no
    user-supplied child arguments and select their operation through
    ``default_mode`` instead.
    """
    from rebar import config
    from rebar._engine import engine_env

    root = str(config.repo_root())
    args = list(argv)
    if not any(arg == "--repo-root" or arg.startswith("--repo-root=") for arg in args):
        args += ["--repo-root", root]
    if not any(arg == "--mode" or arg.startswith("--mode=") for arg in args):
        args += ["--mode", default_mode]
    # Use this interpreter rather than a bare ``python3``: the reconciler imports
    # ``rebar.*`` in-package, while engine_env exposes its top-level package.
    return subprocess.call([sys.executable, "-m", "rebar_reconciler", *args], env=engine_env(root))


def _parser() -> argparse.ArgumentParser:
    """Create the parser for the two supported bridge operations."""
    parser = argparse.ArgumentParser(
        prog="rebar bridge",
        description="Run staged Jira synchronization.",
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="{preview,sync}",
    )
    commands.add_parser(
        "preview",
        help="Show proposed Jira changes without applying them.",
        description="Show proposed Jira changes without applying them.",
    )
    commands.add_parser(
        "sync",
        help="Apply the staged Jira synchronization.",
        description="Apply the staged Jira synchronization.",
    )
    return parser


def _group_help() -> str:
    """Load the pinned group help used by the top-level help dispatcher."""
    from rebar._cli import _help

    return _help.subcommand_help("bridge") or _parser().format_help()


def _launch_bridge_command(command: str) -> int:
    """Launch the reconciler for a parser-validated bridge command."""
    try:
        default_mode = _BRIDGE_MODES[command]
    except KeyError as exc:  # Defensive: argparse limits command to this mapping.
        raise AssertionError(f"unhandled bridge command: {command!r}") from exc
    return launch_reconciler((), default_mode=default_mode)


def bridge_cli(argv: Sequence[str]) -> int:
    """Run the staged bridge command group.

    A bare group invocation shows its compact command overview.  argparse
    handles nested discovery, each verb's help, and rejection of every child
    argument other than its standard help request.
    """
    args = list(argv)
    if not args:
        sys.stdout.write(_group_help())
        return 0

    parser = _parser()
    try:
        parsed = parser.parse_args(args)
    except SystemExit as exc:
        exit_code = exc.code
        return 1 if exit_code is None else int(exit_code)

    return _launch_bridge_command(parsed.command)
