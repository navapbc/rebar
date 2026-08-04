"""Relation-scoped unlink — a pair holding TWO relations removes exactly one (bug e39f).

Tier: interface (library/CLI over a real temp store). Links are WRITTEN keyed on
``(target_id, relation)`` (``graph/_links.add_dependency`` is idempotent per that key), so
one ordered pair CAN hold two differently-related net-active deps — but removal used to be
pair-scoped only (``rebar.unlink(id1, id2)``: "removes the most-recent link between the
pair"). These cells pin the ratified contract (ticket e39f-5055-f5af-424a):

* the graph layer exposes a relation-scoped removal seam symmetric with ``add_dependency``;
* ``rebar.unlink`` / ``rebar unlink`` accept an OPTIONAL relation that removes exactly the
  named relation's link, leaving the pair's other relation net-active;
* with NO relation, the existing pair-scoped most-recent behavior is byte-for-byte unchanged.

Oracles read observable store state (``rebar.show_ticket``'s ``deps``), never internals.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import rebar

pytestmark = pytest.mark.interface


def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args], capture_output=True, text=True, cwd=cwd
    )


def _relations(source: str, target: str, repo: str) -> set[str]:
    deps = rebar.show_ticket(source, repo_root=repo).get("deps") or []
    return {d["relation"] for d in deps if d.get("target_id") == target}


@pytest.fixture
def double_related_pair(rebar_repo) -> tuple[str, str, str]:
    """A pair holding TWO net-active relations: a blocks b AND a relates_to b."""
    repo = str(rebar_repo)
    a = str(rebar.create_ticket("task", "e39f source", repo_root=repo))
    b = str(rebar.create_ticket("task", "e39f target", repo_root=repo))
    rebar.link(a, b, "blocks", repo_root=repo)
    rebar.link(a, b, "relates_to", repo_root=repo)
    return repo, a, b


def test_a_pair_can_hold_two_differently_related_net_active_deps(
    double_related_pair: tuple[str, str, str],
) -> None:
    """AC1: the write side is relation-keyed, so two relations coexist on one pair.

    This is the premise that makes G5 live (ticket e39f): if this cell ever fails,
    the whole relation-scoped removal surface is dead code and e39f reopens.
    """
    repo, a, b = double_related_pair
    assert _relations(a, b, repo) == {"blocks", "relates_to"}, (
        "a pair could not hold blocks + relates_to simultaneously — the (target, relation) "
        "idempotency key in add_dependency no longer admits two relations per pair"
    )


def test_relation_scoped_unlink_removes_only_the_named_relation(
    double_related_pair: tuple[str, str, str],
) -> None:
    """Removing `blocks` by name leaves `relates_to` net-active (the e39f contract)."""
    repo, a, b = double_related_pair

    rebar.unlink(a, b, "blocks", repo_root=repo)

    remaining = _relations(a, b, repo)
    assert "blocks" not in remaining, "the named relation (blocks) was not removed"
    assert "relates_to" in remaining, (
        "the OTHER relation (relates_to) was removed too — relation-scoped removal must "
        f"touch exactly the named relation; remaining={sorted(remaining)}"
    )


def test_relation_scoped_unlink_of_relates_to_removes_the_reciprocal(
    double_related_pair: tuple[str, str, str],
) -> None:
    """relates_to is bidirectional on write; a relation-scoped removal mirrors that."""
    repo, a, b = double_related_pair
    assert "relates_to" in _relations(b, a, repo), (
        "SETUP FAILED: the reciprocal relates_to link was never written"
    )

    rebar.unlink(a, b, "relates_to", repo_root=repo)

    assert _relations(a, b, repo) == {"blocks"}, "the named relates_to link must be gone"
    assert "relates_to" not in _relations(b, a, repo), (
        "the reciprocal relates_to link on the target survived a relation-scoped unlink"
    )


def test_unlink_without_relation_keeps_most_recent_pair_scoped_behavior(
    double_related_pair: tuple[str, str, str],
) -> None:
    """Backward compat: NO relation arg still removes the most-recent link of the pair."""
    repo, a, b = double_related_pair

    rebar.unlink(a, b, repo_root=repo)

    assert _relations(a, b, repo) == {"blocks"}, (
        "pair-scoped unlink must remove the MOST-RECENT link (relates_to, linked second) "
        "and leave the older blocks link — the pre-e39f behavior is contractual"
    )


def test_relation_scoped_unlink_with_no_matching_link_removes_nothing(
    double_related_pair: tuple[str, str, str],
) -> None:
    """A relation with no net-active link errors and leaves BOTH existing links intact."""
    repo, a, b = double_related_pair

    with pytest.raises(rebar.RebarError):
        rebar.unlink(a, b, "depends_on", repo_root=repo)

    assert _relations(a, b, repo) == {"blocks", "relates_to"}, (
        "a removal naming a relation the pair does not hold must remove NOTHING"
    )


def test_relation_scoped_unlink_rejects_an_unknown_relation(
    double_related_pair: tuple[str, str, str],
) -> None:
    repo, a, b = double_related_pair

    with pytest.raises(rebar.RebarError):
        rebar.unlink(a, b, "not_a_relation", repo_root=repo)

    assert _relations(a, b, repo) == {"blocks", "relates_to"}


def test_cli_unlink_accepts_an_optional_relation(
    double_related_pair: tuple[str, str, str],
) -> None:
    """`rebar unlink <a> <b> blocks` removes exactly the blocks link (exit 0)."""
    repo, a, b = double_related_pair

    p = _cli("unlink", a, b, "blocks", cwd=repo)

    assert p.returncode == 0, f"stderr={p.stderr!r}"
    remaining = _relations(a, b, repo)
    assert remaining == {"relates_to"}, (
        f"CLI relation-scoped unlink removed the wrong link(s); remaining={sorted(remaining)}"
    )


def test_cli_unlink_without_relation_is_unchanged(
    double_related_pair: tuple[str, str, str],
) -> None:
    """`rebar unlink <a> <b>` (no relation) still removes the most-recent link."""
    repo, a, b = double_related_pair

    p = _cli("unlink", a, b, cwd=repo)

    assert p.returncode == 0, f"stderr={p.stderr!r}"
    assert _relations(a, b, repo) == {"blocks"}


def test_graph_seam_remove_dependency_is_relation_scoped(
    double_related_pair: tuple[str, str, str],
) -> None:
    """The graph-layer seam removes exactly (target_id, relation) — add_dependency's mirror."""
    from rebar._commands._seam import tracker_dir
    from rebar.graph import remove_dependency

    repo, a, b = double_related_pair
    tracker = str(tracker_dir(repo))

    remove_dependency(a, b, tracker, "blocks")

    assert _relations(a, b, repo) == {"relates_to"}, (
        "graph.remove_dependency must remove exactly the named (target, relation) link"
    )


def test_graph_seam_remove_dependency_rejects_an_unknown_relation(
    double_related_pair: tuple[str, str, str],
) -> None:
    from rebar._commands._seam import tracker_dir
    from rebar.graph import remove_dependency

    repo, a, b = double_related_pair

    with pytest.raises(ValueError):
        remove_dependency(a, b, str(tracker_dir(repo)), "not_a_relation")

    assert _relations(a, b, repo) == {"blocks", "relates_to"}
