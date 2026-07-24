"""Happy-path contracts for the git-derived and analyzer-derived metrics."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar.metrics import git_metrics

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def test_configured_positive():
    loc = {"files": {"a": 100, "b": 850}, "max_loc": 850}

    assert git_metrics.module_size_distribution(loc, 800, 0.1) == {
        "count": 2,
        "near_cap_count": 0,
        "over_cap_count": 1,
        "max_loc": 850,
    }
    oversized = getattr(git_metrics, "oversized_module_count", None)
    assert callable(oversized), "the analyzer-backed oversized metric must be public"
    assert oversized(loc, 800, 0.1) == 1


def test_refactor_to_addition_ratio(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    f = repo / "mod.py"
    f.write_text("\n".join(str(i) for i in range(100)) + "\n", encoding="utf-8")
    _commit(repo, "add 100 lines")  # 100 insertions
    f.write_text("\n".join(str(i) for i in range(60)) + "\n", encoding="utf-8")
    _commit(repo, "delete 40 lines")  # 40 deletions

    ratio = git_metrics.refactor_to_addition_ratio(str(repo), "2000-01-01", "2100-01-01")
    # deletions/insertions over the range = 40 / 100 = 0.4
    assert abs(ratio - 0.4) < 1e-6


def test_churn_sums_insertions_and_deletions(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    f = repo / "mod.py"
    f.write_text("\n".join(str(i) for i in range(100)) + "\n", encoding="utf-8")
    _commit(repo, "add 100 lines")
    f.write_text("\n".join(str(i) for i in range(60)) + "\n", encoding="utf-8")
    _commit(repo, "delete 40 lines")

    c = git_metrics.churn(str(repo), "2000-01-01", "2100-01-01")
    assert c["insertions"] == 100
    assert c["deletions"] == 40
