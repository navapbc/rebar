"""Unit tests for the deterministic ADVISORY cross-ticket citation lint
(story 266e, plan-review citation grounding).

``det_citation`` mirrors ``det_operator_attested``: a pure-stdlib leaf whose
signal is surfaced advisorily through ``det_floor.p6_ac_quality`` (which never
blocks). It parses ``<subject> [rebar:<id>]`` tokens and verifies each cited id
is a DIRECT UPSTREAM prerequisite of the plan ticket P via BOTH encodings:
(a) P's own ``depends_on`` edge to C, or (b) C's own ``blocks`` edge to P (a
reverse lookup through an injected ``resolve_deps``). A ``blocks`` edge in P's
OWN deps points DOWNSTREAM and does NOT verify. The reverse lookup is
fail-closed: any exception ⇒ that citation is treated UNVERIFIED, never raised.
"""

from __future__ import annotations

from rebar.llm.plan_review import det_citation
from rebar.llm.plan_review.det_floor import (
    PlanContext,
    det_blocking_findings,
    p6_ac_quality,
    run_det_floor,
)


def _no_deps(_cid: str) -> list[dict]:
    return []


def test_parse() -> None:
    text = (
        "## Approach\n"
        "We reuse the parser in `src/rebar/x.py` [rebar:1234-abcd] and the\n"
        "helper alias proscience-sudorific-rhino [rebar:proscience-sudorific-rhino].\n"
    )
    cites = det_citation.parse_citations(text)
    ids = [tid for _subj, tid in cites]
    assert ids == ["1234-abcd", "proscience-sudorific-rhino"]
    # subject is the free text preceding the token on its line
    subj0 = cites[0][0]
    assert "src/rebar/x.py" in subj0
    assert "[rebar:" not in subj0


def test_edge_both_directions() -> None:
    P = "aaaa-bbbb-cccc-dddd"
    C = "1111-2222"
    cites = [("subject", C)]

    # (a) P declares depends_on -> C  => VERIFIED (no unbacked issue)
    own = [{"relation": "depends_on", "target_id": C}]
    assert det_citation.unbacked_citations(cites, own, _no_deps, P) == []

    # (b) C declares blocks -> P (reverse lookup) => VERIFIED
    def c_blocks_p(cid: str) -> list[dict]:
        assert cid == C
        return [{"relation": "blocks", "target_id": P}]

    assert det_citation.unbacked_citations(cites, [], c_blocks_p, P) == []

    # DOWNSTREAM: a blocks->X entry in P's OWN deps does NOT verify => UNBACKED
    own_downstream = [{"relation": "blocks", "target_id": C}]
    issues = det_citation.unbacked_citations(cites, own_downstream, _no_deps, P)
    assert len(issues) == 1
    assert C in issues[0] and "[rebar:" in issues[0]

    # NO EDGE at all => UNBACKED
    issues = det_citation.unbacked_citations(cites, [], _no_deps, P)
    assert len(issues) == 1
    assert C in issues[0]


def test_advisory_never_blocks() -> None:
    """A plan with an edge-unbacked citation surfaces the signal but NEVER
    produces a blocking DET finding."""
    P = "aaaa-bbbb-cccc-dddd"
    desc = "## Acceptance Criteria\n- [ ] wire the thing; relies on `foo` [rebar:1111-2222]\n"
    ctx = PlanContext(
        ticket_id=P,
        ticket_type="task",
        title="T",
        description=desc,
        state={"deps": []},
    )
    results = run_det_floor(ctx)
    # No blocking finding anywhere mentions a citation.
    for f in det_blocking_findings(results):
        joined = f.get("finding", "") + " ".join(f.get("evidence", []) or [])
        assert "[rebar:" not in joined


def test_det_floor_wires_citation() -> None:
    """p6_ac_quality invokes det_citation in its advisory lane: an edge-unbacked
    ``[rebar:<id>]`` yields the advisory citation issue AND the verdict is not a
    hard block."""
    P = "aaaa-bbbb-cccc-dddd"
    desc = (
        "## Acceptance Criteria\n"
        "- [ ] add caller for `resolve` [rebar:1111-2222]; proof: `pytest -q`\n"
    )
    ctx = PlanContext(
        ticket_id=P,
        ticket_type="task",
        title="T",
        description=desc,
        state={"deps": []},  # deps LACK the edge
    )
    r = p6_ac_quality(ctx)
    assert r.blocking is False
    assert r.finding is not None
    assert any("[rebar:" in e for e in r.finding["evidence"])
    assert r.coverage.get("citation_gaps", 0) >= 1


def test_reverse_lookup_error_fails_closed() -> None:
    """When the injected resolve_deps raises, the citation is reported UNVERIFIED
    and NO exception escapes det_citation."""
    P = "aaaa-bbbb-cccc-dddd"
    C = "1111-2222"

    def boom(_cid: str) -> list[dict]:
        raise RuntimeError("store unavailable")

    issues = det_citation.unbacked_citations([("subj", C)], [], boom, P)
    assert len(issues) == 1
    assert C in issues[0]
