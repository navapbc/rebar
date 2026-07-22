"""Held-out transaction contract for the pre-STATUS callback."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._commands import txn
from rebar._commands._seam import CommandError
from rebar._engine_support.resolver import resolve_ticket_id


def _resolved(ticket_id: str, repo: Path) -> tuple[str, str]:
    tracker = str(config.tracker_dir(str(repo)))
    resolved = resolve_ticket_id(ticket_id, tracker)
    assert resolved is not None
    return tracker, resolved


def _status_event_count(tracker: str, ticket_id: str) -> int:
    return len(
        [
            name
            for name in os.listdir(os.path.join(tracker, ticket_id))
            if name.endswith("-STATUS.json")
        ]
    )


def test_pre_status_check_receives_fresh_locked_state_and_can_abort_without_event(
    rebar_repo: Path,
) -> None:
    tid = rebar.create_ticket("task", "locked precheck", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    tracker, resolved = _resolved(tid, rebar_repo)
    before = _status_event_count(tracker, resolved)
    observed: list[dict] = []

    def reject(state):  # type: ignore[no-untyped-def]
        observed.append(dict(state))
        raise CommandError("locked policy rejected", returncode=1)

    with pytest.raises(CommandError, match="locked policy rejected"):
        txn.transition_core(
            tracker,
            resolved,
            "in_progress",
            "closed",
            env_id="test-env",
            author="test",
            pre_status_check=reject,
            repo_root=str(rebar_repo),
        )

    assert observed and observed[0]["ticket_id"] == resolved
    assert observed[0]["status"] == "in_progress"
    assert rebar.show_ticket(resolved, repo_root=str(rebar_repo))["status"] == "in_progress"
    assert _status_event_count(tracker, resolved) == before


def test_omitted_pre_status_check_preserves_existing_transition(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "ordinary transition", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    tracker, resolved = _resolved(tid, rebar_repo)

    txn.transition_core(
        tracker,
        resolved,
        "in_progress",
        "closed",
        env_id="test-env",
        author="test",
        repo_root=str(rebar_repo),
    )

    assert rebar.show_ticket(resolved, repo_root=str(rebar_repo))["status"] == "closed"
