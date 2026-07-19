"""Happy-path contract for the persisted bug-close `--class` enum (ticket ed13).

Tier: interface (real CLI subprocess + reduced-state read). This pins the core
behavior change: a bug closes with a bounded `--class` enum (REPLACING the old
required free-text `--reason`), and the value is folded into reduced state so
`rebar show` can see it. Error-message quality and controls are held out.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import rebar

pytestmark = pytest.mark.interface


def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _bug_in_progress(repo: str) -> str:
    bug = rebar.create_ticket("bug", "a bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)
    return bug


def test_bug_close_with_class_persists(rebar_repo) -> None:
    repo = str(rebar_repo)
    bug = _bug_in_progress(repo)

    # Closes with --class ALONE (no --reason) — the enum replaces the free-text reason.
    p = _cli("transition", bug, "in_progress", "closed", "--class=regression", cwd=repo)
    assert p.returncode == 0, p.stderr

    shown = rebar.show_ticket(bug, repo_root=repo)
    assert shown["status"] == "closed"
    assert shown["close_class"] == "regression"


def test_bug_close_undetermined_escape_value_ok(rebar_repo) -> None:
    repo = str(rebar_repo)
    bug = _bug_in_progress(repo)

    p = _cli("transition", bug, "in_progress", "closed", "--class=undetermined", cwd=repo)
    assert p.returncode == 0, p.stderr
    assert rebar.show_ticket(bug, repo_root=repo)["close_class"] == "undetermined"
