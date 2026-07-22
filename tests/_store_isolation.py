"""Behavioral helper for proving tests do not mutate an ambient ticket store."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import rebar


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def assert_nodes_do_not_mutate_external_store(tmp_path: Path, *node_ids: str) -> None:
    repo = tmp_path / "ambient-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "base"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    tracker = repo / ".tickets-tracker"
    before_head = _git_out(tracker, "rev-parse", "HEAD")
    before_dirs = {path.name for path in tracker.iterdir() if path.is_dir()}
    env = {**os.environ, "REBAR_ROOT": str(repo)}

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *node_ids],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git_out(tracker, "rev-parse", "HEAD") == before_head
    assert {path.name for path in tracker.iterdir() if path.is_dir()} == before_dirs
