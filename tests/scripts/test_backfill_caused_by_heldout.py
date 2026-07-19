"""Held-out contracts for the caused_by backfill (ticket 2f8e). WITHHELD.

- an ambiguous multi-commit bug yields NO proposal,
- writing then re-running is idempotent (second write draws 0 new links —
  the _is_active_link guard),
- --dry-run does not write.
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


def _repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _closed_bug_single_culprit(repo, r):
    culprit = rebar.create_ticket("task", "culprit", repo_root=r)
    bug = rebar.create_ticket("bug", "bug", repo_root=r)
    rebar.transition(bug, "open", "in_progress", repo_root=r)
    (Path(repo) / "b.py").write_text("\n".join(f"l{i}" for i in range(20)) + "\n", encoding="utf-8")
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-q", "-m", f"introduce\n\nrebar-ticket: {culprit}")
    rebar.set_file_impact(bug, [{"path": "b.py", "reason": "x"}], repo_root=r)
    (Path(repo) / "b.py").write_text("\n".join(f"f{i}" for i in range(20)) + "\n", encoding="utf-8")
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-q", "-m", f"fix\n\nrebar-ticket: {bug}")
    rebar.transition(bug, "in_progress", "closed", close_class="regression", repo_root=r)
    return bug, culprit


def _caused_by(tid, r):
    return [
        d["target_id"]
        for d in rebar.show_ticket(tid, repo_root=r)["deps"]
        if d["relation"] == "caused_by"
    ]


def test_ambiguous_bug_no_proposal(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    r = str(repo)
    a = rebar.create_ticket("task", "A", repo_root=r)
    b = rebar.create_ticket("task", "B", repo_root=r)
    bug = rebar.create_ticket("bug", "ambiguous", repo_root=r)
    rebar.transition(bug, "open", "in_progress", repo_root=r)
    (repo / "m.py").write_text("\n".join(f"a{i}" for i in range(10)) + "\n", encoding="utf-8")
    _git(repo, "add", "m.py")
    _git(repo, "commit", "-q", "-m", f"half A\n\nrebar-ticket: {a}")
    with (repo / "m.py").open("a", encoding="utf-8") as fh:
        fh.write("\n".join(f"b{i}" for i in range(10)) + "\n")
    _git(repo, "add", "m.py")
    _git(repo, "commit", "-q", "-m", f"half B\n\nrebar-ticket: {b}")
    rebar.set_file_impact(bug, [{"path": "m.py", "reason": "x"}], repo_root=r)
    (repo / "m.py").write_text("rewritten\n", encoding="utf-8")
    _git(repo, "add", "m.py")
    _git(repo, "commit", "-q", "-m", f"fix\n\nrebar-ticket: {bug}")
    rebar.transition(bug, "in_progress", "closed", close_class="regression", repo_root=r)

    mod = _load()
    proposals = {p["bug_id"]: p["culprit_id"] for p in mod.propose_caused_by(r)}
    assert bug not in proposals  # ambiguous -> no proposal


def test_write_is_idempotent(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    r = str(repo)
    bug, culprit = _closed_bug_single_culprit(repo, r)
    mod = _load()

    # 555e's close hook already auto-drew the caused_by at close time. Remove it to
    # simulate a LEGACY bug (closed before 555e existed) — the case the backfill exists for.
    rebar.unlink(bug, culprit, repo_root=r)
    assert culprit not in _caused_by(bug, r)

    n1 = mod.backfill(r, write=True)  # backfill must re-draw the link
    assert culprit in _caused_by(bug, r)
    assert n1 >= 1
    n2 = mod.backfill(r, write=True)  # second run: link already active -> 0 new
    assert n2 == 0
    assert _caused_by(bug, r).count(culprit) == 1  # not duplicated


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    r = str(repo)
    bug, culprit = _closed_bug_single_culprit(repo, r)
    mod = _load()
    # Simulate a legacy link-less bug (remove the close-hook's auto-link).
    rebar.unlink(bug, culprit, repo_root=r)
    assert culprit not in _caused_by(bug, r)

    mod.backfill(r, write=False)  # dry-run: must write nothing
    assert culprit not in _caused_by(bug, r)
