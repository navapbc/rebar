"""Regression coverage for recursively removable pytest temp trees."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rebar.llm.workflow import snapshot

_SNAPSHOT_TEST = Path(__file__).parent / "workflow" / "test_snapshot.py"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_cross_module_snapshot_is_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path.parent / "adjacent.txt").write_text("keep\n")
    _git("init", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test User", cwd=repo)
    (repo / "a.py").write_text("x = 1\n")
    _git("add", "a.py", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)

    snapdir = snapshot.snapshot_at_ref("HEAD", str(repo))

    assert not os.access(snapdir / "a.py", os.W_OK)


@pytest.mark.parametrize(
    "selected_node",
    [
        f"{_SNAPSHOT_TEST}::test_snapshot_is_read_only",
        f"{Path(__file__)}::test_cross_module_snapshot_is_read_only",
    ],
    ids=["test_snapshot_is_read_only", "cross_module"],
)
def test_completed_snapshot_test_leaves_basetemp_removable(
    tmp_path: Path, selected_node: str
) -> None:
    basetemp = tmp_path / "nested-basetemp"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            selected_node,
            "--basetemp",
            str(basetemp),
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert basetemp.is_dir()
    if selected_node.endswith("::test_cross_module_snapshot_is_read_only"):
        assert (basetemp / "adjacent.txt").read_text() == "keep\n"

    shutil.rmtree(basetemp)
    assert not basetemp.exists()
