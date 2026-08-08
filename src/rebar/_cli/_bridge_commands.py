"""The staged ``rebar bridge`` command group and reconciler launcher.

``bridge`` intentionally presents four stable operator actions. Its parser
owns nested-command discovery and argument validation; it never forwards child
arguments to the reconciler.
The launcher is shared with the established ``reconcile`` spelling so that
both entry points continue to use the same interpreter, repository discovery,
and engine environment.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import importlib.util
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

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
    """Create the parser for the supported bridge operations."""
    parser = argparse.ArgumentParser(
        prog="rebar bridge",
        description="Run staged Jira synchronization.",
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="{preview,sync,pause,resume}",
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
    pause = commands.add_parser(
        "pause",
        help="Temporarily stop scheduled reconciliation.",
        description="Temporarily stop scheduled reconciliation.",
    )
    pause.add_argument("reason", metavar="REASON")
    commands.add_parser(
        "resume",
        help="Resume scheduled reconciliation.",
        description="Resume scheduled reconciliation.",
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


def _pause_remote(root: str) -> str:
    """Resolve the configured tickets remote for the reconciler control ref."""
    from rebar.config import tickets_remote

    return tickets_remote(root)


def _ref_lock_module():
    """Load the engine-scoped ref-lock module for the bridge control verbs."""
    from rebar._engine import engine_dir

    package = "rebar_reconciler"
    if package not in sys.modules:
        package_dir = engine_dir() / package
        spec = importlib.util.spec_from_file_location(
            package, package_dir / "__init__.py", submodule_search_locations=[str(package_dir)]
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{package}._ref_lock")


def _pause_or_resume(command: str, reason: str | None = None) -> int:
    """Set or clear the reconciler pause ref through its observed-OID CAS API."""
    from rebar import config

    root = str(config.repo_root())
    remote = _pause_remote(root)
    if subprocess.run(
        ["git", "-C", root, "remote", "get-url", remote], capture_output=True, check=False
    ).returncode:
        sys.stderr.write(f"Error: bridge pause/resume requires configured remote {remote!r}\n")
        return 1
    ref_lock = _ref_lock_module()
    try:
        if command == "resume":
            if not ref_lock.clear_gate(Path(root), remote=remote):
                sys.stdout.write("Bridge reconciliation is already resumed.\n")
            return 0
        from rebar._commands.identity import _git_email

        who = _git_email(root)
        if who is None:
            sys.stderr.write("Error: bridge pause requires a configured git user.email\n")
            return 1
        paused_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        ref_lock.set_pause(
            Path(root),
            reason=reason or "",
            who=who,
            paused_at=paused_at.isoformat().replace("+00:00", "Z"),
            remote=remote,
        )
        return 0
    except (ref_lock.RefLockError, ref_lock.RefLockTimeoutError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1


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

    if parsed.command in _BRIDGE_MODES:
        return _launch_bridge_command(parsed.command)
    return _pause_or_resume(parsed.command, getattr(parsed, "reason", None))
