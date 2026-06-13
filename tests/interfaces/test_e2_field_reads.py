"""Tier E E2: get-file-impact / get-verify-commands — in-process vs dispatcher parity.

Dual-run parity gate: the in-process argparse arm (:func:`rebar._cli.main`) must
match the bash dispatcher byte-for-byte (stdout/stderr/exit) over one store, across
success / miss / arity / empty-id and both ``--output`` modes — the dispatcher being
the second, still-live implementation until the E7/E8 mass retirement.
"""

from __future__ import annotations

import json
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
        env=env,
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return cp.stdout, cp.stderr, cp.returncode


def _inproc(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[str, str, int]:
    capsys.readouterr()  # clear
    code = main(argv)
    cap = capsys.readouterr()
    return cap.out, cap.err, code


@pytest.fixture
def fixture(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    monkeypatch.setenv("_TICKET_TEST_NO_SYNC", "1")
    tid = rebar.create_ticket("task", "E2 field reads", repo_root=str(rebar_repo))
    rebar.set_file_impact(tid, [{"path": "a/b.py", "reason": "why—é"}], repo_root=str(rebar_repo))
    rebar.set_verify_commands(
        tid, [{"dd_id": "DD1", "dd_text": "t", "command": "pytest"}], repo_root=str(rebar_repo)
    )
    return rebar_repo, tid


def _scenarios(tid: str) -> list[list[str]]:
    return [
        ["get-file-impact", tid],
        ["get-file-impact", "NOPE"],
        ["get-file-impact", ""],
        ["get-file-impact"],
        ["get-verify-commands", tid],
        ["get-verify-commands", tid, "--output", "json"],
        ["get-verify-commands", "NOPE"],
        ["get-verify-commands", "NOPE", "--output", "json"],
        ["get-verify-commands", ""],
        ["get-verify-commands", "", "--output", "json"],
        ["get-verify-commands"],
        ["get-verify-commands", tid, "--output", "bogus"],
    ]


def test_field_read_parity(fixture, capsys: pytest.CaptureFixture[str]) -> None:
    repo, tid = fixture
    for argv in _scenarios(tid):
        b_out, b_err, b_code = _bash(argv, repo)
        i_out, i_err, i_code = _inproc(argv, capsys)
        assert i_out == b_out, f"{argv}: stdout (in-proc {i_out!r} vs bash {b_out!r})"
        assert i_err == b_err, f"{argv}: stderr (in-proc {i_err!r} vs bash {b_err!r})"
        assert i_code == b_code, f"{argv}: exit (in-proc {i_code} vs bash {b_code})"


def test_library_get_file_impact_inprocess(fixture) -> None:
    repo, tid = fixture
    assert rebar.get_file_impact(tid, repo_root=str(repo)) == [{"path": "a/b.py", "reason": "why—é"}]
    assert rebar.get_file_impact("NOPE", repo_root=str(repo)) == []  # [] on miss


def test_library_get_verify_commands_inprocess(fixture) -> None:
    repo, tid = fixture
    vc = rebar.get_verify_commands(tid, repo_root=str(repo))
    assert vc == [{"command": "pytest", "dd_id": "DD1", "dd_text": "t"}]
    with pytest.raises(rebar.RebarError) as exc:  # raises on miss (not [])
        rebar.get_verify_commands("NOPE", repo_root=str(repo))
    assert exc.value.returncode == 1


def test_success_output_shapes(fixture, capsys: pytest.CaptureFixture[str]) -> None:
    """file-impact is spaced (default separators); verify-commands is compact (jq -c)."""
    repo, tid = fixture
    fi_out, _, _ = _inproc(["get-file-impact", tid], capsys)
    assert fi_out == json.dumps([{"path": "a/b.py", "reason": "why—é"}], ensure_ascii=False) + "\n"
    vc_out, _, _ = _inproc(["get-verify-commands", tid], capsys)
    assert vc_out == '[{"command":"pytest","dd_id":"DD1","dd_text":"t"}]\n'
