"""Happy-path contract for the caused_by backfill script (ticket 2f8e).

Tier: scripts (real temp store + git). Pins the core: over a closed bug with a
single-culprit git history, the script proposes a caused_by link to the culprit
ticket (reusing 555e's blame resolver). Ambiguity / idempotency / write are held out.

The script exposes `propose_caused_by(repo_root) -> list[dict]` returning
{"bug_id","culprit_id"} proposals (dry-run — the default is not to write).
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.scripts

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_caused_by.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_caused_by", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_proposes_single_culprit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    r = str(repo)

    culprit = rebar.create_ticket("task", "culprit change", repo_root=r)
    bug = rebar.create_ticket("bug", "regression bug", repo_root=r)
    rebar.transition(bug, "open", "in_progress", repo_root=r)

    (repo / "buggy.py").write_text(
        "\n".join(f"line{i}" for i in range(20)) + "\n", encoding="utf-8"
    )
    _git(repo, "add", "buggy.py")
    _git(repo, "commit", "-q", "-m", f"introduce\n\nrebar-ticket: {culprit}")
    rebar.set_file_impact(bug, [{"path": "buggy.py", "reason": "here"}], repo_root=r)
    (repo / "buggy.py").write_text(
        "\n".join(f"fixed{i}" for i in range(20)) + "\n", encoding="utf-8"
    )
    _git(repo, "add", "buggy.py")
    _git(repo, "commit", "-q", "-m", f"fix\n\nrebar-ticket: {bug}")
    rebar.transition(bug, "in_progress", "closed", close_class="regression", repo_root=r)

    mod = _load()
    proposals = {p["bug_id"]: p["culprit_id"] for p in mod.propose_caused_by(r)}
    assert proposals.get(bug) == culprit
