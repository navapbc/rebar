"""Tier E E3: transition / reopen — in-process vs dispatcher byte-parity.

Dual-run gate: the in-process argparse arm (:func:`rebar._cli.main`) must match the
bash dispatcher byte-for-byte (stdout/stderr/exit) — the dispatcher being the
second, still-live implementation until the E7/E8 retirement.

Transitions mutate status, so a shared-store dual-run would let the first impl's
write perturb the second's state. Each *mutating* scenario therefore runs on a
FRESH ticket per impl and compares with the ticket id normalized; non-mutating
scenarios (not-found / usage / invalid / no-op) run on a shared ticket.
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


# ── Non-mutating scenarios: shared ticket, exact dual-run ─────────────────────
def _nonmutating(tid: str) -> list[list[str]]:
    return [
        ["transition"],                                  # usage (exit 1)
        ["transition", "NOPE", "open", "closed"],        # not-found text
        ["transition", "NOPE", "open", "closed", "--output", "json"],  # not-found json
        ["transition", tid, "open", "bogus"],            # invalid target
        ["transition", tid, "open", "deleted"],          # deleted target reject
        ["transition", tid, "open", "open"],             # no-op (text)
        ["transition", tid, "open", "open", "--output", "json"],  # no-op (json: still text!)
        ["transition", tid, "--reason", "x"],            # invalid: --reason as target
        ["transition", tid, "open", "--output", "bogus"],  # bad output value (exit 2)
        ["reopen"],                                      # reopen usage (exit 1)
        ["reopen", "NOPE"],                              # reopen not-found
    ]


def test_transition_nonmutating_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid = rebar.create_ticket("task", "E3 nonmutating", repo_root=str(rebar_repo))
    for argv in _nonmutating(tid):
        b_out, b_err, b_code = _bash(argv, rebar_repo)
        i_out, i_err, i_code = _inproc(argv, capsys)
        assert i_out == b_out, f"{argv}: stdout (inproc {i_out!r} vs bash {b_out!r})"
        assert i_err == b_err, f"{argv}: stderr (inproc {i_err!r} vs bash {b_err!r})"
        assert i_code == b_code, f"{argv}: exit (inproc {i_code} vs bash {b_code})"


# ── Mutating scenarios: fresh ticket per impl, id normalized ──────────────────
@pytest.mark.parametrize(
    "argv_tail",
    [
        ["open", "in_progress"],                          # non-close, text (no output)
        ["open", "in_progress", "--output", "json"],      # non-close, json
        ["open", "closed"],                               # close, text (UNBLOCKED: none)
        ["open", "closed", "--output", "json"],           # close, json
        ["in_progress"],                                  # 2-arg autodetect (open->in_progress)
    ],
)
def test_transition_mutating_parity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str], argv_tail: list[str]
) -> None:
    a = rebar.create_ticket("task", "E3 inproc", repo_root=str(rebar_repo))
    i_out, i_err, i_code = _inproc(["transition", a, *argv_tail], capsys)

    b = rebar.create_ticket("task", "E3 bash", repo_root=str(rebar_repo))
    b_out, b_err, b_code = _bash(["transition", b, *argv_tail], rebar_repo)

    assert _norm(i_out, a) == _norm(b_out, b), f"stdout: {i_out!r} vs {b_out!r}"
    assert _norm(i_err, a) == _norm(b_err, b), f"stderr: {i_err!r} vs {b_err!r}"
    assert i_code == b_code, f"exit: {i_code} vs {b_code}"


def test_reopen_roundtrip_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for tail in ([], ["--output", "json"]):
        a = rebar.create_ticket("task", "E3 reopen inproc", repo_root=str(rebar_repo))
        rebar.transition(a, "open", "closed", repo_root=str(rebar_repo))
        i_out, i_err, i_code = _inproc(["reopen", a, *tail], capsys)

        b = rebar.create_ticket("task", "E3 reopen bash", repo_root=str(rebar_repo))
        rebar.transition(b, "open", "closed", repo_root=str(rebar_repo))
        b_out, b_err, b_code = _bash(["reopen", b, *tail], rebar_repo)

        assert _norm(i_out, a) == _norm(b_out, b), f"{tail}: {i_out!r} vs {b_out!r}"
        assert _norm(i_err, a) == _norm(b_err, b), f"{tail}: {i_err!r} vs {b_err!r}"
        assert i_code == b_code, f"{tail}: {i_code} vs {b_code}"


# ── newly_unblocked parity: B blocks A, close B, A becomes unblocked ──────────
def test_close_unblocks_dependent_parity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for fmt in ("text", "json"):
        out_flag = ["--output", "json"] if fmt == "json" else []
        # in-proc store arm
        ai = rebar.create_ticket("task", "blocked A i", repo_root=str(rebar_repo))
        bi = rebar.create_ticket("task", "blocker B i", repo_root=str(rebar_repo))
        rebar.link(bi, ai, "blocks", repo_root=str(rebar_repo))
        i_out, i_err, i_code = _inproc(["transition", bi, "open", "closed", *out_flag], capsys)
        # bash store arm
        ab = rebar.create_ticket("task", "blocked A b", repo_root=str(rebar_repo))
        bb = rebar.create_ticket("task", "blocker B b", repo_root=str(rebar_repo))
        rebar.link(bb, ab, "blocks", repo_root=str(rebar_repo))
        b_out, b_err, b_code = _bash(["transition", bb, "open", "closed", *out_flag], rebar_repo)

        # Normalize both the closed ticket id and the unblocked dependent id.
        ni = _norm(_norm(i_out, bi), ai)
        nb = _norm(_norm(b_out, bb), ab)
        assert ni == nb, f"{fmt}: unblock stdout {i_out!r} vs {b_out!r}"
        assert i_code == b_code == 0
