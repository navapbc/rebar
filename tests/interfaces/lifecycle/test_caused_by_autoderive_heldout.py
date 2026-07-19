"""Held-out contracts for blame-derived caused_by (ticket 555e). WITHHELD.

- the LIBRARY facade `rebar.transition(..., caused_by=...)` also draws the link
  (the ed13-parallel gap: the facade must thread the new param),
- git-blame auto-derivation on a single-culprit fixture draws caused_by to the
  culprit ticket with NO explicit param,
- an ambiguous multi-commit blame draws NO auto-link,
- the user-guide gains a blame-hunt advisory.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.interface


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args], capture_output=True, text=True, cwd=cwd
    )


def _caused_by_targets(tid: str, repo: str) -> list[str]:
    deps = rebar.show_ticket(tid, repo_root=repo)["deps"]
    return [d["target_id"] for d in deps if d["relation"] == "caused_by"]


def test_library_facade_threads_caused_by(rebar_repo) -> None:
    repo = str(rebar_repo)
    culprit = rebar.create_ticket("task", "culprit", repo_root=repo)
    bug = rebar.create_ticket("bug", "bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)

    # The library facade must accept and thread caused_by (parallel to ed13's close_class).
    rebar.transition(
        bug, "in_progress", "closed", close_class="regression", caused_by=culprit, repo_root=repo
    )
    assert culprit in _caused_by_targets(bug, repo)


def test_blame_autoderives_single_culprit(rebar_repo) -> None:
    repo_path = rebar_repo
    repo = str(rebar_repo)
    culprit = rebar.create_ticket("task", "culprit change", repo_root=repo)
    bug = rebar.create_ticket("bug", "regression bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)

    # The culprit commit: introduces buggy.py, ALL its lines, tagged with the culprit ticket.
    (repo_path / "buggy.py").write_text(
        "\n".join(f"line{i}" for i in range(20)) + "\n", encoding="utf-8"
    )
    _git(repo_path, "add", "buggy.py")
    _git(repo_path, "commit", "-q", "-m", f"introduce feature\n\nrebar-ticket: {culprit}")

    # Record the bug's file_impact so blame knows which file to inspect.
    rebar.set_file_impact(
        bug, [{"path": "buggy.py", "reason": "the bug lives here"}], repo_root=repo
    )

    # The fixing commit: references the bug (so blame can find <fixing> and blame <fixing>~1).
    (repo_path / "buggy.py").write_text(
        "\n".join(f"fixed{i}" for i in range(20)) + "\n", encoding="utf-8"
    )
    _git(repo_path, "add", "buggy.py")
    _git(repo_path, "commit", "-q", "-m", f"fix the regression\n\nrebar-ticket: {bug}")

    # Close with NO explicit --caused-by: auto-derivation must resolve the single culprit.
    p = _cli("transition", bug, "in_progress", "closed", "--class=regression", cwd=repo)
    assert p.returncode == 0, p.stderr
    assert culprit in _caused_by_targets(bug, repo), "single-culprit blame must auto-draw caused_by"


def test_ambiguous_blame_draws_no_autolink(rebar_repo) -> None:
    repo_path = rebar_repo
    repo = str(rebar_repo)
    a = rebar.create_ticket("task", "change A", repo_root=repo)
    b_culprit = rebar.create_ticket("task", "change B", repo_root=repo)
    bug = rebar.create_ticket("bug", "ambiguous bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)

    # Two commits contribute ~half the file each (no >50% dominant culprit).
    (repo_path / "mixed.py").write_text(
        "\n".join(f"a{i}" for i in range(10)) + "\n", encoding="utf-8"
    )
    _git(repo_path, "add", "mixed.py")
    _git(repo_path, "commit", "-q", "-m", f"half A\n\nrebar-ticket: {a}")
    with (repo_path / "mixed.py").open("a", encoding="utf-8") as fh:
        fh.write("\n".join(f"b{i}" for i in range(10)) + "\n")
    _git(repo_path, "add", "mixed.py")
    _git(repo_path, "commit", "-q", "-m", f"half B\n\nrebar-ticket: {b_culprit}")

    rebar.set_file_impact(bug, [{"path": "mixed.py", "reason": "here"}], repo_root=repo)
    (repo_path / "mixed.py").write_text("rewritten\n", encoding="utf-8")
    _git(repo_path, "add", "mixed.py")
    _git(repo_path, "commit", "-q", "-m", f"fix\n\nrebar-ticket: {bug}")

    p = _cli("transition", bug, "in_progress", "closed", "--class=regression", cwd=repo)
    assert p.returncode == 0, p.stderr
    # No single dominant culprit -> no auto caused_by link.
    assert _caused_by_targets(bug, repo) == []


def test_user_guide_has_blame_hunt_advisory() -> None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        ug = parent / "docs" / "user-guide.md"
        if ug.exists():
            text = ug.read_text(encoding="utf-8").lower()
            assert "caused-by" in text or "caused_by" in text, (
                "user-guide must mention the caused-by advisory"
            )
            return
    raise AssertionError("could not locate docs/user-guide.md")
