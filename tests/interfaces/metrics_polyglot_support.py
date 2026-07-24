"""Test support for real-CLI code-health fixture probes."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "metrics_polyglot"


def copy_project(tmp_path: Path, language: str) -> Path:
    """Copy one immutable polyglot fixture into a disposable repository."""

    source = _FIXTURES / language
    assert source.is_dir(), f"missing polyglot fixture: {source}"
    project = tmp_path / language
    shutil.copytree(source, project)
    _initialize_project(project)
    return project


def _initialize_project(project: Path) -> None:
    """Create the disposable repository and ticket store required by the CLI."""

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            command,
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return completed

    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "fixture@example.test"],
        ["git", "config", "user.name", "Metrics Fixture"],
        ["git", "add", "--", "pyproject.toml", "src"],
        ["git", "commit", "-qm", "Add fixture project"],
        [sys.executable, "-m", "rebar.cli", "init"],
    ):
        run(command)

    created = run(
        [
            sys.executable,
            "-m",
            "rebar.cli",
            "create",
            "task",
            "Exercise metrics fixture",
            "--output",
            "json",
        ]
    )
    ticket_id = json.loads(created.stdout)["id"]
    run(
        [
            sys.executable,
            "-m",
            "rebar.cli",
            "claim",
            ticket_id,
            "--assignee",
            "fixture@example.test",
        ]
    )


def fake_analyzer_bin(tmp_path: Path) -> Path:
    """Install deterministic process-level stand-ins for external analyzers."""

    bin_dir = tmp_path / "analyzer-bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "scc",
        """#!/usr/bin/env python3
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[-1]).resolve()
files = []
for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
    code = len(path.read_text(encoding="utf-8").splitlines())
    files.append({"Location": str(path.resolve()), "Code": code})
print(json.dumps([{"Name": "Fixture", "Files": files}]))
""",
    )
    _write_executable(
        bin_dir / "jscpd",
        """#!/usr/bin/env python3
import json
import pathlib
import sys

output = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])
report = {"statistics": {"total": {"clones": 1, "percentage": 12.5}}}
(output / "jscpd-report.json").write_text(json.dumps(report), encoding="utf-8")
""",
    )
    return bin_dir


def git_only_bin(tmp_path: Path) -> Path:
    """Return a PATH directory containing git but no external analyzers."""

    git = shutil.which("git")
    assert git is not None, "interface tests require git"
    bin_dir = tmp_path / "git-only-bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(git)
    return bin_dir


def run_metrics(project: Path, *, path: str) -> dict[str, Any]:
    """Run the public CLI in a subprocess and return its metric document."""

    env = os.environ.copy()
    env["PATH"] = path
    completed = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "metrics", "--output", "json"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)["metrics"]


def with_path_prefix(bin_dir: Path) -> str:
    """Return a PATH that resolves fixture analyzers before ambient tools."""

    return os.pathsep.join((str(bin_dir), os.environ.get("PATH", "")))


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
