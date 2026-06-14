"""Tier E E5: bridge-status — in-process vs dispatcher byte-parity.

bridge-status is a pure read (status file + BRIDGE_ALERT scan), so every scenario
runs over one shared store and compares ``rebar._cli.main`` (capsys) against the
bash dispatcher (subprocess) byte-for-byte.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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


def _assert_parity(argv: list[str], repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    b_out, b_err, b_code = _bash(argv, repo)
    i_out, i_err, i_code = _inproc(argv, capsys)
    assert i_out == b_out, f"{argv}: stdout {i_out!r} vs {b_out!r}"
    assert i_err == b_err, f"{argv}: stderr {i_err!r} vs {b_err!r}"
    assert i_code == b_code, f"{argv}: exit {i_code} vs {b_code}"


def _write_status(repo: Path, obj: dict) -> None:
    (repo / ".tickets-tracker" / ".bridge-status.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8"
    )


def _alert(repo: Path, tkt: str, ts: int, obj: dict) -> None:
    d = repo / ".tickets-tracker" / tkt
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ts}-BRIDGE_ALERT.json").write_text(json.dumps(obj), encoding="utf-8")


def test_missing_status_file_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["bridge-status"], ["bridge-status", "--output", "json"]):
        _assert_parity(argv, rebar_repo, capsys)


def test_arg_error_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_status(rebar_repo, {"last_run_timestamp": 5, "success": True, "unresolved_conflicts": 0})
    for argv in (
        ["bridge-status", "--bogus"],                 # unknown option
        ["bridge-status", "foo"],                     # unexpected argument
        ["bridge-status", "--output", "bogus"],       # bad format value
        ["bridge-status", "--output", "json", "--x"], # bad opt after a valid --output
    ):
        _assert_parity(argv, rebar_repo, capsys)


@pytest.mark.parametrize("fmt", [[], ["--output", "json"], ["-o", "json"]])
def test_status_present_parity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str], fmt: list[str]
) -> None:
    for obj in (
        {"last_run_timestamp": 1700000000, "success": True, "error": None, "unresolved_conflicts": 3},
        {"last_run_timestamp": 1700000050, "success": False, "error": "boom é", "unresolved_conflicts": 0},
        {},  # missing-field defaults
    ):
        _write_status(rebar_repo, obj)
        _assert_parity(["bridge-status", *fmt], rebar_repo, capsys)


def test_unresolved_alert_count_parity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_status(rebar_repo, {"last_run_timestamp": 1, "success": True, "unresolved_conflicts": 0})
    # tkt1: u1 stays unresolved; u2 resolved via resolves_uuid.
    _alert(rebar_repo, "tkt1", 100, {"uuid": "u1", "data": {}})
    _alert(rebar_repo, "tkt1", 200, {"uuid": "u2", "data": {}})
    _alert(rebar_repo, "tkt1", 300, {"uuid": "u3", "data": {"resolved": True, "resolves_uuid": "u2"}})
    # tkt2: u4 stays unresolved; u5 resolved via alert_uuid.
    _alert(rebar_repo, "tkt2", 100, {"uuid": "u4", "data": {}})
    _alert(rebar_repo, "tkt2", 150, {"uuid": "u5", "data": {}})
    _alert(rebar_repo, "tkt2", 200, {"uuid": "u6", "data": {"resolved": True, "alert_uuid": "u5"}})
    for argv in (["bridge-status"], ["bridge-status", "--output", "json"]):
        _assert_parity(argv, rebar_repo, capsys)
