"""The soft-delete non-deleted-children guard must honor the EFFECTIVE parent_id
(full event history), not just the CREATE event — so a child REPARENTED onto a
ticket via ``edit --parent`` still blocks the delete instead of being silently
orphaned. This mirrors the transition open-children guard fix (commit 535bee1),
which closed the same CREATE-only blind spot. Part of ticket 4253.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import _cli


def _exists_live(ticket_id: str, repo: Path) -> bool:
    """True if the ticket is still present and not tombstoned."""
    d = repo / ".tickets-tracker" / ticket_id
    return d.is_dir() and not (d / ".tombstone.json").is_file()


def test_delete_blocked_by_direct_child(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Baseline (unchanged): a ticket with a live direct child can't be soft-deleted."""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rc = _cli.main(["delete", parent, "--user-approved"])
    err = capsys.readouterr().err
    assert rc != 0 and child in err and "children" in err.lower()
    assert _exists_live(parent, rebar_repo)  # not deleted


def test_delete_blocked_by_reparented_child(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression (4253): a child created WITHOUT a parent and later reparented onto
    `parent` via `edit --parent` must still block the delete. Its CREATE event
    records no parent, so a CREATE-only guard would miss it and orphan it."""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "orphan-then-reparented", repo_root=str(rebar_repo))
    assert _cli.main(["edit", child, "--parent", parent]) == 0  # reparent onto parent

    rc = _cli.main(["delete", parent, "--user-approved"])
    err = capsys.readouterr().err
    assert rc != 0, "reparented child must block the delete"
    assert child in err and "children" in err.lower()
    assert _exists_live(parent, rebar_repo) and _exists_live(child, rebar_repo)


def test_delete_freed_when_child_reparented_away(rebar_repo: Path) -> None:
    """The other direction: a child whose CREATE parented it HERE but was later
    reparented AWAY (`edit --parent <other>`) must NOT block — the original parent
    deletes cleanly, and the NEW parent now blocks instead. (A CREATE-only guard
    would wrongly keep blocking the original and miss the new one.)"""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    other = rebar.create_ticket("epic", "other parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    assert _cli.main(["edit", child, "--parent", other]) == 0  # move child to `other`

    assert _cli.main(["delete", parent, "--user-approved"]) == 0  # original is free now
    assert not _exists_live(parent, rebar_repo)
    assert _cli.main(["delete", other, "--user-approved"]) != 0  # new parent blocks
    assert _exists_live(other, rebar_repo) and _exists_live(child, rebar_repo)


def test_delete_allowed_once_reparented_child_is_deleted(rebar_repo: Path) -> None:
    """And once the (reparented) child is itself soft-deleted, the parent deletes
    cleanly — a tombstoned child does not block."""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", repo_root=str(rebar_repo))
    assert _cli.main(["edit", child, "--parent", parent]) == 0
    assert _cli.main(["delete", child, "--user-approved"]) == 0  # delete the child first
    assert _cli.main(["delete", parent, "--user-approved"]) == 0  # now parent is free
    assert not _exists_live(parent, rebar_repo)
