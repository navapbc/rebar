"""Happy-path contract for the git-derivation code-health readers (ticket 7931).

Tier: unit (real temp git repo). Pins the core derivations under the CURRENT
module-size mechanism (a single flat cap in .github/module-size-limit.txt — the
old ceilings/allowlist ratchet is gone): module-size distribution against the cap,
and the refactor-to-addition ratio from numstat. Cap-change events and churn edge
cases live in the held-out companion.

Public surface (from ``rebar.metrics.git_metrics``):
- ``module_size_distribution(repo_root, ref="HEAD") -> dict`` with at least
  ``count``, ``max_loc``, ``over_cap_count`` (the cap read from
  ``.github/module-size-limit.txt``).
- ``refactor_to_addition_ratio(repo_root, since, until) -> float`` (deletions/insertions).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar.metrics.git_metrics import churn, module_size_distribution, refactor_to_addition_ratio

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


def test_module_size_distribution(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    (repo / ".github").mkdir()
    (repo / ".github" / "module-size-limit.txt").write_text("800\n", encoding="utf-8")
    src = repo / "src" / "rebar"
    src.mkdir(parents=True)
    (src / "small.py").write_text("\n".join(f"x = {i}" for i in range(10)) + "\n", encoding="utf-8")
    (src / "big.py").write_text("\n".join(f"y = {i}" for i in range(120)) + "\n", encoding="utf-8")
    _commit(repo, "seed")

    dist = module_size_distribution(str(repo))
    assert dist["count"] == 2  # two .py modules scanned
    assert dist["max_loc"] == 120  # the larger module's line count
    assert dist["over_cap_count"] == 0  # neither exceeds the 800 cap


def test_refactor_to_addition_ratio(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    f = repo / "mod.py"
    f.write_text("\n".join(str(i) for i in range(100)) + "\n", encoding="utf-8")
    _commit(repo, "add 100 lines")  # 100 insertions
    f.write_text("\n".join(str(i) for i in range(60)) + "\n", encoding="utf-8")
    _commit(repo, "delete 40 lines")  # 40 deletions

    ratio = refactor_to_addition_ratio(str(repo), "2000-01-01", "2100-01-01")
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

    c = churn(str(repo), "2000-01-01", "2100-01-01")
    assert c["insertions"] == 100
    assert c["deletions"] == 40
