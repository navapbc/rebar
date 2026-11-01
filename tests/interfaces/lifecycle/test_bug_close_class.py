"""Happy-path contract for the persisted bug-close `--class` enum (ticket ed13).

Tier: interface (real CLI subprocess + reduced-state read). This pins the core
behavior change: a bug closes with a bounded `--class` enum (REPLACING the old
required free-text `--reason`), and the value is folded into reduced state so
`rebar show` can see it. Error-message quality and controls are held out.

Held-out oracles merged from test_bug_close_class_heldout: Held-out contracts for the bug-close
`--class` enum (ticket ed13). WITHHELD.
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
        check=False,
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


# ── held-out oracles (merged from test_bug_close_class_heldout) ──


def test_bug_close_without_class_fails_and_names_allowed_values(rebar_repo) -> None:
    repo = str(rebar_repo)
    bug = _bug_in_progress(repo)

    p = _cli("transition", bug, "in_progress", "closed", cwd=repo)
    assert p.returncode != 0
    # The error must actually surface the enum vocabulary (name at least one value),
    # not merely exit nonzero — a blank/misleading message must fail this test.
    assert "regression" in p.stderr, f"stderr did not name allowed values: {p.stderr!r}"


def test_bug_close_invalid_class_rejected(rebar_repo) -> None:
    repo = str(rebar_repo)
    bug = _bug_in_progress(repo)

    p = _cli("transition", bug, "in_progress", "closed", "--class=definitely_not_valid", cwd=repo)
    assert p.returncode != 0


def test_nonbug_close_does_not_require_class(rebar_repo) -> None:
    # The --class requirement is bug-specific; a task closes with no --class/--reason.
    repo = str(rebar_repo)
    task = rebar.create_ticket("task", "a task", repo_root=repo)
    rebar.transition(task, "open", "in_progress", repo_root=repo)

    p = _cli("transition", task, "in_progress", "closed", cwd=repo)
    assert p.returncode == 0, p.stderr
    assert rebar.show_ticket(task, repo_root=repo)["status"] == "closed"


def test_close_class_absent_before_close(rebar_repo) -> None:
    # close_class is folded only on the *->closed edge, not on open->in_progress.
    repo = str(rebar_repo)
    bug = _bug_in_progress(repo)
    shown = rebar.show_ticket(bug, repo_root=repo)
    assert shown.get("close_class") in (None, "")
