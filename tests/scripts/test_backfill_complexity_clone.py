"""Happy-path contract for the complexity/clone backfill (ticket c595).

Tier: scripts (temp git repo + STUBBED measure_tree). Pins the core: sampling
commits writes one snapshot per commit with integer complexity/clone_count,
tagged with the COMMIT's date (not wall-clock). Skip-on-failure / labels are held out.

The script exposes `measure_tree(tree_path) -> dict | None` (the tool seam, monkeypatched)
and `backfill(repo_root, commits) -> int` (writes snapshots via snapshot.py, returns count).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

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
            env={**os.environ, "GIT_COMMITTER_DATE": cdate},
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
