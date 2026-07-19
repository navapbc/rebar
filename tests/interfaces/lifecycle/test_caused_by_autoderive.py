"""Happy-path contract for blame-derived caused_by on bug close (ticket 555e).

Tier: interface (library/CLI over a real temp store). Pins the deterministic core:
an explicit `--caused-by <id>` on a bug close draws a caused_by link from the
(now-closed) bug to the culprit — proving the flag threads through the close path
AND that the link is written via the direct-writer that bypasses the closed-source
guard. Blame auto-derivation, the library facade, and ambiguity are held out.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import rebar

pytestmark = pytest.mark.interface


def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args], capture_output=True, text=True, cwd=cwd
    )


def _caused_by_targets(tid: str, repo: str) -> list[str]:
    deps = rebar.show_ticket(tid, repo_root=repo)["deps"]
    return [d["target_id"] for d in deps if d["relation"] == "caused_by"]


def test_explicit_caused_by_on_bug_close(rebar_repo) -> None:
    repo = str(rebar_repo)
    culprit = rebar.create_ticket("task", "the culprit change", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)

    p = _cli(
        "transition",
        bug,
        "in_progress",
        "closed",
        "--class=regression",
        f"--caused-by={culprit}",
        cwd=repo,
    )
    assert p.returncode == 0, p.stderr

    # The bug (closed source) has a caused_by edge to the culprit — written via the
    # direct-writer path (add_dependency would reject a closed source).
    assert culprit in _caused_by_targets(bug, repo)
    assert rebar.show_ticket(bug, repo_root=repo)["status"] == "closed"


# --- single-culprit + ambiguous blame auto-derivation (AC1 & AC2 live here, not just the
# --- held-out companion): the AC names this file as the proof of blame auto-derivation.
def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_blame_autoderives_single_culprit(rebar_repo) -> None:
    repo_path = rebar_repo
    repo = str(rebar_repo)
    culprit = rebar.create_ticket("task", "culprit change", repo_root=repo)
    bug = rebar.create_ticket("bug", "regression bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)

    (repo_path / "buggy.py").write_text(
        "\n".join(f"line{i}" for i in range(20)) + "\n", encoding="utf-8"
    )
    _git(repo_path, "add", "buggy.py")
    _git(repo_path, "commit", "-q", "-m", f"introduce feature\n\nrebar-ticket: {culprit}")
    rebar.set_file_impact(
        bug, [{"path": "buggy.py", "reason": "the bug lives here"}], repo_root=repo
    )
    (repo_path / "buggy.py").write_text(
        "\n".join(f"fixed{i}" for i in range(20)) + "\n", encoding="utf-8"
    )
    _git(repo_path, "add", "buggy.py")
    _git(repo_path, "commit", "-q", "-m", f"fix the regression\n\nrebar-ticket: {bug}")

    # Close with NO explicit --caused-by: single-culprit blame must resolve the culprit.
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
    # No >50% dominant culprit -> no auto caused_by link.
    assert _caused_by_targets(bug, repo) == []
