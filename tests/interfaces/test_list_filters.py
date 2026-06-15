"""Exhaustive tests for the generalized ``list`` filters (worm-burr-fly):
``children_count``, ``--min-children``, and ``--unblocked`` / ``--blocked``.

Covers the library path (``rebar.list_tickets``) and CLI parity (``rebar.cli``),
plus edge cases: leaves, deleted children, closed/in-progress children, the
min-children boundary, blocking transitions, and validation of bad input.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import rebar


# ── helpers ──────────────────────────────────────────────────────────────────
def _env(repo: Path) -> dict:
    e = dict(os.environ)
    e["REBAR_ROOT"] = str(repo)
    e["PROJECT_ROOT"] = str(repo)
    return e


def _cli_list(repo: Path, *args: str):
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "list", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_env(repo),
    )
    return cp


def _cli_list_ids(repo: Path, *args: str) -> set:
    cp = _cli_list(repo, *args)
    assert cp.returncode == 0, cp.stderr
    return {t["ticket_id"] for t in json.loads(cp.stdout)}


def _ids(states) -> set:
    return {t["ticket_id"] for t in states}


def _by_alias(repo: Path, alias_substr: str) -> dict:
    for t in rebar.list_tickets(include_archived=True, repo_root=str(repo)):
        if alias_substr in (t.get("title") or ""):
            return t
    raise AssertionError(f"no ticket with title containing {alias_substr!r}")


@pytest.fixture
def scenario(rebar_repo: Path) -> dict:
    """A store with known child-counts and a blocking pair.

    E1: 3 live children (open, closed, in_progress) + 1 deleted (not counted) → 3
    E2: 1 child → 1 ; E3: 0 children → 0
    Blocking (root tasks): BLK(open) blocks BLKD; UNB has no blockers.
    """
    r = str(rebar_repo)
    s: dict = {}
    s["E1"] = rebar.create_ticket("epic", "epic-E1", repo_root=r)
    s["cA"] = rebar.create_ticket("task", "child-cA", parent=s["E1"], repo_root=r)  # open
    s["cB"] = rebar.create_ticket("task", "child-cB", parent=s["E1"], repo_root=r)
    rebar.claim(s["cB"], assignee="x", repo_root=r)
    rebar.transition(s["cB"], "in_progress", "closed", repo_root=r)  # closed
    s["cC"] = rebar.create_ticket("task", "child-cC", parent=s["E1"], repo_root=r)
    rebar.claim(s["cC"], assignee="x", repo_root=r)  # in_progress
    s["cD"] = rebar.create_ticket("task", "child-cD", parent=s["E1"], repo_root=r)
    subprocess.run(  # delete cD → must not count toward E1
        [sys.executable, "-m", "rebar.cli", "delete", s["cD"], "--user-approved"],
        cwd=r,
        capture_output=True,
        text=True,
        env=_env(rebar_repo),
    )
    s["E2"] = rebar.create_ticket("epic", "epic-E2", repo_root=r)
    s["e2c"] = rebar.create_ticket("task", "child-e2c", parent=s["E2"], repo_root=r)
    s["E3"] = rebar.create_ticket("epic", "epic-E3", repo_root=r)
    s["BLK"] = rebar.create_ticket("task", "task-BLK", repo_root=r)
    s["BLKD"] = rebar.create_ticket("task", "task-BLKD", repo_root=r)
    rebar.link(s["BLK"], s["BLKD"], "blocks", repo_root=r)  # BLK blocks BLKD
    s["UNB"] = rebar.create_ticket("task", "task-UNB", repo_root=r)
    s["repo"] = rebar_repo
    return s


# ── children_count (opt-in) ──────────────────────────────────────────────────
def test_children_count_absent_by_default(scenario):
    # OPT-IN: default list shape stays identical to show/search (single-reducer
    # invariant, bug f026) — children_count appears only with with_children_count.
    for t in rebar.list_tickets(repo_root=str(scenario["repo"])):
        assert "children_count" not in t


def test_children_count_present_when_opted_in(scenario):
    for t in rebar.list_tickets(with_children_count=True, repo_root=str(scenario["repo"])):
        assert "children_count" in t and isinstance(t["children_count"], int)


def test_children_count_values(scenario):
    r = str(scenario["repo"])
    counts = {
        t["ticket_id"]: t["children_count"]
        for t in rebar.list_tickets(with_children_count=True, repo_root=r)
    }
    assert counts[scenario["E1"]] == 3  # cA, cB(closed), cC(in_progress); cD deleted excluded
    assert counts[scenario["E2"]] == 1
    assert counts[scenario["E3"]] == 0
    assert counts[scenario["cA"]] == 0  # leaf


def test_children_count_excludes_deleted_child(scenario):
    e1 = next(
        t
        for t in rebar.list_tickets(with_children_count=True, repo_root=str(scenario["repo"]))
        if t["ticket_id"] == scenario["E1"]
    )
    assert e1["children_count"] == 3, "deleted child cD must not be counted"


def test_cli_with_children_count_flag(scenario):
    # default CLI list has no children_count; --with-children-count adds it
    cp = _cli_list(scenario["repo"], "--type=epic")
    assert all("children_count" not in t for t in json.loads(cp.stdout))
    cp = _cli_list(scenario["repo"], "--type=epic", "--with-children-count")
    assert all("children_count" in t for t in json.loads(cp.stdout))


# ── --min-children ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "n,present,absent",
    [
        (3, ["E1"], ["E2", "E3"]),
        (1, ["E1", "E2"], ["E3"]),
    ],
)
def test_min_children_boundary(scenario, n, present, absent):
    r = str(scenario["repo"])
    ids = _ids(rebar.list_tickets(ticket_type="epic", min_children=n, repo_root=r))
    for k in present:
        assert scenario[k] in ids, f"{k} (>= {n} children) should be present"
    for k in absent:
        assert scenario[k] not in ids, f"{k} (< {n} children) should be absent"


def test_min_children_zero_is_noop(scenario):
    r = str(scenario["repo"])
    assert _ids(rebar.list_tickets(min_children=0, repo_root=r)) == _ids(
        rebar.list_tickets(repo_root=r)
    )


def test_min_children_above_max_is_empty(scenario):
    assert rebar.list_tickets(min_children=99, repo_root=str(scenario["repo"])) == []


def test_min_children_rejects_non_integer(scenario):
    cp = _cli_list(scenario["repo"], "--min-children=abc")
    assert cp.returncode == 2
    assert "min-children" in cp.stderr


# ── --unblocked / --blocked ──────────────────────────────────────────────────
def test_blocked_includes_only_active_with_open_blocker(scenario):
    r = str(scenario["repo"])
    blocked = _ids(rebar.list_tickets(ticket_type="task", blocking_state="blocked", repo_root=r))
    assert scenario["BLKD"] in blocked  # blocked by open BLK
    assert scenario["BLK"] not in blocked  # no blockers
    assert scenario["UNB"] not in blocked  # no blockers
    assert scenario["cB"] not in blocked  # closed → not active


def test_unblocked_excludes_blocked(scenario):
    r = str(scenario["repo"])
    unblocked = _ids(
        rebar.list_tickets(ticket_type="task", blocking_state="unblocked", repo_root=r)
    )
    assert scenario["BLKD"] not in unblocked
    assert scenario["BLK"] in unblocked
    assert scenario["UNB"] in unblocked
    assert scenario["cC"] in unblocked  # in_progress, no blockers


def test_blocking_state_transitions_on_blocker_close(scenario):
    r = str(scenario["repo"])
    # close the blocker → BLKD becomes unblocked
    rebar.claim(scenario["BLK"], assignee="x", repo_root=r)
    rebar.transition(scenario["BLK"], "in_progress", "closed", repo_root=r)
    blocked = _ids(rebar.list_tickets(ticket_type="task", blocking_state="blocked", repo_root=r))
    unblocked = _ids(
        rebar.list_tickets(ticket_type="task", blocking_state="unblocked", repo_root=r)
    )
    assert scenario["BLKD"] not in blocked
    assert scenario["BLKD"] in unblocked


# ── CLI ↔ library parity ─────────────────────────────────────────────────────
def test_cli_library_parity_min_children(scenario):
    r = scenario["repo"]
    cli = _cli_list_ids(r, "--type=epic", "--min-children=1")
    lib = _ids(rebar.list_tickets(ticket_type="epic", min_children=1, repo_root=str(r)))
    assert cli == lib


def test_cli_library_parity_unblocked(scenario):
    r = scenario["repo"]
    cli = _cli_list_ids(r, "--type=task", "--unblocked")
    lib = _ids(rebar.list_tickets(ticket_type="task", blocking_state="unblocked", repo_root=str(r)))
    assert cli == lib


def test_cli_blocked_flag(scenario):
    cli = _cli_list_ids(scenario["repo"], "--type=task", "--blocked")
    assert scenario["BLKD"] in cli and scenario["UNB"] not in cli
