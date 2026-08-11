"""Held-out cross-surface and installed-package bridge-runner contracts."""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from adapters import _unwrap

import rebar

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
GITHUB = ROOT / ".github" / "workflows" / "reconcile-bridge.yml"
GITHUB_SETUP = ROOT / "docs" / "jira-sync-setup.md"
JENKINS = ROOT / "Jenkinsfile"
GITLAB = ROOT / ".gitlab-ci.yml"


def _harness():
    path = ROOT / "tests" / "scripts" / "test_run_reconcile_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_runner_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_environment(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_python_library_runs_the_packaged_bridge_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness()
    checkout, _tracker, origin = harness.bridge_workspace(tmp_path)
    env = harness.runner_env(tmp_path, checkout)
    env["REBAR_ROOT"] = str(tmp_path / "wrong-root")
    _install_environment(monkeypatch, env)

    result = rebar.bridge_run(repo_root=checkout)

    assert result["route"] == "run"
    assert result["state"] == "converged"
    assert result["returncode"] == 0
    assert result["details"]["profile"] == "live"
    assert result["details"]["delivery_attempted"] is True
    assert Path(env["REBAR_ROOT_FILE"]).read_text(encoding="utf-8").strip() == str(checkout)
    assert "Reconcile converged." in result["details"]["stdout"]
    assert harness.git(origin, "show", "tickets:bridge-event.txt").stdout == ("event from bridge\n")


def test_mcp_runs_the_same_packaged_core_without_writing_protocol_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rebar.mcp_server import build_server

    harness = _harness()
    checkout, _tracker, origin = harness.bridge_workspace(tmp_path)
    env = harness.runner_env(tmp_path, checkout)
    env["REBAR_ROOT"] = str(checkout)
    _install_environment(monkeypatch, env)
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    monkeypatch.setenv("REBAR_MCP_ALLOW_JIRA_SYNC", "1")
    capsys.readouterr()

    result = _unwrap(asyncio.run(build_server().call_tool("bridge_run", {"profile": "live"})))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert result["route"] == "run"
    assert result["state"] == "converged"
    assert result["returncode"] == 0
    assert harness.git(origin, "show", "tickets:bridge-event.txt").stdout == ("event from bridge\n")


def test_installed_cli_honors_rebar_root_when_started_outside_the_repo(
    tmp_path: Path,
) -> None:
    harness = _harness()
    checkout, _tracker, _origin = harness.bridge_workspace(tmp_path)
    env = harness.runner_env(tmp_path, checkout)

    completed = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "bridge", "run"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert Path(env["REBAR_ROOT_FILE"]).read_text(encoding="utf-8").strip() == str(checkout)


def test_provider_wrappers_invoke_only_the_installed_cli_runner() -> None:
    for path in (GITHUB, JENKINS, GITLAB):
        text = path.read_text(encoding="utf-8")
        assert text.count("rebar bridge run") == 1, path
        assert "scripts/run_reconcile_bridge.py" not in text, path


def test_copied_workflow_refresh_pairs_the_template_with_a_runner_capable_pin() -> None:
    setup = GITHUB_SETUP.read_text(encoding="utf-8")
    refresh = setup.split("### Refreshing existing copied templates", 1)[1].split("## 5.", 1)[0]
    normalized = " ".join(refresh.lower().split())

    assert "nava-rebar==X.Y.Z" in setup
    assert "atomically" in normalized
    assert "rebar bridge run" in refresh
    assert "inline workflows" in normalized


def test_wheel_carries_the_runner_for_pip_brew_and_mcp_installs(tmp_path: Path) -> None:
    hatchling_wheel = pytest.importorskip("hatchling.builders.wheel")
    builder = hatchling_wheel.WheelBuilder(str(ROOT))
    wheels = [Path(path) for path in builder.build(directory=str(tmp_path))]
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        assert "rebar/_bridge_runner.py" in names
        entry_points = next(
            archive.read(name).decode()
            for name in names
            if name.endswith(".dist-info/entry_points.txt")
        )
    assert "rebar = rebar.cli:main" in entry_points
    assert "rebar-mcp = rebar.mcp_server:main" in entry_points
