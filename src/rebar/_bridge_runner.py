"""Packaged runner-neutral reconcile bridge orchestration.

This module owns the compatibility ``MODE`` profiles, provider result translation,
pause recognition, and strict ticket-store delivery.  It captures every subprocess
stream and returns a structured result; rendering belongs to the CLI so library and
MCP callers never write to stdout/stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rebar import config

if TYPE_CHECKING:
    from rebar.types import BridgeRun

TIMEOUT_SECONDS = 3600

MODE_COMMANDS: Mapping[str, tuple[str, ...]] = {
    "reconcile-check": ("rebar", "bridge", "preview"),
    "dry-run": ("rebar", "bridge", "preview"),
    "bootstrap-strict": ("rebar", "bridge", "sync", "--max-changes", "10"),
    "bootstrap-throttle": ("rebar", "bridge", "sync", "--max-changes", "100"),
    "live": ("rebar", "bridge", "sync"),
}

REQUIRED_ENV_NAMES = (
    "MODE",
    "BRIDGE_RUN_ID",
    "BRIDGE_BOT_NAME",
    "BRIDGE_BOT_EMAIL",
    "JIRA_URL",
    "JIRA_USER",
    "JIRA_API_TOKEN",
    "JIRA_PROJECT",
    "REBAR_ENV_ID",
)
_PAUSE_PREFIX = "BRIDGE_PAUSED: "
_PAUSE_KEYS = ("paused", "reason", "who", "paused_at")
_STATE_PREFIX = "BRIDGE_STATE: "


def _result(
    profile: str,
    state: str,
    returncode: int,
    *,
    stdout: str = "",
    stderr: str = "",
    delivery_attempted: bool = False,
    **details: Any,
) -> BridgeRun:
    payload = {
        "route": "run",
        "state": state,
        "returncode": returncode,
        "details": {
            "profile": profile,
            "stdout": stdout,
            "stderr": stderr,
            "delivery_attempted": delivery_attempted,
            **details,
        },
    }
    return cast("BridgeRun", payload)


def _validate_environment(
    requested_profile: str | None, environ: Mapping[str, str]
) -> tuple[str, str | None]:
    profile = requested_profile if requested_profile is not None else environ.get("MODE", "")
    required_without_profile = REQUIRED_ENV_NAMES[1:]
    missing = [name for name in required_without_profile if not environ.get(name, "").strip()]
    if not profile.strip():
        missing.insert(0, "MODE")
    if missing:
        return profile, "Reconcile bridge configuration error: missing required environment: " + (
            ", ".join(missing)
        )
    if profile not in MODE_COMMANDS:
        return profile, f"Unknown bridge profile: {profile}"
    return profile, None


def _validate_checkout(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), "rev-parse", "--is-shallow-repository"),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Reconcile bridge configuration error: cannot inspect checkout history: {exc}"
    if completed.returncode != 0:
        return "Reconcile bridge configuration error: the repository root is not a Git checkout"
    if completed.stdout.strip() == "true":
        return (
            "Reconcile bridge configuration error: shallow checkout detected; "
            "reconcile bridge requires full history (fetch-depth: 0)"
        )
    return None


def _run(
    command: Sequence[str], *, root: Path, environ: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # raw-git-ok: closed rebar/Python argv only; never invokes git
        command,
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def _is_valid_pause_marker(stderr: str) -> bool:
    markers = [
        line.removeprefix(_PAUSE_PREFIX)
        for line in stderr.splitlines()
        if line.startswith(_PAUSE_PREFIX)
    ]
    if len(markers) != 1:
        return False
    try:
        payload = json.loads(markers[0])
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and tuple(payload) == _PAUSE_KEYS
        and payload["paused"] is True
        and all(isinstance(payload[key], str) and bool(payload[key]) for key in _PAUSE_KEYS[1:])
    )


def _dispositions() -> tuple[Any, ...]:
    """Load the reconciler classification once through its supported package seam."""
    from rebar._lib_ops import _engine_module

    orchestrator = _engine_module("rebar_reconciler.__main__")
    return tuple(orchestrator._Disposition)


def _classified_result(returncode: int, stderr: str) -> tuple[str, int]:
    dispositions = _dispositions()
    by_state = {disposition.state: disposition for disposition in dispositions}
    if returncode == 0:
        states = [
            line.removeprefix(_STATE_PREFIX)
            for line in stderr.splitlines()
            if line.startswith(_STATE_PREFIX)
        ]
        if len(states) == 1 and states[0] in by_state:
            disposition = by_state[states[0]]
            return disposition.state, disposition.canonical_exit
    for disposition in dispositions:
        if disposition.legacy_exit == returncode:
            return disposition.state, disposition.canonical_exit
    return "operational_failure", 1


def _delivery_command(environ: Mapping[str, str], root: Path) -> tuple[str, ...]:
    """The delivery child's argv. ``--tracker`` carries the RESOLVED ABSOLUTE store path
    (``rebar.config.tracker_dir``): the child runs with ``cwd=root``, so the relative
    default name this used to pass silently delivered to ``<root>/.tickets-tracker`` on
    any deployment whose store has been relocated."""
    return (
        sys.executable,
        "-m",
        "rebar._store.push",
        "commit-and-push",
        "--tracker",
        str(config.tracker_dir(root)),
        "--message",
        f"chore: sync events from rebar reconciler [run {environ['BRIDGE_RUN_ID']}]",
        "--strict",
        "--author-name",
        environ["BRIDGE_BOT_NAME"],
        "--author-email",
        environ["BRIDGE_BOT_EMAIL"],
    )


def run_bridge(
    profile: str | None = None,
    *,
    repo_root: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> BridgeRun:
    """Run one bridge profile and return captured provider-neutral output."""
    active_environ = dict(os.environ if environ is None else environ)
    profile, environment_error = _validate_environment(profile, active_environ)
    if environment_error is not None:
        return _result(profile, "invalid_invocation", 2, stderr=environment_error + "\n")

    root_override = repo_root if repo_root is not None else active_environ.get("REBAR_ROOT")
    root = Path(config.repo_root(root_override))
    active_environ["REBAR_ROOT"] = str(root)
    checkout_error = _validate_checkout(root)
    if checkout_error is not None:
        return _result(profile, "invalid_invocation", 2, stderr=checkout_error + "\n")

    stdout = f"Reconciling with bridge profile: {profile}\n"
    stderr = ""
    try:
        reconciled = _run(MODE_COMMANDS[profile], root=root, environ=active_environ)
    except subprocess.TimeoutExpired:
        stderr += f"Reconcile failed: timed out after {TIMEOUT_SECONDS} seconds\n"
        return _result(profile, "operational_failure", 1, stdout=stdout, stderr=stderr)
    except OSError as exc:
        stderr += f"Reconcile failed: could not start rebar: {exc}\n"
        return _result(profile, "operational_failure", 1, stdout=stdout, stderr=stderr)

    stdout += reconciled.stdout
    stderr += reconciled.stderr
    state, provider_exit = _classified_result(reconciled.returncode, reconciled.stderr)
    if provider_exit != 0:
        stderr += f"Reconcile failed (exit {reconciled.returncode})\n"
        return _result(
            profile,
            state,
            provider_exit,
            stdout=stdout,
            stderr=stderr,
            reconcile_returncode=reconciled.returncode,
        )
    if _is_valid_pause_marker(reconciled.stderr):
        stdout += "Reconcile bridge is paused; skipping ticket commit and push.\n"
        return _result(
            profile,
            "paused",
            0,
            stdout=stdout,
            stderr=stderr,
            reconcile_returncode=reconciled.returncode,
        )

    # RP-04 S3 (AC4): the delivery child (git commit + push) needs NO Jira send credential,
    # so project active_environ as an "unrelated" sibling — stripping every adapter-owned
    # secret name (JIRA_API_TOKEN / JIRA_PAT) while preserving all native config. Pure: never
    # mutates active_environ (which may be os.environ or a plain dict).
    from rebar import _child_env

    delivery_environ = _child_env.project_child_env(active_environ, relationship="unrelated")
    delivery_environ["REBAR_SYNC_PUSH"] = "always"
    try:
        delivered = _run(
            _delivery_command(active_environ, root), root=root, environ=delivery_environ
        )
    except subprocess.TimeoutExpired:
        stderr += f"Bridge delivery failed: timed out after {TIMEOUT_SECONDS} seconds\n"
        return _result(
            profile,
            "operational_failure",
            1,
            stdout=stdout,
            stderr=stderr,
            delivery_attempted=True,
            reconcile_returncode=reconciled.returncode,
        )
    except OSError as exc:
        stderr += f"Bridge delivery failed: could not start Python: {exc}\n"
        return _result(
            profile,
            "operational_failure",
            1,
            stdout=stdout,
            stderr=stderr,
            delivery_attempted=True,
            reconcile_returncode=reconciled.returncode,
        )

    stdout += delivered.stdout
    stderr += delivered.stderr
    if delivered.returncode != 0:
        stderr += f"Bridge delivery failed (exit {delivered.returncode})\n"
        return _result(
            profile,
            "operational_failure",
            1,
            stdout=stdout,
            stderr=stderr,
            delivery_attempted=True,
            reconcile_returncode=reconciled.returncode,
            delivery_returncode=delivered.returncode,
        )

    stdout += "Reconcile converged.\n"
    return _result(
        profile,
        state,
        0,
        stdout=stdout,
        stderr=stderr,
        delivery_attempted=True,
        reconcile_returncode=reconciled.returncode,
        delivery_returncode=delivered.returncode,
    )
