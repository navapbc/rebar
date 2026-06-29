"""The REVIEW_RESULT sidecar OBSERVABILITY enrichment (db7b follow-on / story 3d3d).

The sidecar gains a reword-tolerant ``norm_id`` + the finding ``location`` so the
voluntary-revision calibration signal is cleanly joinable across re-reviews. The
enrichment is OBSERVABILITY-ONLY: it lands on the sidecar event payload, NEVER on the
surfaced verdict the library / MCP / CLI return — these tests pin that boundary.
"""

from __future__ import annotations

import copy

import pytest

from rebar.llm.plan_review import sidecar

pytestmark = pytest.mark.unit


def test_norm_id_is_reword_tolerant_and_criterion_scoped() -> None:
    """norm_id is the SORTED SET of significant tokens + criteria, so it collapses
    reordering, punctuation, and short filler words (the same significant-token defect
    mints the same id); a different criterion set or a different token set differs."""
    a = {"finding": "The scope boundaries are ambiguous.", "criteria": ["E2"]}
    b = {
        "finding": "Ambiguous! The boundaries, scope...",
        "criteria": ["E2"],
    }  # reordered + punctuation
    c = {
        "finding": "The scope boundaries are ambiguous.",
        "criteria": ["E1"],
    }  # different criterion
    d = {"finding": "The scope is undefined.", "criteria": ["E2"]}  # different tokens
    assert sidecar.norm_id(a) == sidecar.norm_id(b)  # reword/reorder-tolerant
    assert sidecar.norm_id(a) != sidecar.norm_id(c)  # criterion-scoped
    assert sidecar.norm_id(a) != sidecar.norm_id(d)  # token-sensitive
    assert sidecar.norm_id(a).startswith("n")


def test_sidecar_enrichment_is_observability_only() -> None:
    """build_payload adds norm_id + location to the SIDECAR findings, and leaves the
    input verdict (the surfaced library/MCP/CLI return) byte-for-byte unchanged."""
    verdict = {
        "verdict": "PASS",
        "ticket_id": "T-1",
        "ticket_type": "task",
        "advisory": [
            {
                "id": "fabc",
                "finding": "Hot path lacks a time bound.",
                "criteria": ["T5a"],
                "location": "Scope: latency",
                "tier": "LLM",
                "decision": "advisory",
                "priority": 0.4,
                "validity": 0.8,
                "impact": 0.5,
                "verification": {"binary": {}, "severity_attributes": {}},
            }
        ],
        "coverage": {"metrics": {}},
        "coaching": [],
    }
    before = copy.deepcopy(verdict)
    payload = sidecar.build_payload(verdict, material="m")

    # surfaced verdict is untouched (no norm_id / no mutation leaked back)
    assert verdict == before
    assert "norm_id" not in verdict["advisory"][0]

    # sidecar findings carry the enrichment
    sf = payload["findings"][0]
    assert sf["norm_id"] == sidecar.norm_id(verdict["advisory"][0])
    assert sf["location"] == "Scope: latency"
    assert sf["id"] == "fabc"  # exact id still present + unchanged
