"""Tier E E4: fsck-recover — in-process vs dispatcher byte-parity + recovery.

Deterministic paths (no-op / detect-only / errors) dual-run on a shared store.
The destructive cherry-pick path is exercised by orphaning a ticket commit into a
dangling commit, then asserting --recover-dangling cherry-picks it back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._cli import main
from rebar._engine import dispatcher, engine_env


def _bash(argv: list[str], repo: Path) -> tuple[str, str, int]:
    env = engine_env(str(repo))
    env["_TICKET_TEST_NO_SYNC"] = "1"
    cp = subprocess.run(
        ["bash", str(dispatcher()), *argv],
        env=env, cwd=str(repo), capture_output=True, text=True,
    )
    return cp.stdout, cp.stderr, cp.returncode


def _inproc(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[str, str, int]:
    capsys.readouterr()
    code = main(argv)
    cap = capsys.readouterr()
    return cap.out, cap.err, code


@pytest.fixture(autouse=True)
def _no_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_TICKET_TEST_NO_SYNC", "1")


def _norm(s: str, repo: Path) -> str:
    # Normalize the absolute tracker/gitdir paths that appear in detect-only output.
    return s.replace(str(repo), "<REPO>")


def test_fsck_recover_deterministic_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rebar.create_ticket("task", "fr", repo_root=str(rebar_repo))
    tracker = str(rebar_repo / ".tickets-tracker")
    # Pass --tracker-dir explicitly so both impls resolve the SAME tracker (the
    # default resolution differs by process cwd between the in-proc and subprocess
    # harness; --tracker-dir pins it and exercises the same code path).
    for argv in (
        ["fsck-recover", "--tracker-dir", tracker],                   # no-op
        ["fsck-recover", "--tracker-dir", tracker, "--detect-only"],  # detect-only clean
        ["fsck-recover", "--bogus"],                                  # unknown arg (usage, exit 2)
    ):
        b_out, b_err, b_code = _bash(argv, rebar_repo)
        i_out, i_err, i_code = _inproc(argv, capsys)
        assert _norm(i_out, rebar_repo) == _norm(b_out, rebar_repo), f"{argv}: {i_out!r} vs {b_out!r}"
        assert _norm(i_err, rebar_repo) == _norm(b_err, rebar_repo), f"{argv}: {i_err!r} vs {b_err!r}"
        assert i_code == b_code, f"{argv}: exit {i_code} vs {b_code}"


def test_fsck_recover_bad_tracker_dir_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["fsck-recover", "--tracker-dir", "/no-such-dir-xyz"]
    b_out, b_err, b_code = _bash(argv, rebar_repo)
    i_out, i_err, i_code = _inproc(argv, capsys)
    assert i_out == b_out and i_err == b_err and i_code == b_code == 2


def _tracker_git(repo: Path) -> list[str]:
    return ["git", "-C", str(repo / ".tickets-tracker")]


def test_fsck_recover_cherry_picks_dangling(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Orphan a ticket commit into a dangling commit; --recover-dangling restores it."""
    rebar.create_ticket("task", "anchor", repo_root=str(rebar_repo))
    victim = rebar.create_ticket("task", "victim-to-orphan", repo_root=str(rebar_repo))
    # The victim's CREATE is the tip commit; reset one back, then truly orphan the
    # commit (clear ORIG_HEAD + expire reflogs) so `git fsck --no-reflogs` flags it
    # as dangling rather than reachable via reflog/ORIG_HEAD.
    subprocess.run(_tracker_git(rebar_repo) + ["reset", "--hard", "HEAD~1"], check=True, capture_output=True)
    subprocess.run(_tracker_git(rebar_repo) + ["update-ref", "-d", "ORIG_HEAD"], capture_output=True)
    subprocess.run(_tracker_git(rebar_repo) + ["reflog", "expire", "--expire=now", "--all"], capture_output=True)
    # The victim dir is now gone from the worktree.
    assert not (rebar_repo / ".tickets-tracker" / victim).exists()

    out, _, code = _inproc(["fsck-recover", "--recover-dangling"], capsys)
    assert code == 0, out
    assert "cherry-picked" in out
    # Victim restored + reducible again.
    assert (rebar_repo / ".tickets-tracker" / victim).exists()
    assert rebar.show_ticket(victim, repo_root=str(rebar_repo))["title"] == "victim-to-orphan"


def test_fsck_recover_library_clean(rebar_repo: Path) -> None:
    rebar.create_ticket("task", "lib fr", repo_root=str(rebar_repo))
    out = rebar.fsck(recover=True, repo_root=str(rebar_repo))
    assert "nothing to recover" in out
