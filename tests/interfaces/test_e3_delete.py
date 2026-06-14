"""Tier E E3: delete — in-process vs dispatcher byte-parity.

delete mutates (soft-delete + tombstone), so mutating scenarios run on a FRESH
ticket per impl (ids normalized); non-mutating guards (no --user-approved / arity /
not-found) run on a shared store.
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


def _norm(s: str, *ids: str) -> str:
    for i, tid in enumerate(ids):
        s = s.replace(tid, f"<ID{i}>")
    return s


def test_delete_nonmutating_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ["delete", "X"],                                  # no --user-approved
        ["delete", "--user-approved"],                    # arity (no id)
        ["delete", "NOPE", "--user-approved"],            # not-found text
        ["delete", "NOPE", "--user-approved", "--output", "json"],  # not-found json
    ):
        b_out, b_err, b_code = _bash(argv, rebar_repo)
        i_out, i_err, i_code = _inproc(argv, capsys)
        assert i_out == b_out, f"{argv}: stdout {i_out!r} vs {b_out!r}"
        assert i_err == b_err, f"{argv}: stderr {i_err!r} vs {b_err!r}"
        assert i_code == b_code, f"{argv}: exit {i_code} vs {b_code}"


def test_delete_children_guard_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pi = rebar.create_ticket("epic", "parent i", repo_root=str(rebar_repo))
    ci = rebar.create_ticket("task", "child i", parent=pi, repo_root=str(rebar_repo))
    i_out, i_err, i_code = _inproc(["delete", pi, "--user-approved"], capsys)

    pb = rebar.create_ticket("epic", "parent b", repo_root=str(rebar_repo))
    cb = rebar.create_ticket("task", "child b", parent=pb, repo_root=str(rebar_repo))
    b_out, b_err, b_code = _bash(["delete", pb, "--user-approved"], rebar_repo)

    assert _norm(i_err, pi, ci) == _norm(b_err, pb, cb), f"{i_err!r} vs {b_err!r}"
    assert i_code == b_code == 1


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_delete_success_parity(rebar_repo: Path, capsys: pytest.CaptureFixture[str], fmt: str) -> None:
    out_flag = ["--output", "json"] if fmt == "json" else []
    a = rebar.create_ticket("task", "del i", repo_root=str(rebar_repo))
    i_out, i_err, i_code = _inproc(["delete", a, "--user-approved", *out_flag], capsys)
    b = rebar.create_ticket("task", "del b", repo_root=str(rebar_repo))
    b_out, b_err, b_code = _bash(["delete", b, "--user-approved", *out_flag], rebar_repo)

    assert _norm(i_out, a) == _norm(b_out, b), f"{fmt}: {i_out!r} vs {b_out!r}"
    assert i_code == b_code == 0
    # tombstone written; reduced status is deleted.
    assert (rebar_repo / ".tickets-tracker" / a / ".tombstone.json").is_file()
    assert rebar.show_ticket(a, repo_root=str(rebar_repo))["status"] == "deleted"


def test_delete_writes_unlink_for_linked_ticket(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Deleting a ticket that another ticket depends_on writes an UNLINK so the
    surviving ticket's dep is cleared."""
    keep = rebar.create_ticket("task", "keeper", repo_root=str(rebar_repo))
    goner = rebar.create_ticket("task", "goner", repo_root=str(rebar_repo))
    rebar.link(keep, goner, "depends_on", repo_root=str(rebar_repo))
    assert rebar.show_ticket(keep, repo_root=str(rebar_repo))["deps"], "precondition: keep has a dep"

    _inproc(["delete", goner, "--user-approved"], capsys)

    # The surviving ticket's dep on the deleted ticket is gone (UNLINK applied).
    deps = rebar.show_ticket(keep, repo_root=str(rebar_repo))["deps"]
    assert all(d.get("target_id") != goner for d in deps), f"dep not unlinked: {deps}"


def test_delete_idempotent_reinvocation(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = rebar.create_ticket("task", "twice i", repo_root=str(rebar_repo))
    _inproc(["delete", a, "--user-approved"], capsys)
    i_out, i_err, i_code = _inproc(["delete", a, "--user-approved"], capsys)  # already tombstoned

    b = rebar.create_ticket("task", "twice b", repo_root=str(rebar_repo))
    _bash(["delete", b, "--user-approved"], rebar_repo)
    b_out, b_err, b_code = _bash(["delete", b, "--user-approved"], rebar_repo)

    assert i_out == b_out == ""   # re-invocation is silent
    assert i_err == b_err == ""
    assert i_code == b_code == 0
