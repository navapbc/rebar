"""Cross-ticket citation edge-verify advisory DET lint (story 266e; ADR-0016).

Plan authors can cite a code element a prerequisite ticket will create with a
machine-parseable inline token ``<subject> [rebar:<ticket-id>]`` — the ``<subject>``
names the relied-upon file/module/class/function/config-key, and the trailing
``[rebar:<ticket-id>]`` is the token this module parses. A citation is honored only
when it names a VERIFIED direct-upstream prerequisite of the plan ticket P.

Like :mod:`.det_operator_attested`, this is a pure-stdlib leaf (``re`` only) that
imports nothing from :mod:`.det_floor`; it is surfaced ADVISORILY through
``det_floor.p6_ac_quality`` (which never blocks). Blocking coverage grounding stays
where it already lives — the LLM Pass-1 finders (G1G2/E4/E6, Layer 2). This layer
only coaches: an edge-unbacked or uncited citation earns no deterministic credit.

"Upstream" has TWO encodings and BOTH verify (D2 review-confirmed dep semantics —
a dep entry ``{relation, target_id}`` is read from the OWNING ticket's outgoing
perspective):
  (a) P declares the dependency: P's own deps contain ``{depends_on -> C}``; OR
  (b) C declares the block: C's own deps contain ``{blocks -> P}`` (a bounded
      reverse lookup — C blocks P ⇒ C is upstream of P).
A ``{blocks -> X}`` entry in P's OWN deps points DOWNSTREAM (P blocks X) and does
NOT verify. The reverse lookup uses an INJECTED ``resolve_deps(ticket_id)`` callable
(det_floor supplies one backed by :func:`rebar._reads.show_ticket`) so the edge-check
is unit-testable with a fake resolver. FAIL-CLOSED: any exception from
``resolve_deps`` is caught and the citation is treated UNVERIFIED (advisory finding
stands) — no exception propagates into the advisory lane.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

# Marker for the emitted signal, mirroring the reader-module shape (det_operator_attested
# family). Records when the citation-grounding lint began accruing coverage.
_ACCRUING_SINCE = "story 266e (plan-review citation grounding)"

# The machine-parseable citation token ``[rebar:<ticket-id>]``. ``<ticket-id>`` matches
# rebar's id/alias grammar (:mod:`rebar._ids`): a full/short lowercase-hex id
# (``[a-z0-9]{4}-...``) OR an adjective-noun-noun alias (lowercase words in hyphen
# groups). Both reduce to lowercase-alnum segments joined by ``-``.
_CITATION_RE = re.compile(r"\[rebar:([a-z0-9]+(?:-[a-z0-9]+)*)\]")


def parse_citations(plan_text: str) -> list[tuple[str, str]]:
    """Extract each ``[rebar:<id>]`` token and its preceding subject text.

    Returns ``(subject, ticket_id)`` pairs in document order. ``subject`` is the
    trimmed free text immediately preceding the token on its line (from the line
    start or the end of a prior citation on the same line), i.e. the human/LLM-readable
    referent the plan relies upon. Side-effect-free, LLM-free."""
    out: list[tuple[str, str]] = []
    prev_end = 0
    prev_line_start = -1
    for m in _CITATION_RE.finditer(plan_text):
        ticket_id = m.group(1)
        line_start = plan_text.rfind("\n", 0, m.start()) + 1
        # If a prior citation shares this line, the subject starts after it.
        start = line_start if line_start != prev_line_start else max(line_start, prev_end)
        subject = plan_text[start : m.start()].strip()
        out.append((subject, ticket_id))
        prev_end = m.end()
        prev_line_start = line_start
    return out


# A bound on the parent-chain walk for inherited-link resolution — deep enough for any real
# epic/story/task nesting, finite so a malformed cyclic ``parent_id`` chain can never spin.
_MAX_ANCESTOR_WALK = 32


def _declares_depends_on(deps: list[dict[str, Any]], cited_id: str) -> bool:
    return any(
        d.get("relation") == "depends_on" and d.get("target_id") == cited_id for d in deps or []
    )


def _declares_blocks(deps: list[dict[str, Any]], target_id: str) -> bool:
    return any(
        d.get("relation") == "blocks" and d.get("target_id") == target_id for d in deps or []
    )


def _inherited_edge_verified(
    cited_id: str,
    cited_deps: list[dict[str, Any]],
    resolve_deps: Callable[[str], list[dict[str, Any]]],
    resolve_parent: Callable[[str], str | None],
    plan_ticket_id: str,
) -> bool:
    """True iff an ANCESTOR of ``plan_ticket_id`` carries the verified upstream edge to
    ``cited_id`` — ``ancestor.depends_on(C)`` or ``C.blocks(ancestor)``. Epics depend on epics
    and stories on stories, so a parent's dependency is inherited by its children (a child's
    citation to the parent's prerequisite is grounded). Bounded + cycle-guarded; FAIL-CLOSED on
    any resolver exception."""
    seen = {plan_ticket_id}
    current = plan_ticket_id
    for _ in range(_MAX_ANCESTOR_WALK):
        try:
            parent = resolve_parent(current)
        except Exception:  # noqa: BLE001 — fail-closed: any resolve error ⇒ unverified
            return False
        if not parent or parent in seen:
            return False
        seen.add(parent)
        # (a') an ancestor declares depends_on -> C
        try:
            anc_deps = resolve_deps(parent) or []
        except Exception:  # noqa: BLE001 — fail-closed
            return False
        if _declares_depends_on(anc_deps, cited_id):
            return True
        # (b') C declares blocks -> ancestor (reuse the already-fetched cited deps)
        if _declares_blocks(cited_deps, parent):
            return True
        current = parent
    return False


def _edge_verified(
    cited_id: str,
    own_deps: list[dict[str, Any]],
    resolve_deps: Callable[[str], list[dict[str, Any]]],
    plan_ticket_id: str,
    resolve_parent: Callable[[str], str | None] | None = None,
) -> bool:
    """True iff ``cited_id`` is a VERIFIED upstream prerequisite of ``plan_ticket_id`` under
    encoding (a) ``P.depends_on(C)``, (b) ``C.blocks(P)``, or — when ``resolve_parent`` is
    supplied — the INHERITED form where an ancestor of P carries either edge to C. FAIL-CLOSED:
    any exception from the injected resolvers ⇒ unverified (returns False), never propagates."""
    # (a) P declares depends_on -> C
    if _declares_depends_on(own_deps, cited_id):
        return True
    # C's own deps (reverse lookup); fail-closed.
    try:
        cited_deps = resolve_deps(cited_id) or []
    except Exception:  # noqa: BLE001 — fail-closed: any resolve error ⇒ unverified, never raises
        return False
    # (b) C declares blocks -> P
    if _declares_blocks(cited_deps, plan_ticket_id):
        return True
    # Inherited: an ancestor of P carries the verified upstream edge to C.
    if resolve_parent is not None:
        return _inherited_edge_verified(
            cited_id, cited_deps, resolve_deps, resolve_parent, plan_ticket_id
        )
    return False


def unbacked_citations(
    citations: list[tuple[str, str]],
    own_deps: list[dict[str, Any]],
    resolve_deps: Callable[[str], list[dict[str, Any]]],
    plan_ticket_id: str,
    resolve_parent: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Advisory coaching strings — one per citation whose cited id is NOT a verified upstream
    prerequisite of ``plan_ticket_id`` (neither ``P.depends_on(C)``, ``C.blocks(P)``, nor — when
    ``resolve_parent`` is supplied — an inherited ancestor edge to C). Each carries its fix
    inline. Never blocks (p6 is advisory). Returns ``[]`` when every citation is edge-backed."""
    issues: list[str] = []
    for subject, cited_id in citations:
        if _edge_verified(cited_id, own_deps, resolve_deps, plan_ticket_id, resolve_parent):
            continue
        subj = (subject or "").strip()[:80]
        issues.append(
            f"citation [rebar:{cited_id}] ({subj!r}) names no VERIFIED upstream prerequisite: "
            f"P neither declares `depends_on -> {cited_id}` nor is blocked by it. Add a "
            f"`depends_on` edge to {cited_id}, ensure {cited_id} `blocks` this ticket, or remove "
            "the citation. Until then the cited symbol is grounded as normal (fails closed)."
        )
    return issues
