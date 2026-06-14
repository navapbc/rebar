"""Optimistic-concurrency exit-10 on ``transition`` (CLI + library).

The parametrized transition suite covers ``claim`` exit-10 but not ``transition``:
a ``transition <id> <WRONG-current> closed`` whose declared current status does
NOT match the ticket's actual status must be rejected as a stale write —
``rebar._cli.main`` returns exit 10 and the library ``rebar.transition`` raises
``rebar.ConcurrencyError`` — WITHOUT mutating the ticket (it stays open).

This uses an OPEN ticket and a wrong-current of ``in_progress``: the mismatch is
detected under the lock before any STATUS event is written, so the probe is cheap
and non-mutating (exactly the property the ticket called out).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import _cli


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def test_transition_wrong_current_cli_exits_10(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"

    # Declared current (in_progress) != actual (open) → optimistic-concurrency reject.
    rc = _cli.main(["transition", tid, "in_progress", "closed"])
    assert rc == 10
    assert _status(tid, rebar_repo) == "open"  # unchanged


def test_transition_wrong_current_library_raises_concurrency_error(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))

    with pytest.raises(rebar.ConcurrencyError):
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"  # unchanged
