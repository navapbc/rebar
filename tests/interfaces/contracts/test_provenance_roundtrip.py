"""Provenance write→read round-trip through the public library (P1.2 import seam).

T1 plumbs optional ``source`` provenance from the library/composer/leaf write path
into the CREATE/COMMENT event data, where the reducer surfaces it in compiled state.
This pins the end-to-end behavior over a real store: a ticket created with ``source``
shows the source_* fields, and a ticket created without it is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import rebar


def test_create_and_comment_with_source_surface_provenance(rebar_repo: Path) -> None:
    tid = rebar.create_ticket(
        "task",
        "imported",
        source={
            "source_id": "old-1111-2222-3333",
            "source_created_at": 1700000000000000000,
            "source_author": "Origin Author",
            "source_env": "origin-env",
        },
        repo_root=str(rebar_repo),
    )
    rebar.comment(
        tid,
        "ported note",
        source={"source_author": "Origin Commenter", "source_created_at": 1700000001000000000},
        repo_root=str(rebar_repo),
    )

    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))

    assert state["source_id"] == "old-1111-2222-3333"
    assert state["source_created_at"] == 1700000000000000000
    assert state["source_author"] == "Origin Author"
    assert state["source_env"] == "origin-env"

    assert len(state["comments"]) == 1
    entry = state["comments"][0]
    assert entry["source_author"] == "Origin Commenter"
    assert entry["source_created_at"] == 1700000001000000000


def test_create_without_source_has_no_provenance(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "normal", repo_root=str(rebar_repo))
    rebar.comment(tid, "plain note", repo_root=str(rebar_repo))

    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))

    for key in ("source_id", "source_created_at", "source_author", "source_env"):
        assert key not in state
    assert set(state["comments"][0]) == {"body", "author", "timestamp"}
