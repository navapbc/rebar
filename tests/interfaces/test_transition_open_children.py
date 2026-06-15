"""Closing a parent is guarded by its OPEN direct children.

Correctness port of tests/scripts/test-ticket-transition-open-children-perf.sh
(the bash engine is being deleted). That suite was primarily a complexity
benchmark (the open-children scan must be O(children), not O(total_tickets)); the
wall-clock assertion is intentionally DROPPED here (flaky under pytest). What is
preserved is the behavioral contract the perf fix protected:

  * closing a parent with open children is rejected (exit 1, lists the children),
    and the parent stays open;
  * ``--force`` closes the parent anyway;
  * closing the child first lets the parent close;
  * the guard counts ONLY direct children (by parent_id) — unrelated tickets in
    the store never inflate the count (the "targeted lookup, not full scan" intent).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import _cli


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def _cli_run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[str, int]:
    capsys.readouterr()
    rc = _cli.main(argv)
    cap = capsys.readouterr()
    return cap.out + cap.err, rc


def test_close_parent_with_open_child_is_blocked(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    out, rc = _cli_run(["transition", parent, "open", "closed"], capsys)
    assert rc == 1
    assert "unresolved" in out and child in out
    assert _status(parent, rebar_repo) == "open"  # not closed


def test_force_closes_parent_despite_open_child(rebar_repo: Path) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rc = _cli.main(["transition", parent, "open", "closed", "--force"])
    assert rc == 0
    assert _status(parent, rebar_repo) == "closed"


def test_closing_child_first_lets_parent_close(rebar_repo: Path) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))
    rebar.transition(parent, "open", "closed", repo_root=str(rebar_repo))
    assert _status(parent, rebar_repo) == "closed"


def test_guard_counts_only_direct_children_not_unrelated(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    children = [
        rebar.create_ticket("task", f"child {i}", parent=parent, repo_root=str(rebar_repo))
        for i in range(20)
    ]
    # Unrelated tickets that must NOT be counted by the open-children guard.
    for i in range(15):
        rebar.create_ticket("task", f"unrelated {i}", repo_root=str(rebar_repo))

    out, rc = _cli_run(["transition", parent, "open", "closed"], capsys)
    assert rc == 1
    assert "20 unresolved" in out, f"guard miscounted direct children:\n{out}"

    # Close every child → parent now closes cleanly.
    for c in children:
        rebar.transition(c, "open", "closed", repo_root=str(rebar_repo))
    rebar.transition(parent, "open", "closed", repo_root=str(rebar_repo))
    assert _status(parent, rebar_repo) == "closed"
