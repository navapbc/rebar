"""A LINK write must persist even when stdout closes early (bug hulky-bag-aisle).

`add_dependency` emits a machine-readable REDIRECT record to stdout when a
blocking link is promoted up the hierarchy. That stdout write happened BEFORE the
durable LINK commit, so when the reader closed the pipe early (`rebar link ... |
head`), the `print` raised `BrokenPipeError` and the function aborted before
committing — the link silently did not persist, yet the pipeline's exit status
(head's) was 0. Non-promoted relations (e.g. relates_to) print nothing and so
always persisted, which is the asymmetry the report observed.

The contract: a write either persists the event or fails loudly — and the durable
commit must not be defeated by the reader closing stdout. So the redirect record
is emitted only AFTER the LINK is committed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import rebar
from rebar.graph import _links


class _BrokenStdout:
    """A stdout stand-in whose every write raises BrokenPipeError, simulating a
    reader (`| head`) that closed the pipe."""

    def write(self, _data: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def _cross_tier_promoted_link(repo: Path):
    """A task→epic blocking link that promotes to epic→epic (triggers the REDIRECT
    stdout emission), returning (source_task, resolved_source_epic, target_epic)."""
    target_epic = rebar.create_ticket("epic", "target epic", repo_root=str(repo))
    src_epic = rebar.create_ticket("epic", "source epic", repo_root=str(repo))
    story = rebar.create_ticket("story", "a story", parent=src_epic, repo_root=str(repo))
    task = rebar.create_ticket("task", "a task", parent=story, repo_root=str(repo))
    return task, src_epic, target_epic


def _link_event_count(tracker: Path, ticket_id: str) -> int:
    d = tracker / ticket_id
    return len(list(d.glob("*-LINK.json"))) if d.is_dir() else 0


def test_promoted_link_persists_despite_broken_pipe(rebar_repo: Path, monkeypatch):
    """The promoted blocks link must be committed even though the REDIRECT stdout
    write hits a closed pipe (the previously write-losing path)."""
    tracker = rebar_repo / ".tickets-tracker"
    task, src_epic, target_epic = _cross_tier_promoted_link(rebar_repo)
    before = _link_event_count(tracker, src_epic)

    # Simulate `| head` closing the pipe: stdout.write raises BrokenPipeError.
    monkeypatch.setattr(sys, "stdout", _BrokenStdout())
    # The redirect emit may still surface the pipe error (loud) AFTER the commit;
    # what must NOT happen is losing the write. Either outcome is acceptable here.
    try:
        _links.add_dependency(task, target_epic, str(tracker), "blocks")
    except BrokenPipeError:
        pass
    finally:
        monkeypatch.setattr(sys, "stdout", sys.__stdout__)

    after = _link_event_count(tracker, src_epic)
    assert after == before + 1, "promoted LINK was lost when stdout closed early"


def test_relates_to_unaffected_by_broken_pipe(rebar_repo: Path, monkeypatch):
    """Control: relates_to is never promoted (prints nothing), so it always
    persisted — confirming the test setup isolates the promotion-print path."""
    tracker = rebar_repo / ".tickets-tracker"
    a = rebar.create_ticket("task", "a", repo_root=str(rebar_repo))
    b = rebar.create_ticket("task", "b", repo_root=str(rebar_repo))
    before = _link_event_count(tracker, a)

    monkeypatch.setattr(sys, "stdout", _BrokenStdout())
    try:
        _links.add_dependency(a, b, str(tracker), "relates_to")
    except BrokenPipeError:
        pytest.fail("relates_to should not write to stdout, so no BrokenPipeError")
    finally:
        monkeypatch.setattr(sys, "stdout", sys.__stdout__)

    assert _link_event_count(tracker, a) == before + 1
