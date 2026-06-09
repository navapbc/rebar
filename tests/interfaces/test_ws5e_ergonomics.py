"""WS5e ergonomics: create-returns-alias + reopen convenience."""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar


def test_create_returns_id_and_alias(rebar_repo: Path) -> None:
    """create_ticket(return_alias=True) returns both id and alias so an agent
    doesn't need a second show()."""
    result = rebar.create_ticket("task", "alias me", return_alias=True)
    assert isinstance(result, dict)
    assert result["id"] and len(result["id"].replace("-", "")) >= 12
    # alias is the human-readable form; show() agrees.
    assert result["alias"] == (rebar.show_ticket(result["id"]) or {}).get("alias")

    # Backward-compat: default return is still the bare id string.
    tid = rebar.create_ticket("task", "plain")
    assert isinstance(tid, str) and tid


def test_reopen_closed_ticket(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "to reopen")
    rebar.transition(tid, "open", "closed")
    assert rebar.show_ticket(tid)["status"] == "closed"

    result = rebar.reopen(tid)
    assert result["status"] == "open"
    assert rebar.show_ticket(tid)["status"] == "open"


def test_reopen_not_closed_raises_concurrency(rebar_repo: Path) -> None:
    """reopen on a non-closed ticket is rejected via the optimistic-concurrency
    contract (it is transition closed->open; ticket is open)."""
    tid = rebar.create_ticket("task", "still open")
    with pytest.raises(rebar.ConcurrencyError):
        rebar.reopen(tid)
    assert rebar.show_ticket(tid)["status"] == "open"
