"""Single source of truth for Jira issue-link <-> rebar relation DIRECTION (bug 4b59).

The inbound ADD path (``inbound_differ._diff_links_inbound``) and the REMOVE path
(``outbound_links._diff_link_removals``) must disambiguate a Jira ``Blocks`` link by
direction the SAME way. Every prior link fix (3f04/c8ed/3b86) corrected one seam and
left its mirror inverted, "proven" by a round-trip oracle that is invariant under a
double inversion — so this logic lives in ONE place and is pinned to CAPTURED
live-Jira ground truth by ``tests/.../diffing/test_link_direction_absolute.py``.

Stdlib-only so it loads cleanly under both the package import (production) and the
``spec_from_file_location`` standalone load (reconciler unit tests).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Jira link-type name -> base rebar relation (the OUTWARD-side meaning).
JIRA_LINK_TO_RELATION: dict[str, str] = {"Blocks": "blocks", "Relates": "relates_to"}

# blocks<->depends_on are the two faces of one blocking edge; symmetric relations
# (relates_to) invert to themselves via ``.get(rel, rel)``.
INVERSE_RELATION: dict[str, str] = {"blocks": "depends_on", "depends_on": "blocks"}


def resolve_inbound_link(link: dict[str, Any]) -> tuple[str | None, str | None]:
    """Map ONE Jira issuelink (from local issue X's perspective) to ``(other_key, relation)``.

    LIVE-JIRA ground truth (captured 2026-07-17 from the REB project):
      * ``outwardIssue: Y`` + ``Blocks`` == X blocks Y            -> ``blocks``
      * ``inwardIssue:  Y`` + ``Blocks`` == X is blocked by Y     -> ``depends_on`` (inverse)
      * ``Relates`` is symmetric                                  -> ``relates_to`` either side

    Returns ``(None, None)`` for unmapped link types or malformed entries.
    """
    link_type = link.get("type") or {}
    type_name = link_type.get("name") if isinstance(link_type, dict) else None
    base = JIRA_LINK_TO_RELATION.get(type_name) if type_name else None
    if base is None:
        return None, None
    outward = link.get("outwardIssue")
    inward = link.get("inwardIssue")
    if isinstance(outward, dict) and outward.get("key"):
        return outward["key"], base  # X --outward "blocks"--> Y == X blocks Y
    if isinstance(inward, dict) and inward.get("key"):
        return inward["key"], INVERSE_RELATION.get(base, base)  # X is blocked by Y
    return None, None


def observed_peer_deps(
    issuelinks: Any, get_local_id: Callable[[str], str | None]
) -> set[tuple[str, str]]:
    """The peer's link set as ``{(relation, local_target_id)}``, in LOCAL relation vocabulary.

    The counterpart of :func:`deps_as_set` for the REMOVE paths (ticket 2b16): it renders what
    the peer currently carries in the same ``(relation, target)`` shape the local deps use, so
    the two sets can be compared directly. It belongs here beside
    :func:`resolve_inbound_link` because rendering it is exactly the direction problem this
    module exists to own once — comparing raw peer keys, or the un-inverted base relation,
    makes an ``inwardIssue``/``Blocks`` entry read as ``blocks`` against a ``depends_on`` dep,
    so a link that is STILL on the peer reads as deleted.

    Entries with an unmapped link type, a malformed shape, or an unbound counterpart are
    omitted — an unbound counterpart has no local id to compare a dep against.
    """
    out: set[tuple[str, str]] = set()
    for link in issuelinks or []:
        if not isinstance(link, dict):
            continue
        other_key, relation = resolve_inbound_link(link)
        if other_key is None or relation is None:
            continue
        target_id = get_local_id(other_key)
        if target_id:
            out.add((relation, target_id))
    return out


def deps_as_set(ticket: dict[str, Any] | None) -> set[tuple[str, str]]:
    """A ticket's link deps as a ``{(relation, target_id)}`` set (empty if absent)."""
    out: set[tuple[str, str]] = set()
    for dep in (ticket or {}).get("deps") or []:
        if isinstance(dep, dict):
            rel = dep.get("relation")
            tgt = dep.get("target_id")
            if rel and tgt:
                out.add((rel, tgt))
    return out
