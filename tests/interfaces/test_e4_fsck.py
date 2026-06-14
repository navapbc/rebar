"""Tier E E4: fsck — in-process vs dispatcher byte-parity.

fsck is non-destructive (its only mutation is stale-index.lock removal, not
exercised here), so scenarios dual-run on a shared store and compare exactly.
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


def _both(argv: list[str], repo: Path, capsys) -> None:
    b_out, b_err, b_code = _bash(argv, repo)
    i_out, i_err, i_code = _inproc(argv, capsys)
    assert i_out == b_out, f"{argv}: stdout {i_out!r} vs {b_out!r}"
    assert i_err == b_err, f"{argv}: stderr {i_err!r} vs {b_err!r}"
    assert i_code == b_code, f"{argv}: exit {i_code} vs {b_code}"


def test_fsck_clean_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rebar.create_ticket("task", "clean", repo_root=str(rebar_repo))
    _both(["fsck"], rebar_repo, capsys)
    _both(["fsck", "--output", "json"], rebar_repo, capsys)


def test_fsck_corrupt_and_missing_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid = rebar.create_ticket("task", "victim", repo_root=str(rebar_repo))
    tracker = rebar_repo / ".tickets-tracker"
    # Corrupt event file.
    (tracker / tid / "9999-deadbeef-COMMENT.json").write_text("{not json", encoding="utf-8")
    # Ghost dir: events but no CREATE.
    ghost = tracker / "ZZ-ghost"
    ghost.mkdir()
    (ghost / "1-a-COMMENT.json").write_text('{"event_type":"COMMENT"}', encoding="utf-8")

    _both(["fsck"], rebar_repo, capsys)
    _both(["fsck", "--output", "json"], rebar_repo, capsys)
    # Sanity: json shape carries both issue kinds.
    out, _, code = _inproc(["fsck", "--output", "json"], capsys)
    doc = json.loads(out)
    assert doc["issue_count"] == 2
    kinds = {i["kind"] for i in doc["issues"]}
    assert kinds == {"corrupt", "missing_create"}
    assert code == 1


def test_fsck_corrupt_create_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A CREATE event missing required fields → CORRUPT_CREATE (reducer fsck_needed)."""
    tracker = rebar_repo / ".tickets-tracker"
    d = tracker / "BB-badcreate"
    d.mkdir()
    # CREATE event without ticket_type/title.
    (d / "1-a-CREATE.json").write_text(
        '{"event_type":"CREATE","timestamp":1,"uuid":"a","data":{}}', encoding="utf-8"
    )
    _both(["fsck"], rebar_repo, capsys)
    _both(["fsck", "--output", "json"], rebar_repo, capsys)


def test_fsck_ignores_hidden_dirs(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Hidden dirs (.bridge_state, .git, …) are NOT ticket dirs — fsck must skip
    them (the bash `*/` glob does), not flag them MISSING_CREATE."""
    rebar.create_ticket("task", "real", repo_root=str(rebar_repo))
    (rebar_repo / ".tickets-tracker" / ".bridge_state").mkdir()
    _both(["fsck"], rebar_repo, capsys)
    out, _, code = _inproc(["fsck"], capsys)
    assert "no issues found" in out and code == 0


def test_fsck_library_clean(rebar_repo: Path) -> None:
    rebar.create_ticket("task", "lib clean", repo_root=str(rebar_repo))
    out = rebar.fsck(repo_root=str(rebar_repo))
    assert "fsck complete: no issues found" in out


def test_fsck_library_raises_on_issues(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "lib victim", repo_root=str(rebar_repo))
    (rebar_repo / ".tickets-tracker" / tid / "9-x-COMMENT.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(rebar.RebarError) as exc:
        rebar.fsck(repo_root=str(rebar_repo))
    assert exc.value.returncode == 1
