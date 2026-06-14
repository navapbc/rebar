"""Tier E E3: compact / compact-all — in-process vs dispatcher byte-parity.

Single-ticket ``compact`` gets a full dual-run (fresh ticket per impl, id
normalized, since success mutates). ``compact-all``'s per-ticket body is inherently
ticket-id-ordered, so it is checked behaviorally + on its id-independent lines
(headers / Done / Nothing-to-do) against the dispatcher.
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


def test_compact_nonmutating_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid = rebar.create_ticket("task", "compact nonmut", repo_root=str(rebar_repo))
    for argv in (
        ["compact"],                       # usage
        ["compact", "NOPE"],               # not-found
        ["compact", tid],                  # below threshold (default 10)
        ["compact", tid, "--bogus"],       # unknown arg
    ):
        b_out, b_err, b_code = _bash(argv, rebar_repo)
        i_out, i_err, i_code = _inproc(argv, capsys)
        assert i_out == b_out, f"{argv}: stdout {i_out!r} vs {b_out!r}"
        assert i_err == b_err, f"{argv}: stderr {i_err!r} vs {b_err!r}"
        assert i_code == b_code, f"{argv}: exit {i_code} vs {b_code}"


@pytest.mark.parametrize("tail", [["--threshold=0"], ["--threshold=0", "--skip-sync"]])
def test_compact_success_parity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str], tail: list[str]
) -> None:
    a = rebar.create_ticket("task", "compact inproc", repo_root=str(rebar_repo))
    i_out, i_err, i_code = _inproc(["compact", a, *tail], capsys)
    b = rebar.create_ticket("task", "compact bash", repo_root=str(rebar_repo))
    b_out, b_err, b_code = _bash(["compact", b, *tail], rebar_repo)
    assert _norm(i_out, a) == _norm(b_out, b), f"stdout {i_out!r} vs {b_out!r}"
    assert _norm(i_err, a) == _norm(b_err, b), f"stderr {i_err!r} vs {b_err!r}"
    assert i_code == b_code == 0
    # SNAPSHOT was created; originals collapsed.
    snaps = list((rebar_repo / ".tickets-tracker" / a).glob("*-SNAPSHOT.json"))
    assert len(snaps) == 1


def test_compact_remote_snapshot_skip_parity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = rebar.create_ticket("task", "compact twice i", repo_root=str(rebar_repo))
    _inproc(["compact", a, "--threshold=0", "--skip-sync"], capsys)
    i_out, i_err, i_code = _inproc(["compact", a, "--threshold=0"], capsys)  # sync on → remote-snapshot check

    b = rebar.create_ticket("task", "compact twice b", repo_root=str(rebar_repo))
    _bash(["compact", b, "--threshold=0", "--skip-sync"], rebar_repo)
    b_out, b_err, b_code = _bash(["compact", b, "--threshold=0"], rebar_repo)

    assert _norm(i_out, a) == _norm(b_out, b)
    assert _norm(i_err, a) == _norm(b_err, b)
    assert i_code == b_code == 0


def test_state_preserved_across_compact(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Compaction is state-preserving: the reduced status/title survive the SNAPSHOT."""
    a = rebar.create_ticket("task", "stateful", repo_root=str(rebar_repo))
    rebar.transition(a, "open", "in_progress", repo_root=str(rebar_repo))
    before = rebar.show_ticket(a, repo_root=str(rebar_repo))
    _inproc(["compact", a, "--threshold=0", "--skip-sync"], capsys)
    after = rebar.show_ticket(a, repo_root=str(rebar_repo))
    assert after["status"] == before["status"] == "in_progress"
    assert after["title"] == before["title"]


# ── compact-all: id-independent lines compared to bash + behavioral ───────────
def test_compact_all_idindependent_lines_parity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for _ in range(3):
        rebar.create_ticket("task", "ca", repo_root=str(rebar_repo))
    # in-proc compact-all
    i_out, i_err, i_code = _inproc(["compact-all"], capsys)
    # second run: nothing to do (all now snapshotted) — compare to bash nothing-to-do
    i_out2, _, _ = _inproc(["compact-all"], capsys)
    b_out2, _, b_code2 = _bash(["compact-all"], rebar_repo)

    # Behavioral on the first run:
    assert i_code == 0
    assert "Tickets needing compaction     : 3" in i_out
    assert "Done: 3 compacted, 0 errors (of 3 attempted)" in i_out
    assert i_out.count(".") >= 3  # one dot per compacted ticket
    # Nothing-to-do is id-independent → byte-equal to the dispatcher.
    assert i_out2 == b_out2
    assert i_out2.rstrip().endswith("Nothing to do.")


def test_compact_all_dry_run(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ids = [rebar.create_ticket("task", "dry", repo_root=str(rebar_repo)) for _ in range(2)]
    out, _, code = _inproc(["compact-all", "--dry-run"], capsys)
    assert code == 0
    assert "Dry-run — would compact:" in out
    for tid in ids:
        assert f"  {tid}" in out
    # dry-run writes nothing.
    for tid in ids:
        assert not list((rebar_repo / ".tickets-tracker" / tid).glob("*-SNAPSHOT.json"))
