"""Happy-path contract for the complexity/clone backfill (ticket c595).

Tier: scripts (temp git repo + STUBBED measure_tree). Pins the core: sampling
commits writes one snapshot per commit with integer complexity/clone_count,
tagged with the COMMIT's date (not wall-clock). Skip-on-failure / labels are held out.

The script exposes `measure_tree(tree_path) -> dict | None` (the tool seam, monkeypatched)
and `backfill(repo_root, commits) -> int` (writes snapshots via snapshot.py, returns count).

Held-out oracles merged from test_backfill_complexity_clone_heldout: Held-out contracts for the
complexity/clone backfill (ticket c595). WITHHELD.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar.metrics.snapshot import read_snapshots

pytestmark = pytest.mark.scripts


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_complexity_clone_snapshots.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_ccs", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _repo_with_commits(tmp_path: Path, n: int) -> tuple[Path, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    shas = []
    for i in range(n):
        (repo / "f.py").write_text(f"x = {i}\n", encoding="utf-8")
        _git(repo, "add", "f.py")
        cdate = f"2026-0{i + 1}-15T00:00:00"
        subprocess.run(
            ["git", "commit", "-q", "--date", cdate, "-m", f"c{i}"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            env=subprocess_env({"GIT_COMMITTER_DATE": cdate}),
        )
        shas.append(_git(repo, "rev-parse", "HEAD").strip())
    return repo, shas


def test_backfill_writes_one_snapshot_per_commit(tmp_path, monkeypatch):
    repo, shas = _repo_with_commits(tmp_path, 3)
    mod = _load()
    # Stub the tool seam so no scc/lizard/jscpd is needed.
    monkeypatch.setattr(mod, "measure_tree", lambda tree: {"complexity": 42, "clone_count": 3})

    count = mod.backfill(str(repo), shas)
    assert count == 3

    recs = read_snapshots("2026-01-01", "2026-12-31", repo_root=str(repo))
    got = [r for r in recs if "complexity" in r and "clone_count" in r]
    assert len(got) == 3
    for r in got:
        assert isinstance(r["complexity"], int)
        assert isinstance(r["clone_count"], int)


# ── held-out oracles (merged from test_backfill_complexity_clone_heldout) ──


def _repo_with_commits_heldout(tmp_path: Path, dates: list[str]) -> tuple[Path, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    shas = []
    for i, d in enumerate(dates):
        (repo / "f.py").write_text(f"x = {i}\n", encoding="utf-8")
        _git(repo, "add", "f.py")
        cdate = f"{d}T00:00:00"
        subprocess.run(
            ["git", "commit", "-q", "--date", cdate, "-m", f"c{i}"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            env=subprocess_env({"GIT_COMMITTER_DATE": cdate}),
        )
        shas.append(_git(repo, "rev-parse", "HEAD").strip())
    return repo, shas


def test_snapshot_ts_is_commit_date(tmp_path, monkeypatch):
    repo, shas = _repo_with_commits_heldout(tmp_path, ["2026-02-15", "2026-05-15"])
    mod = _load()
    monkeypatch.setattr(mod, "measure_tree", lambda tree: {"complexity": 10, "clone_count": 1})
    mod.backfill(str(repo), shas)

    # A window covering only Feb must return exactly the Feb-dated snapshot, proving ts==commit date
    # (if ts were the wall-clock run time, both would fall on 'now' and this window would be wrong).
    feb = read_snapshots("2026-02-01", "2026-02-28", repo_root=str(repo))
    feb_ccs = [r for r in feb if "complexity" in r]
    assert len(feb_ccs) == 1


def test_measure_failure_skipped_nonfatal(tmp_path, monkeypatch):
    repo, shas = _repo_with_commits_heldout(tmp_path, ["2026-02-15", "2026-03-15", "2026-04-15"])
    mod = _load()
    calls = {"n": 0}

    def flaky(tree):
        calls["n"] += 1
        return None if calls["n"] == 2 else {"complexity": 5, "clone_count": 0}

    monkeypatch.setattr(mod, "measure_tree", flaky)
    count = mod.backfill(str(repo), shas)  # must not raise
    assert count == 2


def test_snapshots_labeled_source_snapshot(tmp_path, monkeypatch):
    repo, shas = _repo_with_commits_heldout(tmp_path, ["2026-02-15"])
    mod = _load()
    monkeypatch.setattr(mod, "measure_tree", lambda tree: {"complexity": 7, "clone_count": 2})
    mod.backfill(str(repo), shas)
    recs = [
        r
        for r in read_snapshots("2026-01-01", "2026-12-31", repo_root=str(repo))
        if "complexity" in r
    ]
    assert recs
    for r in recs:
        assert r.get("source") == "snapshot"
        assert r.get("confidence") == "high"
