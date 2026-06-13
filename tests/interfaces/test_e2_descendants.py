"""Tier E E2: list-descendants — in-process vs dispatcher parity."""

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


@pytest.fixture
def tree(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("_TICKET_TEST_NO_SYNC", "1")
    epic = rebar.create_ticket("epic", "E", repo_root=str(rebar_repo))
    story = rebar.create_ticket("story", "S", parent=epic, repo_root=str(rebar_repo))
    task = rebar.create_ticket("task", "T", parent=story, repo_root=str(rebar_repo))
    bug = rebar.create_ticket("bug", "B", parent=epic, repo_root=str(rebar_repo))
    return rebar_repo, epic, story, task, bug


def test_descendants_parity(tree, capsys: pytest.CaptureFixture[str]) -> None:
    repo, epic, story, task, bug = tree
    for argv in (
        ["list-descendants", epic],
        ["list-descendants", story],
        ["list-descendants", task],
        ["list-descendants", "NOPE"],
        ["list-descendants"],
    ):
        b_out, b_err, b_code = _bash(argv, repo)
        i_out, i_err, i_code = _inproc(argv, capsys)
        assert i_out == b_out, f"{argv}: stdout (in-proc {i_out!r} vs bash {b_out!r})"
        assert i_err == b_err, f"{argv}: stderr (in-proc {i_err!r} vs bash {b_err!r})"
        assert i_code == b_code, f"{argv}: exit (in-proc {i_code} vs bash {b_code})"
