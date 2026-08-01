"""Exercise the production workflow's merge-driver setup in real Git repositories."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "reconcile-bridge.yml"
_BINDINGS = Path(".bridge_state/bindings.json")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _workflow_driver_command() -> list[str]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    commands = [
        line.strip()
        for step in workflow["jobs"]["reconcile"]["steps"]
        for line in step.get("run", "").splitlines()
        if line.strip().startswith("git config merge.ours.driver ")
    ]
    assert len(commands) == 1, (
        "production reconcile workflow must directly configure merge.ours.driver exactly once"
    )
    return shlex.split(commands[0])


def _seed_origin(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "Test Runner")
    _git(source, "config", "user.email", "runner@example.test")
    (source / "README.md").write_text("main\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-q", "-m", "main")

    _git(source, "checkout", "-q", "--orphan", "tickets")
    (source / "README.md").unlink(missing_ok=True)
    (source / ".gitattributes").write_text(".bridge_state/* merge=ours\n", encoding="utf-8")
    bindings = source / _BINDINGS
    bindings.parent.mkdir()
    bindings.write_text('{"owner":"base"}\n', encoding="utf-8")
    _git(source, "add", ".gitattributes", str(_BINDINGS))
    _git(source, "commit", "-q", "-m", "seed tickets")
    _git(source, "checkout", "-q", "main")
    return source


def _runner_clone(tmp_path: Path, name: str, source: Path) -> tuple[Path, Path]:
    runner = tmp_path / name
    subprocess.run(
        ["git", "clone", "-q", str(source), str(runner)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(runner, "config", "user.name", "Test Runner")
    _git(runner, "config", "user.email", "runner@example.test")
    _git(runner, "fetch", "origin", "+tickets:refs/remotes/origin/tickets")
    tracker = runner / ".tickets-tracker"
    _git(runner, "worktree", "add", "-q", "-B", "tickets", str(tracker), "origin/tickets")
    return runner, tracker


def _diverge_and_merge(tracker: Path) -> subprocess.CompletedProcess[str]:
    bindings = tracker / _BINDINGS
    base = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    _git(tracker, "checkout", "-q", "-b", "remote-pass")
    bindings.write_text('{"owner":"remote"}\n', encoding="utf-8")
    _git(tracker, "commit", "-qam", "remote rewrite")
    remote_tip = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    _git(tracker, "checkout", "-q", "tickets")
    bindings.write_text('{"owner":"local"}\n', encoding="utf-8")
    _git(tracker, "commit", "-qam", "local rewrite")
    local_tip = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    assert local_tip != remote_tip
    assert _git(tracker, "merge-base", "tickets", "remote-pass").stdout.strip() == base
    assert bindings.read_text(encoding="utf-8") == '{"owner":"local"}\n'
    return _git(tracker, "merge", "--no-edit", "remote-pass", check=False)


def test_workflow_driver_makes_diverged_bridge_state_local_wins(tmp_path: Path) -> None:
    source = _seed_origin(tmp_path)

    _, unconfigured = _runner_clone(tmp_path, "negative-control", source)
    ordinary_merge = _diverge_and_merge(unconfigured)
    assert ordinary_merge.returncode != 0
    conflicted = (unconfigured / _BINDINGS).read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" in conflicted and ">>>>>>> remote-pass" in conflicted
    _git(unconfigured, "merge", "--abort")

    runner, configured = _runner_clone(tmp_path, "configured", source)
    subprocess.run(_workflow_driver_command(), cwd=runner, check=True)
    local_wins_merge = _diverge_and_merge(configured)

    assert local_wins_merge.returncode == 0, local_wins_merge.stderr
    result = (configured / _BINDINGS).read_text(encoding="utf-8")
    assert result == '{"owner":"local"}\n'
    assert "<<<<<<<" not in result and ">>>>>>>" not in result
    assert _git(configured, "status", "--porcelain").stdout == ""
