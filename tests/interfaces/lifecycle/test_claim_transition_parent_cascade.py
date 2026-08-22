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


def test_transition_cascade_false_suppresses_cascade(rebar_repo: Path) -> None:
    """`cascade=False` opts a caller out of the parent-first cascade — the seam used
    by per-ticket state replay (NDJSON import) so it never pre-moves an open parent."""
    from rebar._commands.transition import transition_compute

    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    transition_compute(child, "open", "in_progress", cascade=False, repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "open"  # NOT cascaded


# --------------------------------------------------------------------------- reopen


def _closed_chain(repo: Path, depth: int) -> list[str]:
    """Create a parent chain ``depth`` deep (root first) and close it bottom-up, which
    is the only order the unresolved-open-children guard permits."""
    chain: list[str] = []
    parent: str | None = None
    for i in range(depth):
        kind = "epic" if i == 0 else ("story" if i < depth - 1 else "task")
        chain.append(rebar.create_ticket(kind, f"n{i}", parent=parent, repo_root=str(repo)))
        parent = chain[-1]
    for tid in reversed(chain):
        rebar.transition(tid, "open", "closed", repo_root=str(repo))
    return chain


def test_reopen_child_reopens_closed_parent(rebar_repo: Path) -> None:
    """The bug: reopening a child left its CLOSED parent closed, so the store held a
    closed parent with a non-closed child."""
    parent, child = _closed_chain(rebar_repo, 2)

    rebar.reopen(child, repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "open"
    assert _status(parent, rebar_repo) == "open"  # cascaded


def test_reopen_cascades_through_multiple_closed_levels(rebar_repo: Path) -> None:
    grand, parent, child = _closed_chain(rebar_repo, 3)

    rebar.reopen(child, repo_root=str(rebar_repo))

    for t in (grand, parent, child):
        assert _status(t, rebar_repo) == "open", f"{t} not cascaded"


def test_reopen_does_not_disturb_already_open_parent(rebar_repo: Path) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))

    rebar.reopen(child, repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "open"
    assert _status(parent, rebar_repo) == "open"  # untouched no-op


def test_reopen_does_not_disturb_in_progress_parent(rebar_repo: Path) -> None:
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rebar.claim(parent, assignee="owner", repo_root=str(rebar_repo))
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))

    rebar.reopen(child, repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "open"
    assert _status(parent, rebar_repo) == "in_progress"  # not dragged backwards


def test_reopen_parent_failure_aborts_child_with_attributed_error(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-fast, no partial cascade: a raced parent keeps its concurrency identity
    (exit 10) at the leaf and the child is NOT reopened."""
    parent, child = _closed_chain(rebar_repo, 2)

    orig = txn.transition_core

    def fake_transition_core(tracker, ticket_id, current, target, **kw):  # type: ignore[no-untyped-def]
        if ticket_id == parent:
            raise ConcurrencyMismatch("simulated parent reopen failure")
        return orig(tracker, ticket_id, current, target, **kw)

    monkeypatch.setattr(txn, "transition_core", fake_transition_core)

    with pytest.raises(rebar.ConcurrencyError) as ei:
        rebar.reopen(child, repo_root=str(rebar_repo))

    assert ei.value.returncode == 10
    msg = str(ei.value)
    assert parent in msg, f"error must name the parent: {msg}"
    assert child in msg
    assert "parent" in msg.lower()
    assert _status(child, rebar_repo) == "closed"  # child NOT reopened


def test_cli_reopen_cascade_smoke(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI parity: `rebar reopen <child>` cascades to the closed parent too."""
    parent, child = _closed_chain(rebar_repo, 2)

    rc = _cli.main(["reopen", child])
    capsys.readouterr()

    assert rc == 0
    assert _status(child, rebar_repo) == "open"
    assert _status(parent, rebar_repo) == "open"


def test_reopen_cascade_false_suppresses_cascade(rebar_repo: Path) -> None:
    """`cascade=False` (per-ticket state replay, e.g. NDJSON import) opts out of the
    reopen cascade exactly as it does for the open -> in_progress edge."""
    from rebar._commands.transition import transition_compute

    parent, child = _closed_chain(rebar_repo, 2)

    transition_compute(child, "closed", "open", cascade=False, repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "open"
    assert _status(parent, rebar_repo) == "closed"  # NOT cascaded


# --------------------------------------------------- reactivation (closed -> in_progress)


def test_reactivate_child_reactivates_closed_parent(rebar_repo: Path) -> None:
    """The bug (paragonite-fruited-minnow): reactivating a closed child straight to
    ``in_progress`` left its CLOSED parent closed, so the store held a closed parent with
    an ``in_progress`` child — the invalid state I4a forbids, reached through the one edge
    into it that the cascade table did not cover."""
    parent, child = _closed_chain(rebar_repo, 2)

    rebar.transition(child, "closed", "in_progress", repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "in_progress"  # cascaded


def test_reactivate_cascades_through_multiple_closed_levels(rebar_repo: Path) -> None:
    grand, parent, child = _closed_chain(rebar_repo, 3)

    rebar.transition(child, "closed", "in_progress", repo_root=str(rebar_repo))

    for t in (grand, parent, child):
        assert _status(t, rebar_repo) == "in_progress", f"{t} not cascaded"


def test_reactivate_does_not_disturb_in_progress_parent(rebar_repo: Path) -> None:
    """An already-``in_progress`` parent is not eligible on this edge — only the requested
    ticket moves."""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rebar.claim(parent, assignee="owner", repo_root=str(rebar_repo))
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))

    rebar.transition(child, "closed", "in_progress", repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "in_progress"
    assert _assignee(parent, rebar_repo) == "owner"  # untouched


def test_reactivate_does_not_disturb_open_parent(rebar_repo: Path) -> None:
    """Only a ``closed`` parent is eligible on the ``closed -> in_progress`` edge, so an
    ``open`` parent is left alone (see the residual noted on the ticket)."""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))

    rebar.transition(child, "closed", "in_progress", repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "open"  # not eligible on this edge


def test_reactivate_parent_failure_aborts_child_with_attributed_error(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-fast, no partial cascade: a raced parent keeps its concurrency identity
    (exit 10) at the leaf and the child is NOT reactivated."""
    parent, child = _closed_chain(rebar_repo, 2)

    orig = txn.transition_core

    def fake_transition_core(tracker, ticket_id, current, target, **kw):  # type: ignore[no-untyped-def]
        if ticket_id == parent:
            raise ConcurrencyMismatch("simulated parent reactivation failure")
        return orig(tracker, ticket_id, current, target, **kw)

    monkeypatch.setattr(txn, "transition_core", fake_transition_core)

    with pytest.raises(rebar.ConcurrencyError) as ei:
        rebar.transition(child, "closed", "in_progress", repo_root=str(rebar_repo))

    assert ei.value.returncode == 10
    msg = str(ei.value)
    assert parent in msg, f"error must name the parent: {msg}"
    assert child in msg
    assert "parent" in msg.lower()
    assert _status(child, rebar_repo) == "closed"  # child NOT reactivated


def test_reactivate_wrong_current_status_still_rejected(rebar_repo: Path) -> None:
    """The new edge does not loosen optimistic concurrency: naming a current status the
    ticket is not in is still exit 10, and nothing (child or parent) moves."""
    parent, child = _closed_chain(rebar_repo, 2)

    with pytest.raises(rebar.ConcurrencyError) as ei:
        rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))

    assert ei.value.returncode == 10
    assert _status(child, rebar_repo) == "closed"
    assert _status(parent, rebar_repo) == "closed"


def test_cli_reactivate_cascade_smoke(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI parity: `rebar transition <child> closed in_progress` cascades too."""
    parent, child = _closed_chain(rebar_repo, 2)

    rc = _cli.main(["transition", child, "closed", "in_progress"])
    capsys.readouterr()

    assert rc == 0
    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "in_progress"


def test_reactivate_cascade_false_suppresses_cascade(rebar_repo: Path) -> None:
    """``cascade=False`` (per-ticket state replay, e.g. NDJSON import) opts out of the
    reactivation cascade exactly as it does for the other two edges."""
    from rebar._commands.transition import transition_compute

    parent, child = _closed_chain(rebar_repo, 2)

    transition_compute(child, "closed", "in_progress", cascade=False, repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "closed"  # NOT cascaded


# ------------------------------------------------- cascade TOCTOU benign-race parity


def test_transition_benign_parent_race_still_moves_child(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity with `claim`'s cascade: the cascade decision reads the parent's status
    WITHOUT the write lock, so a peer can move the parent off the eligible status in
    between. When the parent op then fails but the parent has ALREADY left that status,
    the cascade's purpose is met — that is benign and the child still moves."""
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))

    orig = txn.transition_core

    def racing_transition_core(tracker, ticket_id, current, target, **kw):  # type: ignore[no-untyped-def]
        if ticket_id == parent:
            # Simulate the peer that won the race: the parent leaves `open` under the
            # lock, and OUR parent transition is rejected.
            orig(tracker, ticket_id, "open", "blocked", **kw)
            raise ConcurrencyMismatch("parent progressed concurrently")
        return orig(tracker, ticket_id, current, target, **kw)

    monkeypatch.setattr(txn, "transition_core", racing_transition_core)

    rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "in_progress"  # benign — child still moved
    assert _status(parent, rebar_repo) == "blocked"  # the peer's write stands


def test_reopen_benign_parent_race_still_reopens_child(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same benign-race rule on the reopen edge: a parent already dragged off
    `closed` by a peer is benign, so the child is still reopened."""
    parent, child = _closed_chain(rebar_repo, 2)

    orig = txn.transition_core

    def racing_transition_core(tracker, ticket_id, current, target, **kw):  # type: ignore[no-untyped-def]
        if ticket_id == parent:
            orig(tracker, ticket_id, "closed", "open", **kw)
            raise ConcurrencyMismatch("parent reopened concurrently")
        return orig(tracker, ticket_id, current, target, **kw)

    monkeypatch.setattr(txn, "transition_core", racing_transition_core)

    rebar.reopen(child, repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == "open"  # benign — child still reopened
    assert _status(parent, rebar_repo) == "open"


# ------------------------------------------------------- table-driven edge coverage
def _cascading_edges() -> dict[tuple[str, str], str]:
    from rebar._commands.transition import _CASCADING_EDGES

    return dict(_CASCADING_EDGES)


def _chain_in_status(repo: Path, status: str) -> tuple[str, str]:
    """A (parent, child) pair both sitting in ``status``, built the only way each status is
    legally reachable. A row whose source status is neither ``open`` nor ``closed`` fails
    LOUDLY here rather than being skipped: a silently-unbuildable fixture would turn this
    whole guard into a no-op for exactly the new row it exists to cover."""
    if status == "open":
        parent = rebar.create_ticket("epic", "parent", repo_root=str(repo))
        child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(repo))
        return parent, child
    if status == "closed":
        parent, child = _closed_chain(repo, 2)
        return parent, child
    raise AssertionError(
        f"_CASCADING_EDGES grew a row whose source status is {status!r}; extend "
        "_chain_in_status so the new edge is actually covered rather than skipped"
    )


@pytest.mark.parametrize(("edge", "eligible"), sorted(_cascading_edges().items()))
def test_every_table_edge_cascades_through_the_real_transition(
    edge: tuple[str, str], eligible: str, rebar_repo: Path
) -> None:
    """Coverage that GROWS WITH THE TABLE, exercised through the REAL surface.

    ``test_the_cascading_edge_table_is_unchanged`` (unit tier) is only a tripwire: adding a
    fourth row breaks its equality assertion and makes the author look, but updating that
    literal is all it forces — nothing then obliges the new row to actually cascade. The two
    bugs this story descends from, ``cranial-sulfur-peafowl`` and ``paragonite-fruited-minnow``,
    were both "an edge the cascade did not know about", so a tripwire alone leaves the bug
    class open.

    Parametrising over the LIVE table closes it: a new row mints a new case on import, and
    that case only passes if ``rebar.transition`` genuinely walks the parent first. It is
    driven through the library surface rather than through ``cascade_parent_first`` directly,
    because the injected-walk unit tests pass for ANY row by construction — the walk is
    table-agnostic. Only the real path can tell you the row is WIRED.
    """
    from_status, to_status = edge
    parent, child = _chain_in_status(rebar_repo, from_status)
    assert _status(parent, rebar_repo) == eligible

    rebar.transition(child, from_status, to_status, repo_root=str(rebar_repo))

    assert _status(child, rebar_repo) == to_status
    assert _status(parent, rebar_repo) == to_status, (
        f"edge {from_status} -> {to_status} did not cascade to its {eligible} parent"
    )
