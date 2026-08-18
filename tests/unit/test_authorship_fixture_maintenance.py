"""The real-history authorship fixture must not launch detached Git upkeep."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit


def _local_config(repo: Path, key: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get", key],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "<absent>"


def test_authorship_history_fixture_keeps_auto_maintenance_foreground(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    target = repo_root / "tests/unit/test_authorship_batched_ticket_map_7084.py"
    global_config = tmp_path / "hostile-global-gitconfig"
    trace = tmp_path / "trace2.json"
    basetemp = tmp_path / "nested-pytest"
    global_config.write_text("[gc]\n\tautoDetach = true\n[maintenance]\n\tautoDetach = true\n")
    env = subprocess_env()
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TRACE2_EVENT": str(trace),
        }
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{target}::test_batched_map_matches_the_per_event_resolver_for_every_event",
            "-q",
            "-n",
            "0",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(basetemp),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

    trackers = [path.parent for path in basetemp.rglob("tracker/.git")]
    assert len(trackers) == 1, trackers
    tracker = trackers[0]

    maintenance_children = []
    for line in trace.read_text().splitlines():
        event = json.loads(line)
        argv = event.get("argv", [])
        if event.get("event") == "child_start" and argv[:3] == ["git", "maintenance", "run"]:
            maintenance_children.append(argv)

    assert maintenance_children, "fixture did not exercise Git auto-maintenance"
    assert all("--no-detach" in argv for argv in maintenance_children), maintenance_children
    assert _local_config(tracker, "gc.autoDetach") == "false"
    assert _local_config(tracker, "maintenance.autoDetach") == "false"
