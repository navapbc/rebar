"""The primary ``rebar bridge`` command group and reconciler launcher.

``bridge`` presents the stable operator actions for reconciliation, auditing,
access checks, and setup. Its parser
owns nested-command discovery and argument validation, then forwards the
validated canonical child arguments to the reconciler.
The launcher is shared with the established ``reconcile`` spelling so that
both entry points continue to use the same interpreter, repository discovery,
and engine environment.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from rebar._cli._parser import ParseError, render_parse_error

_BRIDGE_ROUTES = {"preview", "sync"}


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
    from rebar._cli._parsers.advanced import bridge as _bridge_parser

    return _bridge_parser.build(prog="rebar bridge")


def _group_help() -> str:
    """Load the pinned group help used by the top-level help dispatcher."""
    from rebar._cli import _help

    return _help.subcommand_help("bridge") or _parser().format_help()


def _launch_bridge_command(command: str, parsed: argparse.Namespace) -> int:
    """Launch the reconciler for a parser-validated bridge command."""
    if command not in _BRIDGE_ROUTES:
        raise AssertionError(f"unhandled bridge command: {command!r}")
    args = [command]
    if getattr(parsed, "max_changes", None) is not None:
        args += ["--max-changes", str(parsed.max_changes)]
    if getattr(parsed, "only", None) is not None:
        args += ["--only", parsed.only]
    if getattr(parsed, "except_ids", None) is not None:
        args += ["--except", parsed.except_ids]
    return launch_reconciler(args, default_mode=None)


def bridge_fsck_cli(argv: Sequence[str]) -> int:
    """Run the established bridge audit with its existing auto-init policy."""
    from rebar import config

    args = list(argv)
    is_help = bool(args and args[0] in {"--help", "-h"})
    if not is_help and not config.tracker_dir_override():
        from rebar._cli._init import ensure_initialized

        ensure_initialized(init_only=False)
    from rebar._engine_support import bridge_fsck

    return bridge_fsck.main(args)


def _passthrough(command: str, argv: Sequence[str]) -> int:
    """Route canonical bridge children through their established implementations."""
    if command == "fsck":
        return bridge_fsck_cli(argv)
    if command == "check-access":
        from rebar._cli import _bridge_probe

        return _bridge_probe(list(argv))
    if command == "setup":
        from rebar._cli._jira_onboard import jira_onboard

        return jira_onboard(list(argv), prog="rebar bridge setup")
    raise AssertionError(f"unhandled bridge pass-through command: {command!r}")


def _passthrough_help(command: str) -> int:
    """Render canonical child help without invoking an operational implementation."""
    if command == "fsck":
        text = (
            "Usage: rebar bridge fsck [--tickets-tracker=<path>] [--output json] "
            "[--repair] [--live-visibility]\n\n"
            "Audit committed event compatibility, binding drift, and forward/reverse "
            "binding-store integrity without Jira access.\n\n"
            "--repair prunes reverse bindings that have no forward entry "
            "(store_integrity / reverse_missing_forward). It is the only writing mode, "
            "refuses when any other integrity kind is present, and records a durable "
            "audit line. The audit itself never writes.\n\n"
            "--live-visibility additionally runs a READ-ONLY, ADVISORY live check that "
            "the mapped project keys + legacy_default are visible to the bridge bot, "
            "reusing the reconcile-pass visibility helper. It requires live Jira "
            "credentials (JIRA_URL / JIRA_USER / JIRA_API_TOKEN) and skips cleanly when "
            "they are absent. The advisory is written to stderr and never changes the "
            "exit code or the JSON output contract.\n"
        )
    elif command == "check-access":
        text = (
            "Usage: rebar bridge check-access\n\n"
            "Check live Jira access with a create/label/search/delete round-trip. "
            "Requires JIRA_URL, JIRA_USER, and JIRA_API_TOKEN; JIRA_PROJECT is optional.\n"
        )
    else:
        raise AssertionError(f"unhandled bridge help command: {command!r}")
    sys.stdout.write(text)
    return 0


def _ref_lock_module() -> ModuleType:
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
    import rebar
    from rebar._errors import RebarError

    try:
        if command == "resume":
            from rebar._lib_ops import _bridge_resume_operation

            _result, changed = _bridge_resume_operation()
            if not changed:
                sys.stdout.write("Bridge reconciliation is already resumed.\n")
            return 0
        rebar.bridge_pause(reason or "")
        return 0
    except (RebarError, ValueError) as exc:
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


def _run_profile(parsed: argparse.Namespace) -> int:
    """Render the packaged bridge runner's captured streams exactly once."""
    import rebar

    result = rebar.bridge_run(profile=parsed.profile)
    details = result["details"]
    stdout = details.get("stdout", "")
    stderr = details.get("stderr", "")
    if isinstance(stdout, str):
        sys.stdout.write(stdout)
    if isinstance(stderr, str):
        sys.stderr.write(stderr)
    return result["returncode"]


def _projects(parsed: argparse.Namespace) -> int:
    """Handle ``rebar bridge projects {list,set,remove}`` in-process."""
    import rebar
    from rebar._errors import RebarError

    verb = parsed.projects_verb
    if verb == "list":
        mapping = rebar.bridge_projects_list()
        sys.stdout.write(json.dumps(mapping) + "\n")
        return 0
    if verb == "set":
        repos = [r for r in parsed.repos.split(",") if r] if parsed.repos else []
        try:
            rebar.bridge_projects_set(parsed.key, repos)
        except RebarError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        return 0
    if verb == "remove":
        try:
            rebar.bridge_projects_remove(parsed.key)
        except RebarError as exc:
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        return 0
    raise AssertionError(f"unhandled projects verb: {verb!r}")


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
    if args[0] in {"fsck", "check-access", "setup"}:
        if args[0] != "setup" and args[1:] in (["--help"], ["-h"]):
            return _passthrough_help(args[0])
        return _passthrough(args[0], args[1:])

    parser = _parser()
    try:
        parsed = parser.parse_args(args)
    except ParseError as exc:
        return render_parse_error(exc)
    except SystemExit as exc:
        # A ``--help`` action still exits (argparse prints help, exit 0); only
        # parse *failures* are re-routed through ParseError above.
        exit_code = exc.code
        return 1 if exit_code is None else int(exit_code)

    if parsed.command in _BRIDGE_ROUTES:
        return _launch_bridge_command(parsed.command, parsed)
    if parsed.command == "run":
        return _run_profile(parsed)
    if parsed.command == "status":
        return _status(parsed)
    if parsed.command == "projects":
        return _projects(parsed)
    return _pause_or_resume(parsed.command, getattr(parsed, "reason", None))
