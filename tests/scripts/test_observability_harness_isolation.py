"""Keep every whole-script observability test off the host's state and processes.

``observability.sh`` defaults its persistent state to ``/var/lib/rebar``. A test that leaves one
of those paths unredirected reads or overwrites a real host's state; one that runs the script
directly orphans its subshells when the run times out. Each guard here names the offending
variable, line, or call site, so a regression fails in seconds rather than as a 60 s timeout.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from _observability_harness import (
    HOST_STATE_ROOT,
    SCRIPT,
    observability_env,
    run_observability,
    state_path_defaults,
)

pytestmark = pytest.mark.unit

TESTS_DIR = Path(__file__).resolve().parent
_SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output"}

# The 17 defaults present when the harness was introduced. A missing one means the parse broke.
_KNOWN_STATE_VARIABLES = {
    "GATE_SCRATCH_MOUNT",
    "GATE_SCRATCH_VOLUME_ID_FILE",
    "REGION_CACHE",
    "REVIEWBOT_WEBHOOK_AUTH_OFFSET_FILE",
    "REPL_OFFSET_FILE",
    "VOTER_OFFSET_FILE",
    "MERGE_OFFSET_FILE",
    "DEPLOY_OFFSET_FILE",
    "DEFER_OFFSET_FILE",
    "INTERRUPT_OFFSET_FILE",
    "INTERRUPT_BOUND_OFFSET_FILE",
    "INTERRUPT_SIGNAL_OFFSET_FILE",
    "DISK_PRESSURE_OFFSET_FILE",
    "DISK_PRESSURE_PERSIST_OFFSET_FILE",
    "MCP_RETIRE_CAP_OFFSET_FILE",
    "MCP_MEM_ABORT_OFFSET_FILE",
    "G2P_OFFSET_FILE",
}


def test_every_known_state_default_is_parsed() -> None:
    missing = _KNOWN_STATE_VARIABLES - state_path_defaults().keys()
    assert not missing, f"state_path_defaults() no longer finds: {sorted(missing)}"


def test_the_parse_covers_every_host_state_line() -> None:
    lines = [
        line
        for line in SCRIPT.read_text().splitlines()
        if HOST_STATE_ROOT in line and not line.lstrip().startswith("#")
    ]
    assert len(state_path_defaults()) == len(lines), (len(state_path_defaults()), len(lines))


def test_the_instance_id_cache_follows_the_redirected_region_cache(tmp_path: Path) -> None:
    """INSTANCE_ID_CACHE carries no literal default; it lives beside REGION_CACHE."""
    assert 'INSTANCE_ID_CACHE="${INSTANCE_ID_CACHE:-${REGION_CACHE_DIR}/' in SCRIPT.read_text(), (
        "the script no longer derives INSTANCE_ID_CACHE from REGION_CACHE's directory; "
        "revisit how observability_env redirects it"
    )
    env = observability_env(tmp_path)
    assert Path(env["INSTANCE_ID_CACHE"]).parent == Path(env["REGION_CACHE"]).parent
    assert env["INSTANCE_ID_CACHE"].startswith(str(tmp_path))


def test_every_host_state_literal_is_an_overridable_default() -> None:
    """A hard-coded ``/var/lib/rebar`` path cannot be redirected, so it must not exist."""
    offenders = []
    for number, line in enumerate(SCRIPT.read_text().splitlines(), start=1):
        if line.lstrip().startswith("#") or HOST_STATE_ROOT not in line:
            continue
        name = line.split("=", 1)[0]
        if name not in state_path_defaults() or not line.startswith(f'{name}="${{{name}:-'):
            offenders.append(f"{SCRIPT.name}:{number}: {line.strip()}")
    assert not offenders, (
        f'these lines name {HOST_STATE_ROOT} outside a NAME="${{NAME:-...}}" default, so '
        "tests cannot redirect them:\n" + "\n".join(offenders)
    )


def test_observability_env_redirects_every_state_path(tmp_path: Path) -> None:
    env = observability_env(tmp_path)
    for name in state_path_defaults():
        assert env[name].startswith(str(tmp_path)), f"{name}={env[name]} escapes tmp_path"


def _no_process(*args: object, **kwargs: object) -> None:
    raise AssertionError("run_observability started a process before refusing the env")


def test_an_unredirected_state_path_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused before anything starts, so the failure is immediate and names the variable."""
    env = observability_env(tmp_path)
    del env["REGION_CACHE"]
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", _no_process)
        with pytest.raises(AssertionError, match=r"REGION_CACHE=/var/lib/rebar/probe-region"):
            run_observability(["bash", str(SCRIPT)], env=env, timeout=60)


def test_the_host_docker_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host_docker = shutil.which("docker")
    if host_docker is None:
        host_bin = tmp_path / "host-bin"
        host_bin.mkdir()
        (host_bin / "docker").write_text("#!/usr/bin/env bash\nexit 0\n")
        (host_bin / "docker").chmod(0o755)
        monkeypatch.setenv("PATH", f"{host_bin}{os.pathsep}{os.environ['PATH']}")
    env = observability_env(tmp_path)
    env["PATH"] = env["PATH"].split(os.pathsep, 1)[1]  # drop the fallback stub directory
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", _no_process)
        with pytest.raises(AssertionError, match="host's docker"):
            run_observability(["bash", str(SCRIPT)], env=env, timeout=60)


def _script_names(tree: ast.Module) -> set[str]:
    """Names bound anywhere in a module to the probe script or a copy of it."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            source = ast.unparse(node.value)
            if "observability.sh'" in source and "install-observability.sh" not in source:
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _argv_items(argv: ast.expr) -> list[ast.expr]:
    return list(argv.elts) if isinstance(argv, (ast.List, ast.Tuple)) else [argv]


def _names_script(item: ast.expr, script_names: set[str]) -> bool:
    """``SCRIPT`` or ``str(SCRIPT)`` — the script itself, not a sibling under its directory."""
    if isinstance(item, ast.Call) and ast.unparse(item.func) == "str" and len(item.args) == 1:
        item = item.args[0]
    return isinstance(item, ast.Name) and item.id in script_names


def _direct_script_runs(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    script_names = _script_names(tree)
    hits = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SUBPROCESS_CALLS
            and ast.unparse(node.func.value) == "subprocess"
            and node.args
        ):
            continue
        if any(_names_script(item, script_names) for item in _argv_items(node.args[0])):
            hits.append(f"{path.name}:{node.lineno}: {ast.unparse(node.func)}")
    return hits


def test_no_test_runs_the_script_outside_the_harness() -> None:
    offenders = [
        hit
        for path in sorted(TESTS_DIR.glob("test_*.py"))
        if path.name != Path(__file__).name
        for hit in _direct_script_runs(path)
    ]
    assert not offenders, (
        "run observability.sh through _observability_harness.run_observability so its state "
        "is redirected and a timeout reaps the whole process tree:\n" + "\n".join(offenders)
    )


def test_the_scan_detects_a_direct_run(tmp_path: Path) -> None:
    """ANTI-VACUITY: the scan above must flag the pattern it exists to forbid."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        textwrap.dedent(
            """
            SCRIPT = ROOT / "infra" / "scripts" / "observability.sh"
            def test_x():
                subprocess.run(["bash", str(SCRIPT)], env={}, timeout=60)
            """
        )
    )
    assert _direct_script_runs(sample) == ["test_sample.py:4: subprocess.run"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_timeout_leaves_no_process_behind(tmp_path: Path) -> None:
    """A stalled probe's grandchild must die with the script, not reparent to PID 1."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "grandchild.pid"
    stall = bin_dir / "curl"
    stall.write_text(f'#!/usr/bin/env bash\nsleep 120 &\necho $! > "{pid_file}"\nwait\n')
    stall.chmod(0o755)
    env = observability_env(tmp_path)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    with pytest.raises(subprocess.TimeoutExpired):
        run_observability(["bash", str(SCRIPT)], env=env, timeout=3)

    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild), f"pid {grandchild} survived the timed-out run"
