"""One parent-first cascade shared by claim, transition and the inbound writer (story 4329).

Three call sites walk a ticket's ancestors pulling an eligible parent along the same lifecycle
edge, and each grew its own copy of the walk:

* ``transition._cascade_parent_first`` — the reference implementation, driven by the
  ``_CASCADING_EDGES`` table (``open -> in_progress``, ``closed -> open``,
  ``closed -> in_progress``), recursing into ``transition_compute``;
* ``claim._claim_compute`` — its own walk, hardcoded to the ``open`` edge, recursing into
  ``claim_compute``;
* ``apply_inbound_events._cascade_inbound_status_parents`` (Gerrit 2044) — an iterative walk
  that already single-sources the DECISION but writes through the reconciler's own primitive.

What actually differs between them is the WRITE PRIMITIVE, not the walk. These tests pin the
extracted walk's contract directly, so the three sites can share it without any of them losing
the benign-race re-check or the error attribution that make it safe.
"""

from __future__ import annotations

import pytest

from rebar._commands._seam import CommandError
from rebar._commands.txn import ConcurrencyMismatch


class _Chain:
    """A parent chain with a scripted status map and a recording advance()."""

    def __init__(self, parents: dict[str, str | None], statuses: dict[str, str]) -> None:
        self.parents = parents
        self.statuses = statuses
        self.advanced: list[str] = []
        self.fail_on: dict[str, Exception] = {}
        self.on_advance = None

    def resolve(self, ticket_id: str, eligible: str) -> str | None:
        parent = self.parents.get(ticket_id)
        if parent is None or self.statuses.get(parent) != eligible:
            return None
        return parent

    def advance(self, parent_id: str, seen) -> None:
        if parent_id in self.fail_on:
            raise self.fail_on[parent_id]
        self.advanced.append(parent_id)
        self.statuses[parent_id] = "in_progress"
        if self.on_advance is not None:
            self.on_advance(parent_id)


# ======================================================================================
# HAPPY PATH
# ======================================================================================
def test_an_eligible_parent_is_advanced_before_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: the parent moves FIRST, so a child is never left ahead of it."""
    from rebar._commands import lifecycle_cascade

    chain = _Chain({"child": "parent"}, {"parent": "open", "child": "open"})
    lifecycle_cascade.cascade_parent_first(
        "child",
        eligible_status="open",
        resolve_parent=chain.resolve,
        advance=chain.advance,
        action="claim child",
        parent_action="claimed",
    )
    assert chain.advanced == ["parent"]


def test_an_ineligible_or_absent_parent_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent already past the eligible status — or no parent at all — is untouched."""
    from rebar._commands import lifecycle_cascade

    busy = _Chain({"child": "parent"}, {"parent": "in_progress", "child": "open"})
    lifecycle_cascade.cascade_parent_first(
        "child",
        eligible_status="open",
        resolve_parent=busy.resolve,
        advance=busy.advance,
        action="claim child",
        parent_action="claimed",
    )
    assert busy.advanced == []

    orphan = _Chain({"child": None}, {"child": "open"})
    lifecycle_cascade.cascade_parent_first(
        "child",
        eligible_status="open",
        resolve_parent=orphan.resolve,
        advance=orphan.advance,
        action="claim child",
        parent_action="claimed",
    )
    assert orphan.advanced == []


# ======================================================================================
# HELD OUT
# ======================================================================================
def test_a_multi_level_chain_cascades_all_the_way_up() -> None:
    """Grandparent included: the walk recurses, it does not stop at one level."""
    from rebar._commands import lifecycle_cascade

    chain = _Chain(
        {"child": "parent", "parent": "grandparent"},
        {"grandparent": "open", "parent": "open", "child": "open"},
    )
    chain.on_advance = lambda pid: lifecycle_cascade.cascade_parent_first(
        pid,
        eligible_status="open",
        resolve_parent=chain.resolve,
        advance=chain.advance,
        action="claim child",
        parent_action="claimed",
    )
    lifecycle_cascade.cascade_parent_first(
        "child",
        eligible_status="open",
        resolve_parent=chain.resolve,
        advance=chain.advance,
        action="claim child",
        parent_action="claimed",
    )
    assert set(chain.advanced) == {"parent", "grandparent"}


def test_a_malformed_parent_cycle_terminates() -> None:
    """A ticket that is (transitively) its own parent must not spin forever."""
    from rebar._commands import lifecycle_cascade

    chain = _Chain({"a": "b", "b": "a"}, {"a": "open", "b": "open"})
    lifecycle_cascade.cascade_parent_first(
        "a",
        eligible_status="open",
        resolve_parent=chain.resolve,
        advance=chain.advance,
        action="claim child",
        parent_action="claimed",
        cascade_seen=frozenset({"b"}),
    )
    assert chain.advanced == []


def test_a_benign_parent_race_still_lets_the_child_proceed() -> None:
    """TOCTOU: the decision read the parent WITHOUT the write lock. If a peer moved it off
    the eligible status in between, the cascade's purpose is already satisfied — the child
    must proceed rather than fail. Losing this re-check turns ordinary concurrency into
    spurious failures, so it is pinned separately from the failure path below."""
    from rebar._commands import lifecycle_cascade

    chain = _Chain({"child": "parent"}, {"parent": "open", "child": "open"})
    chain.fail_on["parent"] = CommandError("parent claim rejected", returncode=1)

    def racing_resolve(ticket_id: str, eligible: str) -> str | None:
        got = chain.resolve(ticket_id, eligible)
        chain.statuses["parent"] = "in_progress"  # a peer moves it after the first read
        return got

    lifecycle_cascade.cascade_parent_first(
        "child",
        eligible_status="open",
        resolve_parent=racing_resolve,
        advance=chain.advance,
        action="claim child",
        parent_action="claimed",
    )  # must NOT raise


def test_a_genuine_parent_failure_aborts_the_child_and_names_the_parent() -> None:
    """A parent still in the eligible status after the failure is a REAL failure (its own
    gate blocked it), so the child must not move and the error must say which parent."""
    from rebar._commands import lifecycle_cascade

    chain = _Chain({"child": "parent"}, {"parent": "open", "child": "open"})
    chain.fail_on["parent"] = CommandError("gate refused", returncode=3)

    with pytest.raises(CommandError) as excinfo:
        lifecycle_cascade.cascade_parent_first(
            "child",
            eligible_status="open",
            resolve_parent=chain.resolve,
            advance=chain.advance,
            action="claim child",
            parent_action="claimed",
        )
    assert "parent" in str(excinfo.value)
    assert "gate refused" in str(excinfo.value)
    assert excinfo.value.returncode == 3, "the parent's exit code must survive"


def test_a_raced_parent_keeps_its_concurrency_identity() -> None:
    """A ConcurrencyMismatch at the parent must surface as ConcurrencyMismatch (exit 10) at
    the leaf, so the caller's "someone else holds it, pick another" retry path still fires."""
    from rebar._commands import lifecycle_cascade

    chain = _Chain({"child": "parent"}, {"parent": "open", "child": "open"})
    chain.fail_on["parent"] = ConcurrencyMismatch("parent moved")

    with pytest.raises(ConcurrencyMismatch) as excinfo:
        lifecycle_cascade.cascade_parent_first(
            "child",
            eligible_status="open",
            resolve_parent=chain.resolve,
            advance=chain.advance,
            action="claim child",
            parent_action="claimed",
        )
    assert excinfo.value.returncode == 10


def test_the_cascading_edge_table_is_unchanged() -> None:
    """The DECISION is not what this story consolidates — the three edges and their eligible
    parent statuses must survive the extraction verbatim. A `* -> closed` edge in particular
    must stay absent, or closing a child would start closing its parent."""
    from rebar._commands.transition import _CASCADING_EDGES

    assert _CASCADING_EDGES == {
        ("open", "in_progress"): "open",
        ("closed", "open"): "closed",
        ("closed", "in_progress"): "closed",
    }
    assert ("in_progress", "closed") not in _CASCADING_EDGES


# ======================================================================================
# TABLE-DRIVEN COVERAGE (parent epic airborne-wellloved-kingfisher, story AC2)
# ======================================================================================
def _cascading_edges() -> dict[tuple[str, str], str]:
    from rebar._commands.transition import _CASCADING_EDGES

    return dict(_CASCADING_EDGES)


@pytest.mark.parametrize(("edge", "eligible"), sorted(_cascading_edges().items()))
def test_every_edge_in_the_table_cascades_its_parent_first(
    edge: tuple[str, str], eligible: str
) -> None:
    """Every row's CONTRACT for the shared walk, enumerated from the live table.

    Scope note, so this is not mistaken for more than it is: the walk is table-agnostic, so
    this passes for ANY row by construction. It pins that each row's eligible-status contract
    is coherent — not that the row is wired into ``transition``. The test that a new row is
    actually WIRED has to go through the real surface, and lives in
    ``tests/interfaces/lifecycle/test_claim_transition_parent_cascade.py::
    test_every_table_edge_cascades_through_the_real_transition``. Claiming durability here
    would be a tautology dressed as a guard.
    """
    from rebar._commands import lifecycle_cascade

    from_status, to_status = edge
    chain = _Chain({"child": "parent"}, {"parent": eligible, "child": from_status})
    lifecycle_cascade.cascade_parent_first(
        "child",
        eligible_status=eligible,
        resolve_parent=chain.resolve,
        advance=chain.advance,
        action=f"move child to {to_status}",
        parent_action="advanced",
    )
    assert chain.advanced == ["parent"], (
        f"edge {from_status} -> {to_status} (eligible parent status {eligible!r}) did not "
        "cascade to its parent"
    )


@pytest.mark.parametrize(("edge", "eligible"), sorted(_cascading_edges().items()))
def test_no_edge_disturbs_a_parent_in_the_wrong_status(
    edge: tuple[str, str], eligible: str
) -> None:
    """The mirror of the rule above, parametrised the same way: cascading is conditional on the
    parent being in the row's eligible status. A row that advanced ANY parent would quietly
    move tickets nobody asked it to touch — worse than not cascading at all."""
    from rebar._commands import lifecycle_cascade

    from_status, to_status = edge
    wrong = "in_progress" if eligible != "in_progress" else "open"
    chain = _Chain({"child": "parent"}, {"parent": wrong, "child": from_status})
    lifecycle_cascade.cascade_parent_first(
        "child",
        eligible_status=eligible,
        resolve_parent=chain.resolve,
        advance=chain.advance,
        action=f"move child to {to_status}",
        parent_action="advanced",
    )
    assert chain.advanced == []


def test_claim_cascades_only_the_open_edge_and_says_so() -> None:
    """The story's AC2 as first written — "every edge exercised for both `claim` and
    `transition`" — is not satisfiable, and pinning WHY here is more useful than an AC nobody
    can meet. `claim` moves a ticket `open -> in_progress` and nothing else, so it can only
    ever walk the `(open, in_progress)` row; the two `closed`-source rows belong to
    `transition`/`reopen` alone.

    This asserts that narrowness deliberately. If `claim` ever grows a second edge, this test
    fails and the surface-parity question gets asked on purpose rather than by accident.
    """
    edges = _cascading_edges()
    assert edges[("open", "in_progress")] == "open"
    claimable = {edge for edge in edges if edge[0] == "open"}
    assert claimable == {("open", "in_progress")}
