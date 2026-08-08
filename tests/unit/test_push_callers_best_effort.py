"""Compatibility contracts for callers that must not opt into strict delivery."""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar._store import event_append, lock, push

pytestmark = pytest.mark.unit


def test_event_and_batch_append_keep_the_default_push_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(event_append, "stage_and_commit", lambda *_a, **_k: 1)
    monkeypatch.setattr(event_append, "batch_stage_and_commit", lambda *_a, **_k: 1)
    monkeypatch.setattr(lock, "canonical_tracker", lambda _tracker: str(tracker))
    monkeypatch.setattr(event_append, "_maybe_enrich_drain", lambda _tracker: None)
    monkeypatch.setattr(
        push,
        "push_tickets_branch",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert event_append.write_and_push(tracker, "1111-test-1111-1111", {}) == 1
    assert event_append.batch_write_and_push(tracker, [("1111-test-1111-1111", {})]) == 1

    assert len(calls) == 2
    assert all(args == (str(tracker),) and kwargs == {} for args, kwargs in calls)


def test_push_after_commit_delegates_to_the_default_core_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(lock, "canonical_tracker", lambda _tracker: str(tracker))
    monkeypatch.setattr(
        push,
        "push_tickets_branch",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    push.push_after_commit(tracker)

    assert calls == [((str(tracker),), {})]
