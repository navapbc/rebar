"""Provenance surfacing (P1.2 import): source_* on CREATE and COMMENT events.

`rebar import` re-creates a ticket in a fresh store via the normal locked write
path, recording the ORIGINAL store's id/date/author/env as source_* on the CREATE
event (and the original comment author/time on each COMMENT). The reducer surfaces
these additively — present only when the event carried them, so a normally-created
ticket's compiled state is byte-for-byte unchanged. These tests pin both halves.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, _UUID2, _write_event


@pytest.mark.unit
@pytest.mark.scripts
def test_create_with_provenance_surfaces_source_fields(tmp_path: Path, reducer: ModuleType) -> None:
    """A CREATE carrying source_* surfaces all four ticket-level provenance fields."""
    ticket_dir = tmp_path / "tkt-prov"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Imported ticket",
            "source_id": "abcd-1234-5678-9012",
            "source_created_at": 1700000000111222333,
            "source_author": "Original Author",
            "source_env": "old-env-uuid",
        },
        author="importer",
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    # Local identity uses the new event's fields...
    assert state["author"] == "importer"
    assert state["created_at"] == 1742605200
    # ...while provenance preserves the source store's identity.
    assert state["source_id"] == "abcd-1234-5678-9012"
    assert state["source_created_at"] == 1700000000111222333
    assert state["source_author"] == "Original Author"
    assert state["source_env"] == "old-env-uuid"


@pytest.mark.unit
@pytest.mark.scripts
def test_create_without_provenance_omits_source_fields(tmp_path: Path, reducer: ModuleType) -> None:
    """A normal CREATE (no source_*) leaves the state free of provenance keys."""
    ticket_dir = tmp_path / "tkt-noprov"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Normal ticket"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    for key in ("source_id", "source_created_at", "source_author", "source_env"):
        assert key not in state, f"non-imported ticket must not carry {key}"


@pytest.mark.unit
@pytest.mark.scripts
def test_comment_provenance_surfaces_on_entry(tmp_path: Path, reducer: ModuleType) -> None:
    """A COMMENT carrying source_* surfaces them on the comment entry, present-only."""
    ticket_dir = tmp_path / "tkt-comment-prov"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Has comments"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="COMMENT",
        data={
            "body": "ported comment",
            "source_author": "Commenter",
            "source_created_at": 1699999999000000000,
        },
        author="importer",
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["comments"]) == 1
    entry = state["comments"][0]
    assert entry["body"] == "ported comment"
    assert entry["author"] == "importer"  # local event author
    assert entry["source_author"] == "Commenter"
    assert entry["source_created_at"] == 1699999999000000000


@pytest.mark.unit
@pytest.mark.scripts
def test_comment_without_provenance_keeps_existing_shape(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A normal COMMENT entry carries no source_* keys (shape unchanged)."""
    ticket_dir = tmp_path / "tkt-comment-noprov"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Has comments"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="COMMENT",
        data={"body": "normal comment"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    entry = state["comments"][0]
    assert set(entry) == {"body", "author", "timestamp"}
