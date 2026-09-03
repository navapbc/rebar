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
