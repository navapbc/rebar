"""Regression tests for the 3 correctness bugs the E0–E4 opus review surfaced.

Each test FAILS against the pre-fix code and passes after the fix:
  1. delete must abort (non-zero, no false "Deleted") + roll back on a git-commit
     failure — not report success on a half-written store.
  2. transition_core must UN-STAGE the orphaned event on a commit failure, so it is
     not swept into the next write's commit (index pollution).
  3. the auto-init consent gate must check the tracker at the SAME location init
     writes it (config.tracker_dir), so a REBAR_ROOT that differs from the git
     toplevel does not cause an infinite re-prompt / re-error.
"""

from __future__ import annotations

import builtins
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._cli import main


def test_delete_aborts_and_rolls_back_on_commit_failure(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from rebar._commands import delete as d
    from rebar._commands._seam import CommandError

    tid = rebar.create_ticket("task", "victim", repo_root=str(rebar_repo))
    real = d._git

    def fail_on_commit(tracker, *args):
        if args[:1] == ("commit",):
            raise CommandError("Error: git operation failed during delete: boom", returncode=2)
        return real(tracker, *args)

    monkeypatch.setattr(d, "_git", fail_on_commit)
    capsys.readouterr()
    code = main(["delete", tid, "--user-approved"])
    out = capsys.readouterr().out
    assert code == 2, "delete must NOT report success when the commit fails"
    assert "Deleted ticket" not in out
    # Rolled back: tombstone removed, ticket still reduces as a live (non-deleted) ticket.
    assert not (rebar_repo / ".tickets-tracker" / tid / ".tombstone.json").exists()
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


def test_transition_unstages_orphaned_event_on_commit_failure(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands import txn
    from rebar._commands._seam import CommandError

    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    tracker = str(rebar_repo / ".tickets-tracker")
    real = txn._git

    def fail_on_commit(td, *args):
        if args[:1] == ("commit",):
            raise CommandError("Error: git operation failed: boom", returncode=2)
        return real(td, *args)

    monkeypatch.setattr(txn, "_git", fail_on_commit)
    with pytest.raises(CommandError):
        txn.transition_core(tracker, tid, "open", "in_progress", env_id="e", author="a")

    # The orphaned STATUS event must be unstaged (index clean) AND off disk — else the
    # next write's commit would sweep in a transition the user never completed.
    diff = subprocess.run(
        ["git", "-C", tracker, "diff", "--cached", "--quiet"], capture_output=True
    )
    assert diff.returncode == 0, "orphaned event left STAGED in the index"
    assert not list((rebar_repo / ".tickets-tracker" / tid).glob("*-STATUS.json"))


def test_consent_gate_no_reprompt_when_root_differs_from_toplevel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=repo, check=True)
    sub = repo / "sub"
    sub.mkdir()
    # REBAR_ROOT points at a SUBDIR (≠ git toplevel). Pre-fix: the gate checked
    # sub/.tickets-tracker while init wrote toplevel/.tickets-tracker → re-prompt loop.
    monkeypatch.setenv("REBAR_ROOT", str(sub))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")

    from rebar._cli import _init as cli_init

    # First (interactive) command auto-inits.
    monkeypatch.setattr(cli_init, "_is_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda: "y")
    capsys.readouterr()
    assert main(["list"]) == 0
    from rebar import config

    assert config.tracker_dir(str(sub)).is_dir(), "init wrote the tracker where commands look"

    # Second (non-interactive) command must find the tracker — NOT re-error.
    monkeypatch.setattr(cli_init, "_is_interactive", lambda: False)
    capsys.readouterr()
    assert main(["list"]) == 0, "gate re-errored: it checks a different path than init wrote"
