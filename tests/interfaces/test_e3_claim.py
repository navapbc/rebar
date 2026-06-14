"""Tier E E3: claim — in-process vs dispatcher byte-parity.

Dual-run gate: the in-process argparse arm (:func:`rebar._cli.main`) must match the
bash dispatcher byte-for-byte (stdout/stderr/exit). claim mutates status, so each
*mutating* scenario runs on a FRESH ticket per impl (id normalized); non-mutating
scenarios (usage / not-found / arity) run on a shared ticket.
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


def _norm(s: str, tid: str) -> str:
    return s.replace(tid, "<ID>")


def _nonmutating(tid: str) -> list[list[str]]:
    return [
        ["claim"],                                  # usage (exit 1)
        ["claim", "NOPE"],                          # not-found text
        ["claim", "NOPE", "--output", "json"],      # not-found json (envelope)
        ["claim", tid, "--assignee"],               # value-less flag (exit 1)
        ["claim", tid, "--output", "bogus"],        # bad output value (exit 2)
    ]


def test_claim_nonmutating_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid = rebar.create_ticket("task", "E3 claim nonmut", repo_root=str(rebar_repo))
    for argv in _nonmutating(tid):
        b_out, b_err, b_code = _bash(argv, rebar_repo)
        i_out, i_err, i_code = _inproc(argv, capsys)
        assert i_out == b_out, f"{argv}: stdout (inproc {i_out!r} vs bash {b_out!r})"
        assert i_err == b_err, f"{argv}: stderr (inproc {i_err!r} vs bash {b_err!r})"
        assert i_code == b_code, f"{argv}: exit (inproc {i_code} vs bash {b_code})"


@pytest.mark.parametrize(
    "tail",
    [
        [],                                    # claim, text
        ["--output", "json"],                  # claim, json
        ["--assignee", "alice"],               # claim + assignee, text
        ["--assignee=bob", "--output", "json"],  # claim + assignee, json
    ],
)
def test_claim_success_parity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str], tail: list[str]
) -> None:
    a = rebar.create_ticket("task", "E3 claim inproc", repo_root=str(rebar_repo))
    i_out, i_err, i_code = _inproc(["claim", a, *tail], capsys)

    b = rebar.create_ticket("task", "E3 claim bash", repo_root=str(rebar_repo))
    b_out, b_err, b_code = _bash(["claim", b, *tail], rebar_repo)

    assert _norm(i_out, a) == _norm(b_out, b), f"{tail}: stdout {i_out!r} vs {b_out!r}"
    assert _norm(i_err, a) == _norm(b_err, b), f"{tail}: stderr {i_err!r} vs {b_err!r}"
    assert i_code == b_code, f"{tail}: exit {i_code} vs {b_code}"


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_claim_already_claimed_parity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    out_flag = ["--output", "json"] if fmt == "json" else []
    a = rebar.create_ticket("task", "E3 reclaim inproc", repo_root=str(rebar_repo))
    rebar.claim(a, repo_root=str(rebar_repo))
    i_out, i_err, i_code = _inproc(["claim", a, *out_flag], capsys)

    b = rebar.create_ticket("task", "E3 reclaim bash", repo_root=str(rebar_repo))
    rebar.claim(b, repo_root=str(rebar_repo))
    b_out, b_err, b_code = _bash(["claim", b, *out_flag], rebar_repo)

    assert _norm(i_out, a) == _norm(b_out, b), f"{fmt}: stdout {i_out!r} vs {b_out!r}"
    assert _norm(i_err, a) == _norm(b_err, b), f"{fmt}: stderr {i_err!r} vs {b_err!r}"
    assert i_code == b_code == 10
