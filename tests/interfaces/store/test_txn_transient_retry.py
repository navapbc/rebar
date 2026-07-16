"""A transient git object-DB/ref-read fault on the TRANSITION/CLAIM ``git commit`` is
retried, not surfaced as a hard write failure (bug childsafe-special-springtail).

This is the completion of edf7/vocal-dip-robin. That fix retried the transient
loose-object runner-FS fault on the CREATE path's ``git add`` (see
``test_event_append_transient_retry.py``), but the SAME transient also strikes the
READ side: a ``git commit`` on the transition/claim path resolves ``HEAD`` to set the
new commit's parent (``parse_commit``), and when HEAD's commit object is transiently
unreadable under a shared tracker's ``.git/objects/`` git dies with verbatim
``fatal: could not parse HEAD`` (exit 128). It is a filesystem hiccup, not data
corruption: a Gerrit ``recheck`` on the identical patchset passes.

The transition/claim path routes through ``txn.py``'s ``_git`` → ``run_git_write``,
which (before this fix) retried ONLY ``index.lock`` — so the transient surfaced as the
hard ``rebar transition failed (exit 2): Error: git operation failed: fatal: could not
parse HEAD``. These tests inject that exact stderr on the FIRST transition commit and
assert the write self-heals on retry, while a NON-transient commit failure still fails
immediately (no behavior change there).
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

    def fake_run(cmd, *a, **kw):  # noqa: ANN001
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

    def fake_run(cmd, *a, **kw):  # noqa: ANN001
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
