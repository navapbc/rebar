"""Citation edge-verify must honor INHERITED (ancestor) hierarchy links.

Design (client report §7, per maintainer): epics depend on epics and stories on stories, so a
parent's dependency is inherited by its children. If Epic A has Child B and B cites Epic C, the
citation is grounded as long as A (B's ancestor) has the verified upstream edge to C — even with
no direct B->C edge. The direct-edge behavior is unchanged; the inherited path is purely
additive, and a citation with neither a direct nor an inherited edge stays unbacked.
"""

from __future__ import annotations

from rebar.llm.plan_review import det_citation

B = "bbbb-bbbb-bbbb-bbbb"  # child (the plan ticket citing C)
A = "aaaa-aaaa-aaaa-aaaa"  # B's parent epic
C = "cccc-cccc-cccc-cccc"  # cited prerequisite epic
CITES = [("relied-upon symbol", C)]


def _resolvers(deps, parents):
    return (lambda cid: list(deps.get(cid, []))), (lambda cid: parents.get(cid))


def test_inherited_parent_depends_on_grounds_child_citation() -> None:
    # A depends_on C; B (parent=A) cites C with no direct B->C edge.
    deps = {A: [{"relation": "depends_on", "target_id": C}], C: []}
    resolve_deps, resolve_parent = _resolvers(deps, {B: A, A: None})
    assert (
        det_citation.unbacked_citations(CITES, [], resolve_deps, B, resolve_parent=resolve_parent)
        == []
    )


def test_inherited_reverse_block_grounds_child_citation() -> None:
    # C blocks A; B (parent=A) cites C.
    deps = {A: [], C: [{"relation": "blocks", "target_id": A}]}
    resolve_deps, resolve_parent = _resolvers(deps, {B: A, A: None})
    assert (
        det_citation.unbacked_citations(CITES, [], resolve_deps, B, resolve_parent=resolve_parent)
        == []
    )


def test_neither_direct_nor_inherited_stays_unbacked() -> None:
    # No edge anywhere along B's ancestry to C -> still flagged (no over-grounding).
    deps = {A: [], C: []}
    resolve_deps, resolve_parent = _resolvers(deps, {B: A, A: None})
    issues = det_citation.unbacked_citations(
        CITES, [], resolve_deps, B, resolve_parent=resolve_parent
    )
    assert len(issues) == 1


def test_direct_edge_still_grounds_when_parent_resolver_present() -> None:
    # Existing direct behavior is preserved even with the ancestor walk available.
    own = [{"relation": "depends_on", "target_id": C}]
    resolve_deps, resolve_parent = _resolvers({C: []}, {B: A, A: None})
    assert (
        det_citation.unbacked_citations(CITES, own, resolve_deps, B, resolve_parent=resolve_parent)
        == []
    )


def test_parent_cycle_is_bounded_and_fails_closed() -> None:
    # A pathological parent cycle must not hang and must not over-ground.
    deps = {A: [], C: []}
    resolve_deps, resolve_parent = _resolvers(deps, {B: A, A: B})
    issues = det_citation.unbacked_citations(
        CITES, [], resolve_deps, B, resolve_parent=resolve_parent
    )
    assert len(issues) == 1
