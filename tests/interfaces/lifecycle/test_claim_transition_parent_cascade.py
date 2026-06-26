"""Parent-first cascade for ``claim`` and ``transition`` (``open -> in_progress``).

Grabbing a child grabs its OPEN parent first. When a client claims a ticket, or
transitions it ``open -> in_progress``, and the ticket has a parent that is still
``open``, the same operation runs on the parent first (recursively up the chain)
BEFORE the child. A parent that is already ``in_progress`` / ``closed`` is not
cascaded. If the parent operation fails, the child operation is NOT attempted and
the error names the parent as the cause. Cascading is cycle-safe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import _cli
from rebar._commands import txn
from rebar._commands.txn import ConcurrencyMismatch


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def _assignee(tid: str, repo: Path) -> str | None:
    return rebar.show_ticket(tid, repo_root=str(repo)).get("assignee")


# --------------------------------------------------------------------------- claim


def test_claim_child_claims_open_parent_first(rebar_repo: Path) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    rebar.claim(child, assignee="alice", repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "in_progress"  # cascaded
    assert _assignee(parent, rebar_repo) == "alice"  # same assignee


def test_claim_cascades_through_multiple_open_levels(rebar_repo: Path) -> None:
    grand = rebar.create_ticket("epic", "grand", repo_root=str(rebar_repo))
    parent = rebar.create_ticket("story", "parent", parent=grand, repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    rebar.claim(child, assignee="bob", repo_root=str(rebar_repo))

    for t in (grand, parent, child):
        assert _status(t, rebar_repo) == "in_progress", f"{t} not cascaded"


def test_claim_does_not_cascade_when_parent_already_in_progress(rebar_repo: Path) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    # Parent already grabbed by someone else.
    rebar.claim(parent, assignee="owner", repo_root=str(rebar_repo))

    rebar.claim(child, assignee="alice", repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "in_progress"
    # Parent untouched by the child claim — still its original assignee.
    assert _assignee(parent, rebar_repo) == "owner"


def test_claim_parentless_ticket_unaffected(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "solo", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


def test_claim_parent_failure_aborts_child_with_attributed_error(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    orig = txn.claim_core

    def fake_claim_core(tracker, ticket_id, **kw):  # type: ignore[no-untyped-def]
        if ticket_id == parent:
            raise ConcurrencyMismatch("simulated parent claim failure")
        return orig(tracker, ticket_id, **kw)

    monkeypatch.setattr(txn, "claim_core", fake_claim_core)

    # A parent concurrency conflict must keep the concurrency identity at the leaf
    # (exit-10 / ConcurrencyError), so the "pick another" retry path still fires.
    with pytest.raises(rebar.ConcurrencyError) as ei:
        rebar.claim(child, assignee="alice", repo_root=str(rebar_repo))

    assert ei.value.returncode == 10
    msg = str(ei.value)
    assert parent in msg, f"error must name the parent: {msg}"
    assert child in msg
    assert "parent" in msg.lower()
    # Child was NOT claimed.
    assert _status(child, rebar_repo) == "open"


def test_claim_parent_succeeds_then_child_fails_is_not_rolled_back(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No rollback: if the parent claim succeeds and the CHILD then races, the parent
    stays in_progress (documented, conservative direction) and the child stays open."""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    orig = txn.claim_core

    def fake_claim_core(tracker, ticket_id, **kw):  # type: ignore[no-untyped-def]
        if ticket_id == child:
            raise ConcurrencyMismatch("simulated child race")
        return orig(tracker, ticket_id, **kw)

    monkeypatch.setattr(txn, "claim_core", fake_claim_core)

    with pytest.raises(rebar.ConcurrencyError):
        rebar.claim(child, assignee="alice", repo_root=str(rebar_repo))

    assert _status(parent, rebar_repo) == "in_progress"  # NOT rolled back
    assert _status(child, rebar_repo) == "open"


def test_cli_claim_parent_failure_propagates_exit_10(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI returns exit 10 (not 1) when the cascade fails on a raced parent."""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    orig = txn.claim_core

    def fake_claim_core(tracker, ticket_id, **kw):  # type: ignore[no-untyped-def]
        if ticket_id == parent:
            raise ConcurrencyMismatch("simulated parent claim failure")
        return orig(tracker, ticket_id, **kw)

    monkeypatch.setattr(txn, "claim_core", fake_claim_core)

    rc = _cli.main(["claim", child, "--assignee", "alice"])
    out = capsys.readouterr()
    assert rc == 10
    assert parent in (out.out + out.err)
    assert _status(child, rebar_repo) == "open"


def test_claim_does_not_cascade_when_parent_blocked(rebar_repo: Path) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rebar.transition(parent, "open", "blocked", repo_root=str(rebar_repo))

    rebar.claim(child, assignee="alice", repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "blocked"  # not cascaded


# ----------------------------------------------------------------------- transition


def test_transition_child_to_in_progress_cascades_to_open_parent(rebar_repo: Path) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "in_progress"  # cascaded


def test_transition_cascades_through_multiple_open_levels(rebar_repo: Path) -> None:
    grand = rebar.create_ticket("epic", "grand", repo_root=str(rebar_repo))
    parent = rebar.create_ticket("story", "parent", parent=grand, repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))

    for t in (grand, parent, child):
        assert _status(t, rebar_repo) == "in_progress", f"{t} not cascaded"


def test_transition_does_not_cascade_when_parent_in_progress(rebar_repo: Path) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rebar.transition(parent, "open", "in_progress", repo_root=str(rebar_repo))

    rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))
    assert _status(child, rebar_repo) == "in_progress"  # no error


def test_close_does_not_cascade_to_parent(rebar_repo: Path) -> None:
    """Only ``open -> in_progress`` cascades; closing a child leaves the parent open."""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))
    assert _status(child, rebar_repo) == "closed"
    assert _status(parent, rebar_repo) == "open"  # untouched


def test_transition_parent_failure_aborts_child_with_attributed_error(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    orig = txn.transition_core

    def fake_transition_core(tracker, ticket_id, current, target, **kw):  # type: ignore[no-untyped-def]
        if ticket_id == parent:
            raise ConcurrencyMismatch("simulated parent transition failure")
        return orig(tracker, ticket_id, current, target, **kw)

    monkeypatch.setattr(txn, "transition_core", fake_transition_core)

    with pytest.raises(rebar.ConcurrencyError) as ei:
        rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))

    assert ei.value.returncode == 10
    msg = str(ei.value)
    assert parent in msg, f"error must name the parent: {msg}"
    assert child in msg
    assert _status(child, rebar_repo) == "open"  # child NOT transitioned


def test_claim_with_parent_cycle_terminates(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cycle guard: a malformed A<->B parent cycle must not recurse forever. The
    claim terminates (a broken guard would hang / RecursionError) and succeeds."""
    a = rebar.create_ticket("task", "A", repo_root=str(rebar_repo))
    b = rebar.create_ticket("task", "B", parent=a, repo_root=str(rebar_repo))
    # Close the loop: A's parent becomes B (B's parent is already A) -> A<->B cycle.
    _cli.main(["edit", a, "--parent", b])
    capsys.readouterr()

    rc = _cli.main(["claim", a, "--assignee", "alice"])
    capsys.readouterr()
    assert rc == 0
    assert _status(a, rebar_repo) == "in_progress"


def test_cli_claim_cascade_smoke(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI parity: claiming a leaf via the CLI cascades to its open parent."""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rc = _cli.main(["claim", child, "--assignee", "alice"])
    capsys.readouterr()
    assert rc == 0
    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "in_progress"
