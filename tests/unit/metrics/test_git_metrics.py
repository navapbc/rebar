"""Happy-path contracts for the git-derived and analyzer-derived metrics."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

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


def _commit_at(repo: Path, msg: str, iso_date: str) -> str:
    """Commit with a fixed author/committer date for deterministic history tests."""
    env = subprocess_env(GIT_AUTHOR_DATE=iso_date, GIT_COMMITTER_DATE=iso_date)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", msg], cwd=repo, check=True, capture_output=True, env=env
    )
    return _git(repo, "rev-parse", "HEAD").strip()


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


def _seed_module_size_history(repo: Path) -> dict[str, str]:
    """Build a 3-commit history: pre-cap, cap+module (10 LOC), grown module (20 LOC)."""
    _init(repo)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    shas = {"pre_cap": _commit_at(repo, "no cap yet", "2026-01-01T00:00:00+00:00")}

    (repo / ".github").mkdir()
    (repo / ".github" / "module-size-limit.txt").write_text("800\n", encoding="utf-8")
    (repo / "src" / "rebar").mkdir(parents=True)
    (repo / "src" / "rebar" / "mod.py").write_text(
        "\n".join(str(i) for i in range(10)) + "\n", encoding="utf-8"
    )
    shas["first"] = _commit_at(repo, "add cap + module", "2026-01-02T00:00:00+00:00")

    (repo / "src" / "rebar" / "mod.py").write_text(
        "\n".join(str(i) for i in range(20)) + "\n", encoding="utf-8"
    )
    shas["second"] = _commit_at(repo, "grow module", "2026-01-03T00:00:00+00:00")
    return shas


def test_module_size_trend_orders_samples_oldest_to_newest(tmp_path):
    repo = tmp_path / "repo"
    shas = _seed_module_size_history(repo)

    result = git_metrics.module_size_trend(str(repo), "2000-01-01", "2100-01-01")

    assert result == {
        "samples": [
            {
                "sha": shas["first"],
                "timestamp": "2026-01-02T00:00:00+00:00",
                "cap": 800,
                "module_count": 1,
                "max_loc": 10,
            },
            {
                "sha": shas["second"],
                "timestamp": "2026-01-03T00:00:00+00:00",
                "cap": 800,
                "module_count": 1,
                "max_loc": 20,
            },
        ],
        "qualified_revisions": 2,
        "sampled_revisions": 2,
    }


def test_cap_change_events_reports_ordered_changes(tmp_path):
    repo = tmp_path / "repo"
    _seed_module_size_history(repo)
    (repo / ".github" / "module-size-limit.txt").write_text("900\n", encoding="utf-8")
    raise_sha = _commit_at(repo, "raise cap", "2026-01-04T00:00:00+00:00")

    result = git_metrics.cap_change_events(str(repo), "2000-01-01", "2100-01-01")

    assert result == {
        "events": [
            {
                "from": 800,
                "to": 900,
                "sha": raise_sha,
                "timestamp": "2026-01-04T00:00:00+00:00",
            }
        ],
        "qualified_revisions": 3,
    }


def test_cap_change_events_empty_list_when_cap_never_changes(tmp_path):
    repo = tmp_path / "repo"
    _seed_module_size_history(repo)

    result = git_metrics.cap_change_events(str(repo), "2000-01-01", "2100-01-01")

    # A sufficient, qualifying history with no cap change is a real value: events=[].
    assert result == {"events": [], "qualified_revisions": 2}


# Held-out oracle re-applied after implementation.
def _oracle_write_cap(repo: Path, cap: int) -> None:
    cap_file = repo / ".github" / "module-size-limit.txt"
    cap_file.parent.mkdir(parents=True, exist_ok=True)
    cap_file.write_text(f"{cap}\n", encoding="utf-8")


def _oracle_write_module(repo: Path, name: str, line_count: int) -> None:
    module = repo / "src" / "rebar" / name
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("".join(f"line_{i}\n" for i in range(line_count)), encoding="utf-8")


def test_oracle_module_size_trend_uses_historical_caps_and_raw_newline_loc(tmp_path):
    repo = tmp_path / "repo"
    _init(repo)
    _oracle_write_cap(repo, 800)
    _oracle_write_module(repo, "small.py", 2)
    first = _commit_at(repo, "first qualified", "2026-01-02T00:00:00+00:00")
    _oracle_write_cap(repo, 700)
    _oracle_write_module(repo, "small.py", 4)
    _oracle_write_module(repo, "large.py", 6)
    second = _commit_at(repo, "second qualified", "2026-01-03T00:00:00+00:00")

    trend = git_metrics.module_size_trend(str(repo), "2026-01-01", "2026-01-31")

    assert trend == {
        "qualified_revisions": 2,
        "sampled_revisions": 2,
        "samples": [
            {
                "sha": first,
                "timestamp": "2026-01-02T00:00:00+00:00",
                "cap": 800,
                "module_count": 1,
                "max_loc": 2,
            },
            {
                "sha": second,
                "timestamp": "2026-01-03T00:00:00+00:00",
                "cap": 700,
                "module_count": 2,
                "max_loc": 6,
            },
        ],
    }


def test_oracle_cap_change_events_compares_unsampled_adjacent_revisions(tmp_path):
    from datetime import datetime, timedelta

    repo = tmp_path / "repo"
    _init(repo)
    changed: list[tuple[str, int, str]] = []
    start = datetime.fromisoformat("2026-02-01T00:00:00+00:00")
    for idx in range(60):
        cap = 800 if idx < 30 else 750
        timestamp = (start + timedelta(days=idx)).isoformat()
        _oracle_write_cap(repo, cap)
        _oracle_write_module(repo, "m.py", idx + 1)
        sha = _commit_at(repo, f"qualified {idx}", timestamp)
        if idx == 30:
            changed.append((sha, cap, timestamp))

    events = git_metrics.cap_change_events(str(repo), "2026-02-01", "2026-04-30")
    trend = git_metrics.module_size_trend(str(repo), "2026-02-01", "2026-04-30")

    assert events == {
        "qualified_revisions": 60,
        "events": [{"from": 800, "to": 750, "sha": changed[0][0], "timestamp": changed[0][2]}],
    }
    assert trend["qualified_revisions"] == 60
    assert trend["sampled_revisions"] == 50
    assert len(trend["samples"]) == 50
    assert trend["samples"][0]["max_loc"] == 1
    assert trend["samples"][-1]["max_loc"] == 60
