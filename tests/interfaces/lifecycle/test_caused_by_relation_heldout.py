"""Held-out contracts for the `caused_by` relation (ticket 0b96). WITHHELD.

These separate a real 7th-relation implementation from one that only adds the
string to the canonical set:
- `caused_by` is DIRECTIONAL (forward edge only — no reciprocal, unlike relates_to),
- `caused_by` is NON-CYCLE-INDUCING (it must be added to the non-cycle tuple in
  graph/_graph.py; otherwise a reverse edge falls into blocking-cycle semantics and
  wrongly raises) — the teeth that catch a partial implementation,
- bogus relations are still rejected,
- the additive change did not break the pre-existing six relations.
"""

from __future__ import annotations

import pytest

import rebar

pytestmark = pytest.mark.interface


def _rels(tid: str, repo: str) -> list[tuple[str, str]]:
    deps = rebar.show_ticket(tid, repo_root=repo)["deps"]
    return [(d["target_id"], d["relation"]) for d in deps]


def test_caused_by_is_directional(rebar_repo) -> None:
    repo = str(rebar_repo)
    bug = rebar.create_ticket("bug", "bug", repo_root=repo)
    culprit = rebar.create_ticket("task", "culprit", repo_root=repo)
    rebar.link(bug, culprit, "caused_by", repo_root=repo)

    # Forward edge on the source; NO reciprocal on the target.
    assert (culprit, "caused_by") in _rels(bug, repo)
    assert all(rel != "caused_by" for _, rel in _rels(culprit, repo))

    # Contrast: relates_to IS reciprocal (appears on both).
    x = rebar.create_ticket("task", "x", repo_root=repo)
    y = rebar.create_ticket("task", "y", repo_root=repo)
    rebar.link(x, y, "relates_to", repo_root=repo)
    assert (y, "relates_to") in _rels(x, repo)
    assert (x, "relates_to") in _rels(y, repo)


def test_caused_by_is_non_cycle_inducing(rebar_repo) -> None:
    # Teeth for the graph/_graph.py non-cycle-tuple fix: establish a REAL blocking
    # edge a->b, then draw the reverse caused_by edge b->a. This must NOT raise,
    # because caused_by is non-cycle-inducing. If caused_by were (wrongly) treated
    # as blocking — i.e. missing from the non-cycle tuple — the cycle check would
    # see a->b (blocks) closing with b->a and raise CyclicDependencyError.
    repo = str(rebar_repo)
    a = rebar.create_ticket("bug", "a", repo_root=repo)
    b = rebar.create_ticket("task", "b", repo_root=repo)
    rebar.link(a, b, "blocks", repo_root=repo)  # real blocking edge a -> b
    rebar.link(b, a, "caused_by", repo_root=repo)  # reverse caused_by must NOT raise
    assert (a, "caused_by") in _rels(b, repo)


def test_blocking_cycle_still_rejected(rebar_repo) -> None:
    # Control: the real blocking relations still enforce acyclicity.
    repo = str(rebar_repo)
    x = rebar.create_ticket("task", "x", repo_root=repo)
    y = rebar.create_ticket("task", "y", repo_root=repo)
    rebar.link(x, y, "blocks", repo_root=repo)
    with pytest.raises(rebar.RebarError):
        rebar.link(y, x, "blocks", repo_root=repo)  # would create a blocks cycle


def test_bogus_relation_still_rejected(rebar_repo) -> None:
    repo = str(rebar_repo)
    a = rebar.create_ticket("task", "a", repo_root=repo)
    b = rebar.create_ticket("task", "b", repo_root=repo)
    with pytest.raises(rebar.RebarError, match="invalid relation"):
        rebar.link(a, b, "not_a_real_relation", repo_root=repo)


@pytest.mark.parametrize(
    "relation",
    ["blocks", "depends_on", "relates_to", "duplicates", "supersedes", "discovered_from"],
)
def test_legacy_relations_still_work(rebar_repo, relation: str) -> None:
    # Additive change must not break the pre-existing six relations.
    repo = str(rebar_repo)
    a = rebar.create_ticket("task", "a", repo_root=repo)
    b = rebar.create_ticket("task", "b", repo_root=repo)
    rebar.link(a, b, relation, repo_root=repo)
    assert (b, relation) in _rels(a, repo)
