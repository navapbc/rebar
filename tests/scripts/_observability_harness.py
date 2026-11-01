# mechanism-ok: test_helper _observability_harness — 7f3d-ddc9-04d9-4861: one isolation door
# for every whole-script observability test, replacing per-file partial redirection.
"""Run ``infra/scripts/observability.sh`` in tests without touching the host.

The script keeps its persistent state under ``/var/lib/rebar`` and probes the host's Docker
daemon. A test that inherits either reads or overwrites a real host's state, or waits on a
real daemon that the script bounds only where coreutils ``timeout`` exists (not on stock
macOS). Every whole-script run goes through :func:`run_observability` with an environment
built by :func:`observability_env`; ``test_observability_harness_isolation.py`` enforces it.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from _subprocess_env import SubprocessEnv, subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
HOST_STATE_ROOT = "/var/lib/rebar"

# `NAME="${NAME:-/var/lib/rebar/...}"` — the script's only sanctioned way to name host state.
_STATE_DEFAULT = re.compile(
    r'^(?P<name>[A-Z][A-Z0-9_]*)="\$\{(?P=name):-(?P<default>'
    + re.escape(HOST_STATE_ROOT)
    + r'[^}]*)\}"',
    re.MULTILINE,
)


@cache
def state_path_defaults(script: Path = SCRIPT) -> dict[str, str]:
    """Return every ``NAME -> /var/lib/rebar/...`` default the script declares."""
    return {m["name"]: m["default"] for m in _STATE_DEFAULT.finditer(script.read_text())}


def observability_env(tmp_path: Path, overrides: Mapping[str, str] | None = None) -> SubprocessEnv:
    """Return an ambient-derived env with all host state under ``tmp_path`` and docker stubbed.

    Every state variable points at ``tmp_path/host-state`` (the mirror of ``/var/lib/rebar``).
    A fallback ``docker`` that reports the daemon unavailable sits ahead of the host ``PATH``;
    a test that prepends its own ``bin/`` keeps any ``docker`` stub it writes there.
    """
    state_root = tmp_path / "host-state"
    state_root.mkdir(parents=True, exist_ok=True)
    fallback_bin = tmp_path / "host-stubs"
    fallback_bin.mkdir(parents=True, exist_ok=True)
    docker = fallback_bin / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 1\n")
    docker.chmod(0o755)

    env = subprocess_env()
    for name, default in state_path_defaults().items():
        env[name] = str(state_root / default.removeprefix(HOST_STATE_ROOT).lstrip("/"))
    # Derived from REGION_CACHE's directory in the script; set it outright so the redirect
    # does not hang on that derivation staying as it is.
    env["INSTANCE_ID_CACHE"] = str(Path(env["REGION_CACHE"]).parent / "probe-instance-id")
    env["PATH"] = f"{fallback_bin}{os.pathsep}{env.get('PATH', '')}"
    env.update(overrides or {})
    return env


def unredirected_state(env: Mapping[str, str]) -> dict[str, str]:
    """Return ``NAME -> path`` for every state variable that would still reach the host."""
    reached: dict[str, str] = {}
    for name, default in state_path_defaults().items():
        value = env.get(name, default)
        if value == HOST_STATE_ROOT or value.startswith(HOST_STATE_ROOT + "/"):
            reached[name] = value
    return reached


def run_observability(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    capture_output: bool = False,
    text: bool = False,
) -> subprocess.CompletedProcess[Any]:
    """Run the script and reap its whole process group, including on timeout.

    Refuses, before starting anything, an env in which any state variable still resolves
    under ``/var/lib/rebar`` (naming each one) or ``docker`` resolves to the host's binary.
    """
    reached = unredirected_state(env)
    assert not reached, (
        "observability.sh would reach host state; build the env with observability_env(): "
        + ", ".join(f"{name}={path}" for name, path in sorted(reached.items()))
    )
    host_docker = shutil.which("docker")
    assert host_docker is None or shutil.which("docker", path=env.get("PATH")) != host_docker, (
        f"observability.sh would call the host's docker ({host_docker}); build the env with "
        "observability_env() or stub docker in the test's bin/"
    )
    pipe = subprocess.PIPE if capture_output else None
    # A new session puts the script and every subshell it forks in one process group, so a
    # timeout can signal all of them; killing only the direct child orphans the rest.
    with subprocess.Popen(
        list(argv), env=dict(env), stdout=pipe, stderr=pipe, text=text, start_new_session=True
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except BaseException:
            # Timeout or interrupt: the group is still alive, so its id cannot be reused yet.
            _kill_group(proc.pid)
            proc.communicate()
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
