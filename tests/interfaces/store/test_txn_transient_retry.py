"""Retry transient ``could not parse HEAD`` failures on transition and claim commits.

The read-side object fault occurs before the ref moves and is safe to retry through
``txn``'s write seam. These tests fail the first commit with the exact stderr, require
self-healing, and keep non-transient commit failures immediate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._store import gitutil

# The verbatim CI stderr for a transient HEAD-resolution fault on the commit step.
_COULD_NOT_PARSE_HEAD = "fatal: could not parse HEAD"


def _fresh_repo(tmp_path: Path, name: str) -> str:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return str(repo)


def _fail_first_commit(monkeypatch: pytest.MonkeyPatch, stderr: str) -> dict:
    """Make the FIRST ``git commit`` routed through gitutil return *stderr* with a
    non-zero exit; delegate every other git call (and later commits) to the real
    ``subprocess.run``. Returns a mutable ``{"commits": n}`` counter."""
    real_run = gitutil.subprocess.run
    state = {"commits": 0}

    def fake_run(cmd, *a, **kw):
        is_commit = isinstance(cmd, list) and "commit" in cmd
        if is_commit:
            state["commits"] += 1
            if state["commits"] == 1:
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr=stderr)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)
    return state


def test_transition_retries_transient_could_not_parse_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transition ``git commit`` self-heals a transient ``could not parse HEAD``."""
    repo = _fresh_repo(tmp_path, "txn")
    tid = rebar.create_ticket("task", "t", repo_root=repo)
    # The ticket is created BEFORE the monkeypatch, so only the transition commit is hit.
    state = _fail_first_commit(monkeypatch, _COULD_NOT_PARSE_HEAD)

    # The first transition commit fails transiently; the write MUST self-heal on retry
    # (before the fix this raises RebarError "could not parse HEAD" — the reported bug).
    rebar.transition(tid, "open", "in_progress", repo_root=repo)

    assert state["commits"] >= 2, "the transient commit failure was retried (not surfaced)"
    assert rebar.show_ticket(tid, repo_root=repo)["status"] == "in_progress"


def test_nontransient_commit_failure_still_fails_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NON-transient ``git commit`` failure is NOT retried — it surfaces immediately, so
    the retry never masks a genuine fault."""
    repo = _fresh_repo(tmp_path, "hard")
    tid = rebar.create_ticket("task", "t", repo_root=repo)
    real_run = gitutil.subprocess.run
    state = {"commits": 0}

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and "commit" in cmd:
            state["commits"] += 1
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="fatal: some genuine non-transient error"
            )
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)

    with pytest.raises(rebar.RebarError):
        rebar.transition(tid, "open", "in_progress", repo_root=repo)
    assert state["commits"] == 1, "a non-transient commit failure must NOT be retried"
