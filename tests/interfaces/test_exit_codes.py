"""Conformance test for the canonical exit-code contract (sub-effort (a) of
story fatty-cipher-range / ticket urge-index-zoom).

The exit codes 0/1/2/10 are load-bearing for agents driving rebar over the CLI,
but were historically scattered through per-script headers with no single
source of truth. ``docs/exit-codes.md`` is now that source; this test pins the
load-bearing paths against it so the contract cannot silently drift.

Canonical contract (see docs/exit-codes.md for the full per-command table):
  0  — success
  1  — runtime error: ticket-not-found, invalid input value, missing required
        positional argument, failed precondition, or a gate "fail" verdict.
  2  — usage error: an unrecognized CLI ``--option`` (every read command rejects
        unknown options with 2), plus clarity-check's not-found/usage path (the
        gate overloads 1 as a fail VERDICT, so not-found cannot also be 1).
  10 — optimistic-concurrency mismatch (transition/claim/reopen state mismatch).

``validate`` is the one documented exception: its exit code is a 0-4 health
severity bucket, not the standard contract (and it takes NO ticket id).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import rebar

MISSING = "zzzz-zzzz-zzzz-0000"


def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _rc(*args: str, cwd: str) -> int:
    return _cli(*args, cwd=cwd).returncode


def _seed(repo: Path) -> str:
    """Create one open task carrying an Acceptance Criteria block (so the
    per-ticket gates have something well-formed to score), return its id."""
    return rebar.create_ticket(
        "task",
        "Conformance task",
        description="Body of a well-formed ticket.\n\n## Acceptance Criteria\n- [ ] a",
        repo_root=str(repo),
    )


# ── 0: success ────────────────────────────────────────────────────────────────
def test_success_paths_exit_0(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    r = str(rebar_repo)
    assert _rc("show", tid, cwd=r) == 0
    assert _rc("list", cwd=r) == 0
    assert _rc("ready", cwd=r) == 0
    assert _rc("deps", tid, cwd=r) == 0
    assert _rc("fsck", cwd=r) == 0


# ── 1: runtime errors ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "cmd",
    [
        ("show", MISSING),
        ("deps", MISSING),
        ("check-ac", MISSING),
        ("quality-check", MISSING),
        ("comment", MISSING, "body"),
        ("tag", MISSING, "atag"),
        ("transition", MISSING, "open", "closed"),
    ],
)
def test_ticket_not_found_exits_1(rebar_repo: Path, cmd: tuple) -> None:
    assert _rc(*cmd, cwd=str(rebar_repo)) == 1


@pytest.mark.parametrize(
    "cmd",
    [
        ("show",),  # missing required <ticket_id>
        ("create",),  # missing required <type> <title>
        ("deps",),  # missing required <ticket_id>
    ],
)
def test_missing_required_arg_exits_1(rebar_repo: Path, cmd: tuple) -> None:
    assert _rc(*cmd, cwd=str(rebar_repo)) == 1


def test_link_without_relation_exits_1(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    # link requires a relation arg; omitting it is a missing-arg (1), not 2.
    assert _rc("link", tid, tid, cwd=str(rebar_repo)) == 1


# ── 2: usage errors (unrecognized option) ─────────────────────────────────────
# Every read command rejects an unknown --option with exit 2. show and list were
# the historical stragglers (returned 1); the canonical contract is 2 uniformly.
@pytest.mark.parametrize(
    "base",
    [
        ("show",),
        ("list",),
        ("deps",),
        ("ready",),
        ("search", "q"),
    ],
)
def test_unknown_option_exits_2(rebar_repo: Path, base: tuple) -> None:
    tid = _seed(rebar_repo)
    args = tuple(a if a != "q" else "query" for a in base)
    # deps/show need a valid id before the option so we exercise the option path.
    if base[0] in ("show", "deps"):
        args = (base[0], tid, "--definitely-not-a-real-option")
    else:
        args = (*args, "--definitely-not-a-real-option")
    assert _rc(*args, cwd=str(rebar_repo)) == 2


def test_clarity_check_not_found_exits_2(rebar_repo: Path) -> None:
    # clarity-check uses 0=pass / 1=fail-VERDICT, so a not-found ticket cannot be
    # signalled with 1; it uses 2 (the gate convention, documented in exit-codes.md).
    assert _rc("clarity-check", MISSING, cwd=str(rebar_repo)) == 2


# ── 10: optimistic-concurrency mismatch ───────────────────────────────────────
def test_transition_stale_current_exits_10(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    # ticket is open; claiming current=="closed" is a stale-current mismatch.
    assert _rc("transition", tid, "closed", "open", cwd=str(rebar_repo)) == 10


def test_reopen_non_closed_exits_10(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    # reopen expects a CLOSED ticket; reopening an open one is a state mismatch.
    assert _rc("reopen", tid, cwd=str(rebar_repo)) == 10


# ── validate: documented exception (0-4 health bucket; takes NO ticket id) ─────
def test_validate_rejects_ticket_id(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    # Passing a ticket id to the repo-wide health check is a usage error.
    assert _rc("validate", tid, cwd=str(rebar_repo)) != 0
