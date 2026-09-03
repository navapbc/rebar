"""Happy-path oracle for the plan-review fixture selector (ticket 549b).

These tests pin the CORE observable contract of ``rebar.llm.evals.fixture_selection``:
a fully-corroborated fire candidate is admitted as a ``blocking`` manifest row carrying its
three tier signals, and :func:`write_manifest` emits byte-stable sorted-keys JSONL. Edge
cases (vintage exclusion, zero-candidate rows, tier downgrades, no-fire admission,
escaped-defect priority, ranking) and the real-corpus determinism test live in the held-out
suites and are validated by the orchestrator.
"""

from __future__ import annotations

import json
from typing import Any

from rebar.llm.evals.fixture_selection import select_candidates, write_manifest


def finding(
    norm_id: str,
    *,
    criteria: list[str] | None = None,
    cohort: list[str] | None = None,
    decision_margin: float | None = None,
    decision: str = "block",
) -> dict[str, Any]:
    """A slimmed-sidecar-shaped finding carrying only the keys the selector reads."""
    return {
        "norm_id": norm_id,
        "criteria": list(criteria or []),
        "cohort": cohort,
        "decision_margin": decision_margin,
        "decision": decision,
    }


def review(
    uuid: str,
    ts: int,
    fingerprint: str,
    findings: list[dict[str, Any]],
    *,
    ticket_id: str = "t1",
    verdict: str = "BLOCK",
    ticket_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "review_event_ts": ts,
        "review_event_uuid": uuid,
        "verdict": verdict,
        "material_fingerprint": fingerprint,
        "findings": findings,
        "ticket_state": ticket_state or {},
    }


def _fully_corroborated_reviews() -> list[dict[str, Any]]:
    """n1 fires for project.alpha in two equal-fingerprint reviews (reproduction consensus),
    then drops out across a consecutive differing-material pair (author response), with a
    margin at/above the floor. All reviews postdate the injected rubric vintage (ts 1000)."""
    n1 = "n1"
    return [
        review("u-a1", 1001, "A", [finding(n1, criteria=["project.alpha"], decision_margin=0.20)]),
        review("u-a2", 1002, "A", [finding(n1, criteria=["project.alpha"], decision_margin=0.20)]),
        review("u-b1", 1003, "B", []),
    ]


def test_fully_corroborated_fire_candidate_is_blocking_with_all_signals():
    rows = select_candidates(
        _fully_corroborated_reviews(),
        criteria_ids=["project.alpha"],
        rubric_history=lambda c: 1000,
    )
    cands = [r for r in rows if r["kind"] == "candidate"]
    assert len(cands) == 1
    c = cands[0]
    assert c["criterion"] == "project.alpha"
    assert c["direction"] == "fire"
    assert c["norm_id"] == "n1"
    assert c["tier"] == "blocking"
    assert sorted(c["signals"]) == ["author_response", "margin", "reproduction_consensus"]
    assert c["escaped_defect"] is False
    assert c["abs_margin"] == 0.20
    assert c["rank"] == 0


def _norm_less_finding(*, criteria: list[str]) -> dict[str, Any]:
    """A historical slimmed finding that names a criterion but carries NO ``norm_id`` key at
    all — the shape (from sidecar events predating the ``norm_id`` field) that crashed
    real-corpus selection (ticket 57c4-4834-2a7a-4a05)."""
    return {
        "criteria": list(criteria),
        "cohort": None,
        "decision_margin": 0.20,
        "decision": "block",
    }


def _reviews_with_norm_less() -> list[dict[str, Any]]:
    """``_fully_corroborated_reviews`` with a norm_id-less finding (naming the same criterion)
    added to every review — so the norm-less finding is routed through BOTH the ``_fire_rows``
    grouping AND the ``_author_response_norm_ids`` -> ``classify_finding_survival`` survival path
    (the u-a2/u-b1 differing-material pair)."""
    nl = lambda: _norm_less_finding(criteria=["project.alpha"])  # noqa: E731
    return [
        review(
            "u-a1",
            1001,
            "A",
            [finding("n1", criteria=["project.alpha"], decision_margin=0.20), nl()],
        ),
        review(
            "u-a2",
            1002,
            "A",
            [finding("n1", criteria=["project.alpha"], decision_margin=0.20), nl()],
        ),
        review("u-b1", 1003, "B", [nl()]),
    ]


def test_norm_less_findings_do_not_crash_selection():
    """A criterion-naming finding with no ``norm_id`` key no longer aborts selection through
    either norm-keyed path; the norm-bearing candidate is still produced (57c4 AC1)."""
    rows = select_candidates(
        _reviews_with_norm_less(),
        criteria_ids=["project.alpha"],
        rubric_history=lambda c: 1000,
    )
    cands = [r for r in rows if r["kind"] == "candidate"]
    assert [c["norm_id"] for c in cands] == ["n1"]


def test_norm_less_finding_contributes_no_candidate_and_does_not_perturb():
    """The norm-less finding yields NO candidate and leaves the norm-bearing rows byte-identical
    to the baseline where it is absent (57c4 AC2)."""
    with_nl = select_candidates(
        _reviews_with_norm_less(),
        criteria_ids=["project.alpha"],
        rubric_history=lambda c: 1000,
    )
    without_nl = select_candidates(
        _fully_corroborated_reviews(),
        criteria_ids=["project.alpha"],
        rubric_history=lambda c: 1000,
    )
    assert with_nl == without_nl


def test_norm_less_selection_is_deterministic():
    """Two runs over the same norm-less-bearing eligible set return byte-identical rows
    (57c4 AC4)."""
    first = select_candidates(
        _reviews_with_norm_less(),
        criteria_ids=["project.alpha"],
        rubric_history=lambda c: 1000,
    )
    second = select_candidates(
        _reviews_with_norm_less(),
        criteria_ids=["project.alpha"],
        rubric_history=lambda c: 1000,
    )
    assert first == second


def test_write_manifest_emits_sorted_key_jsonl(tmp_path):
    rows = select_candidates(
        _fully_corroborated_reviews(),
        criteria_ids=["project.alpha"],
        rubric_history=lambda c: 1000,
    )
    out = tmp_path / "manifest.jsonl"
    write_manifest(rows, out)
    text = out.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == len(rows)
    for line, row in zip(lines, rows, strict=True):
        assert json.loads(line) == row
        # keys sorted → serialising the parsed row with sort_keys reproduces the line
        assert json.dumps(json.loads(line), sort_keys=True) == line
