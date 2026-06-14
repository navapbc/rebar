"""Tier E E3: scratch set/get/clear — in-process vs dispatcher byte-parity.

Scratch output echoes the input id/key (deterministic) except ``get`` hit, whose
stored ``ts`` is wall-clock — that one is compared with ``ts`` normalized.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from rebar._cli import main
from rebar._engine import dispatcher, engine_env


def _bash(argv: list[str], repo: Path) -> tuple[str, str, int]:
    cp = subprocess.run(
        ["bash", str(dispatcher()), *argv],
        env=engine_env(str(repo)), cwd=str(repo), capture_output=True, text=True,
    )
    return cp.stdout, cp.stderr, cp.returncode


def _inproc(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[str, str, int]:
    capsys.readouterr()
    code = main(argv)
    cap = capsys.readouterr()
    return cap.out, cap.err, code


def _both_equal(argv: list[str], repo: Path, capsys) -> None:
    b_out, b_err, b_code = _bash(argv, repo)
    i_out, i_err, i_code = _inproc(argv, capsys)
    assert i_out == b_out, f"{argv}: stdout {i_out!r} vs {b_out!r}"
    assert i_err == b_err, f"{argv}: stderr {i_err!r} vs {b_err!r}"
    assert i_code == b_code, f"{argv}: exit {i_code} vs {b_code}"


def test_scratch_deterministic_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ["scratch"],                              # usage
        ["scratch", "bogus"],                     # unknown verb (compact json)
        ["scratch", "set", "T1", "k", "hello"],   # set ok (echoes id/key)
        ["scratch", "get", "T1", "missingkey"],   # miss
        ["scratch", "get", "T1", "../etc"],       # invalid_key (leading dot)
        ["scratch", "get", ".hidden", "k"],       # invalid_id (leading dot)
        ["scratch", "set", "T1"],                 # set arity
        ["scratch", "get", "T1"],                 # get arity
        ["scratch", "clear", "T1", "neverset"],   # clear missing key (removed 0)
        ["scratch", "clear"],                     # clear missing args
    ):
        _both_equal(argv, rebar_repo, capsys)


def test_scratch_get_hit_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ts_re = re.compile(r'"ts": "[^"]*"')

    def norm(s: str) -> str:
        return ts_re.sub('"ts": "<TS>"', s)

    # in-proc set+get
    _inproc(["scratch", "set", "H", "ki", "payload-é"], capsys)
    i_out, i_err, i_code = _inproc(["scratch", "get", "H", "ki"], capsys)
    # bash set+get (separate key)
    _bash(["scratch", "set", "H", "kb", "payload-é"], rebar_repo)
    b_out, b_err, b_code = _bash(["scratch", "get", "H", "kb"], rebar_repo)

    assert norm(i_out) == norm(b_out), f"{i_out!r} vs {b_out!r}"
    assert i_code == b_code == 0
    assert json.loads(i_out)["value"] == "payload-é"


def test_scratch_clear_removed_count_parity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # single-key removed=1
    _inproc(["scratch", "set", "C", "k1", "v"], capsys)
    i_out, _, _ = _inproc(["scratch", "clear", "C", "k1"], capsys)
    _bash(["scratch", "set", "C", "k1", "v"], rebar_repo)
    b_out, _, _ = _bash(["scratch", "clear", "C", "k1"], rebar_repo)
    assert i_out == b_out == '{"status": "ok", "ticket_id": "C", "key": "k1", "removed": 1}\n'

    # whole-ticket removed=count
    for k in ("a", "b", "c"):
        _inproc(["scratch", "set", "W", k, "v"], capsys)
    iw_out, _, _ = _inproc(["scratch", "clear", "W"], capsys)
    assert iw_out == '{"status": "ok", "ticket_id": "W", "removed": 3}\n'
    # dir is gone
    assert not (rebar_repo / ".rebar" / "scratch" / "W").exists()


def test_scratch_oversize_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    big = "x" * 100000  # > 98304 byte cap
    _both_equal(["scratch", "set", "O", "k", big], rebar_repo, capsys)
