"""Held-out contracts for the bug-close `--class` enum (ticket ed13). WITHHELD.

- omitting `--class` on a bug close fails AND the error names the allowed values
  (the message-quality claim is measured, not just the exit code),
- an out-of-enum value is rejected,
- the `--class` requirement is bug-specific (a non-bug closes without it),
- `close_class` is only set on the close, not on earlier transitions.
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
