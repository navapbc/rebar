"""Packaged runner-neutral reconcile bridge orchestration.

This module owns the compatibility ``MODE`` profiles, provider result translation,
pause recognition, and strict ticket-store delivery.  It captures every subprocess
stream and returns a structured result; rendering belongs to the CLI so library and
MCP callers never write to stdout/stderr.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rebar import config
from rebar._proc import reap_process_group

if TYPE_CHECKING:
    from rebar.types import BridgeRun

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 3600
# Bounds for reaping a timed-out child's process group (``rebar._proc.reap_process_group``).
_REAP_GRACE_S = 10.0
_REAP_DRAIN_S = 5.0

#: Engine route argv per bridge profile; the runner spawns ``python -m rebar.reconciler``
#: directly so a timeout reaps the engine itself, not a CLI hop above it.
MODE_ARGS: Mapping[str, tuple[str, ...]] = {
    "dry-run": ("preview",),
    "bootstrap-strict": ("sync", "--max-changes", "10"),
    "bootstrap-throttle": ("sync", "--max-changes", "100"),
    "live": ("sync",),
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
_STATE_BY_EXIT: Mapping[int, tuple[str, int]] = {
    0: ("converged", 0),
    1: ("operational_failure", 1),
    2: ("invalid_invocation", 2),
}


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
    if profile not in MODE_ARGS:
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


def _engine_argv(profile: str, root: Path) -> tuple[str, ...]:
    return (sys.executable, "-m", "rebar.reconciler", *MODE_ARGS[profile], "--repo-root", str(root))


def _spawn_bounded(
    command: Sequence[str], *, root: Path, environ: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run *command* with captured output, reaping its whole process group on timeout.

    The child leads its own session on POSIX, so a timeout reaps the child AND anything
    it spawned through ``rebar._proc.reap_process_group`` before ``TimeoutExpired`` is
    re-raised for the caller's "timed out" branch.
    """
    proc = subprocess.Popen(  # raw-git-ok: closed rebar/Python argv only; never invokes git
        list(command),
        cwd=root,
        env=dict(environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        reap_process_group(
            proc, grace=_REAP_GRACE_S, drain=_REAP_DRAIN_S, label="bridge", logger=logger
        )
        raise
    return subprocess.CompletedProcess(list(command), proc.returncode, stdout, stderr)


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


def _benign_states() -> frozenset[str]:
    """The state names a ``BRIDGE_STATE:`` marker may carry (the engine's exit-0 states)."""
    guarded_pass = importlib.import_module("rebar.reconciler.guarded_pass")
    return frozenset(d.state for d in guarded_pass._Disposition if d.canonical_exit == 0)


def _classified_result(returncode: int, stderr: str) -> tuple[str, int]:
    """Name the state of one engine run without re-canonicalising its exit.

    The engine child canonicalises ``preview``/``sync`` exits once
    (``__main__._finish_disposition``), so the runner only reads the state name: a single
    ``BRIDGE_STATE:`` marker on an exit-0 run names a benign state; otherwise exit 0 is
    ``converged``, 1 ``operational_failure`` and 2 ``invalid_invocation``, propagated
    unchanged. An exit outside that contract (a signal death, a crash, a stray legacy
    3/4/75) is an ``operational_failure`` reported as 1 so ``BridgeRun.returncode`` stays
    in 0/1/2; the raw code is kept in ``details.reconcile_returncode``.
    """
    states = [
        line.removeprefix(_STATE_PREFIX)
        for line in stderr.splitlines()
        if line.startswith(_STATE_PREFIX)
    ]
    if returncode == 0 and len(states) == 1 and states[0] in _benign_states():
        return states[0], 0
    return _STATE_BY_EXIT.get(returncode, ("operational_failure", 1))


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
        reconciled = _spawn_bounded(_engine_argv(profile, root), root=root, environ=active_environ)
    except subprocess.TimeoutExpired:
        stderr += f"Reconcile failed: timed out after {TIMEOUT_SECONDS} seconds\n"
        return _result(profile, "operational_failure", 1, stdout=stdout, stderr=stderr)
    except OSError as exc:
        stderr += f"Reconcile failed: could not start the reconciler: {exc}\n"
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
        delivered = _spawn_bounded(
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
