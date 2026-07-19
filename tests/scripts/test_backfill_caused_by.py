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


def test_end_to_end_exactly_one_link_from_two_bugs(tmp_path, monkeypatch):
    # AC: over a store with one single-culprit bug AND one ambiguous bug, backfill(write=True)
    # draws EXACTLY ONE caused_by link (to the single-culprit's resolved ticket).
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    r = str(repo)

    # single-culprit bug
    culprit = rebar.create_ticket("task", "culprit", repo_root=r)
    sc_bug = rebar.create_ticket("bug", "single-culprit", repo_root=r)
    rebar.transition(sc_bug, "open", "in_progress", repo_root=r)
    (repo / "sc.py").write_text("\n".join(f"l{i}" for i in range(20)) + "\n", encoding="utf-8")
    _git(repo, "add", "sc.py")
    _git(repo, "commit", "-q", "-m", f"introduce\n\nrebar-ticket: {culprit}")
    rebar.set_file_impact(sc_bug, [{"path": "sc.py", "reason": "x"}], repo_root=r)
    (repo / "sc.py").write_text("\n".join(f"f{i}" for i in range(20)) + "\n", encoding="utf-8")
    _git(repo, "add", "sc.py")
    _git(repo, "commit", "-q", "-m", f"fix\n\nrebar-ticket: {sc_bug}")
    rebar.transition(sc_bug, "in_progress", "closed", close_class="regression", repo_root=r)
    # 555e's close hook already auto-drew the link; remove it to simulate a legacy bug.
    rebar.unlink(sc_bug, culprit, repo_root=r)

    # ambiguous bug (two commits contribute ~half each -> no dominant culprit)
    a = rebar.create_ticket("task", "A", repo_root=r)
    b = rebar.create_ticket("task", "B", repo_root=r)
    amb_bug = rebar.create_ticket("bug", "ambiguous", repo_root=r)
    rebar.transition(amb_bug, "open", "in_progress", repo_root=r)
    (repo / "amb.py").write_text("\n".join(f"a{i}" for i in range(10)) + "\n", encoding="utf-8")
    _git(repo, "add", "amb.py")
    _git(repo, "commit", "-q", "-m", f"half A\n\nrebar-ticket: {a}")
    with (repo / "amb.py").open("a", encoding="utf-8") as fh:
        fh.write("\n".join(f"b{i}" for i in range(10)) + "\n")
    _git(repo, "add", "amb.py")
    _git(repo, "commit", "-q", "-m", f"half B\n\nrebar-ticket: {b}")
    rebar.set_file_impact(amb_bug, [{"path": "amb.py", "reason": "x"}], repo_root=r)
    (repo / "amb.py").write_text("rewritten\n", encoding="utf-8")
    _git(repo, "add", "amb.py")
    _git(repo, "commit", "-q", "-m", f"fix\n\nrebar-ticket: {amb_bug}")
    rebar.transition(amb_bug, "in_progress", "closed", close_class="regression", repo_root=r)

    mod = _load()
    n = mod.backfill(r, write=True)
    assert n == 1, "backfill must draw exactly one link (single-culprit only, ambiguous skipped)"

    def _caused_by(tid):
        deps = rebar.show_ticket(tid, repo_root=r)["deps"]
        return [d["target_id"] for d in deps if d["relation"] == "caused_by"]

    assert _caused_by(sc_bug) == [culprit]
    assert _caused_by(amb_bug) == []
