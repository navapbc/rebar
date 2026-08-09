"""The primary ``rebar bridge`` command group and reconciler launcher.

``bridge`` intentionally presents five stable operator actions. Its parser
owns nested-command discovery and argument validation, then forwards the
validated canonical child arguments to the reconciler.
The launcher is shared with the established ``reconcile`` spelling so that
both entry points continue to use the same interpreter, repository discovery,
and engine environment.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_BRIDGE_MODES = {"preview", "sync"}


def launch_reconciler(argv: Sequence[str], *, default_mode: str | None = "dry-run") -> int:
    """Run ``rebar_reconciler`` with repository defaults when they are absent.

    ``reconcile`` deliberately accepts its historical reconciler arguments, so
    this function retains that behavior. Canonical bridge commands forward their
    validated subcommand and options unchanged, without injecting a legacy mode.
    """
    from rebar import config
    from rebar._engine import engine_env

    root = str(config.repo_root())
    args = list(argv)
    if not any(arg == "--repo-root" or arg.startswith("--repo-root=") for arg in args):
        args += ["--repo-root", root]
    if default_mode is not None and not any(
        arg == "--mode" or arg.startswith("--mode=") for arg in args
    ):
        args += ["--mode", default_mode]
    # Use this interpreter rather than a bare ``python3``: the reconciler imports
    # ``rebar.*`` in-package, while engine_env exposes its top-level package.
    return subprocess.call([sys.executable, "-m", "rebar_reconciler", *args], env=engine_env(root))


def _parser() -> argparse.ArgumentParser:
    """Create the parser for the supported bridge operations."""
    parser = argparse.ArgumentParser(
        prog="rebar bridge",
        description="Synchronize rebar tickets with Jira.",
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="{preview,sync,status,pause,resume}",
    )
    preview = commands.add_parser(
        "preview",
        help="Show proposed Jira changes without applying them.",
        description="Show proposed Jira changes without applying them.",
    )
    sync = commands.add_parser(
        "sync",
        help="Apply proposed Jira changes.",
        description="Apply proposed Jira changes.",
    )
    for command in (preview, sync):
        selection = command.add_mutually_exclusive_group()
        selection.add_argument(
            "--only", metavar="IDS", help="Examine only these local IDs or bound Jira keys."
        )
        selection.add_argument(
            "--except",
            dest="except_ids",
            metavar="IDS",
            help="Exclude these local IDs or bound Jira keys.",
        )
    sync.add_argument(
        "--max-changes",
        type=_positive_int,
        metavar="N",
        help="Apply at most N proposed changes and retain an audit manifest.",
    )
    status = commands.add_parser(
        "status",
        help="Show the reconciler's durable status snapshot.",
        description="Show the reconciler's durable status snapshot.",
    )
    status.add_argument("--target", metavar="ENVIRONMENT_ID")
    status.add_argument("--max-age", type=_duration_seconds, metavar="DURATION")
    status.add_argument("--json", action="store_true", help="Emit the snapshot as JSON.")
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


def _positive_int(value: str) -> int:
    """Argparse converter for canonical mutation ceilings."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _duration_seconds(value: str) -> int:
    """Parse a positive integer duration with an optional s/m/h suffix."""
    units = {"s": 1, "m": 60, "h": 3600}
    suffix = value[-1:].lower()
    number = value[:-1] if suffix in units else value
    try:
        parsed = int(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive duration (for example 2h)") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive duration (for example 2h)")
    return parsed * units.get(suffix, 1)


def _group_help() -> str:
    """Load the pinned group help used by the top-level help dispatcher."""
    from rebar._cli import _help

    return _help.subcommand_help("bridge") or _parser().format_help()


def _launch_bridge_command(command: str, parsed: argparse.Namespace) -> int:
    """Launch the reconciler for a parser-validated bridge command."""
    if command not in _BRIDGE_MODES:
        raise AssertionError(f"unhandled bridge command: {command!r}")
    args = [command]
    if getattr(parsed, "max_changes", None) is not None:
        args += ["--max-changes", str(parsed.max_changes)]
    if getattr(parsed, "only", None) is not None:
        args += ["--only", parsed.only]
    if getattr(parsed, "except_ids", None) is not None:
        args += ["--except", parsed.except_ids]
    return launch_reconciler(args, default_mode=None)


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


def _status(parsed: argparse.Namespace) -> int:
    """Render the shared status-core snapshot in machine or operator form."""
    from rebar import config

    _ref_lock_module()  # bootstrap the engine package for the in-process status core
    last_pass = importlib.import_module("rebar_reconciler.last_pass")
    try:
        result = last_pass.snapshot(
            Path(config.repo_root()),
            target_environment_id=parsed.target,
            max_age_seconds=parsed.max_age,
        )
    except Exception as exc:  # noqa: BLE001 - one clean CLI error boundary
        sys.stderr.write(f"Error: cannot read bridge status: {exc}\n")
        return 1
    if parsed.json:
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    else:
        summary = result["verdict"]
        if result.get("pass_id"):
            summary += f" pass={result['pass_id']} environment={result['environment_id']}"
        if result.get("failure_kind"):
            summary += f" failure={result['failure_kind']}"
        sys.stdout.write(summary + "\n")
    return 0 if result["verdict"] in last_pass.HEALTHY_VERDICTS else 1


def bridge_cli(argv: Sequence[str]) -> int:
    """Run the primary bridge command group.

    A bare group invocation shows its compact command overview.  argparse
    handles nested discovery, each verb's help, and validation of its supported
    child arguments before forwarding canonical operations to the engine.
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
        return _launch_bridge_command(parsed.command, parsed)
    if parsed.command == "status":
        return _status(parsed)
    return _pause_or_resume(parsed.command, getattr(parsed, "reason", None))
