"""Held-out contracts for the git-derivation readers (ticket 7931). WITHHELD.

- ``over_cap_count`` actually counts modules exceeding the single-sourced cap,
- ``cap_change_events`` surfaces a change to .github/module-size-limit.txt with the
  old and new values, and returns [] for a range with no such change,
- the metrics register into the c085 registry with source="git" (authoritative).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar.metrics.git_metrics import (
    cap_change_events,
    module_size_distribution,
    refactor_to_addition_ratio,
)
from rebar.metrics.registry import REGISTRY, is_authoritative

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


def test_over_cap_count_uses_the_single_sourced_cap(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    (repo / ".github").mkdir()
    # A deliberately small cap so a modest module counts as "over".
    (repo / ".github" / "module-size-limit.txt").write_text("50\n", encoding="utf-8")
    src = repo / "src" / "rebar"
    src.mkdir(parents=True)
    (src / "under.py").write_text("\n".join(str(i) for i in range(20)) + "\n", encoding="utf-8")
    (src / "over.py").write_text("\n".join(str(i) for i in range(90)) + "\n", encoding="utf-8")
    _commit(repo, "seed")

    dist = module_size_distribution(str(repo))
    assert dist["over_cap_count"] == 1  # only over.py (90) exceeds the cap of 50
    assert dist["max_loc"] == 90


def test_cap_change_events(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    limit = repo / ".github" / "module-size-limit.txt"
    limit.parent.mkdir()
    limit.write_text("500\n", encoding="utf-8")
    _commit(repo, "introduce cap 500")
    limit.write_text("800\n", encoding="utf-8")
    _commit(repo, "raise cap to 800")

    events = cap_change_events(str(repo), "2000-01-01", "2100-01-01")
    # The raise 500 -> 800 is surfaced with both values.
    raises = [
        e for e in events if str(e.get("old_limit")) == "500" and str(e.get("new_limit")) == "800"
    ]
    assert raises, f"expected a 500->800 cap-change event, got {events}"

    # A future-only range has no cap change.
    assert cap_change_events(str(repo), "2099-01-01", "2100-01-01") == []


def test_refactor_ratio_none_on_zero_insertions(tmp_path):
    # An empty/no-insertions range must return None (=> Unavailable), not raise ZeroDivisionError.
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "seed.py").write_text("x\n", encoding="utf-8")
    _commit(repo, "seed")
    # A future-only window has no commits -> zero insertions -> None.
    assert refactor_to_addition_ratio(str(repo), "2099-01-01", "2100-01-01") is None


def test_git_metrics_registered_with_labels():
    git_specs = {s.id: s for s in REGISTRY if s.source == "git"}
    assert git_specs, "7931 must register git-sourced metrics into REGISTRY"
    for s in git_specs.values():
        assert is_authoritative(s.source) is True
    # The specific new metric ids must actually be registered, each labeled code_health/git.
    named = {"module_size_distribution", "churn", "cap_change_events", "refactor_to_addition_ratio"}
    present = named & set(git_specs)
    assert present, f"expected 7931's git metric ids in REGISTRY, got {sorted(git_specs)}"
    for mid in present:
        assert git_specs[mid].lens == "code_health"
        assert git_specs[mid].source == "git"
