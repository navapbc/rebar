"""Story 4e19: plan-review sidecar -> lossless v2 (evidence, scenarios, thresholds).

The plan-review REVIEW_RESULT sidecar drops the Pass-1 ``evidence``/``scenarios`` prose
and never records the numeric ``block_threshold``/``blocking_enabled`` a finding was judged
against. v2 persists all four, per finding, so an auditor can see a finding's grounding
quotes AND the exact decision boundary that was applied. The surfaced verdict shape and the
all-buckets pooling stay byte-unchanged; the reader tolerates both v1 and v2.
"""

from __future__ import annotations

import copy

import pytest

from rebar.llm import review_kernel
from rebar.llm.plan_review import sidecar

pytestmark = pytest.mark.unit


def _verif(binary=None, attrs=None) -> dict:
    base_b = {q: "yes" for q in review_kernel.GRADED_BINARY}
    base_b["cited_reference_accurate"] = "na"
    base_a = {
        "prod_impact": "high",
        "debt_impact": "high",
        "blast_radius": "system",
        "likelihood": "high",
        "reversibility": "hard",
    }
    return {
        "binary": {**base_b, **(binary or {})},
        "severity_attributes": {**base_a, **(attrs or {})},
    }


# ── HAPPY PATH (shared with the implementer) ────────────────────────────────────────────
def test_pass3_decide_exposes_resolved_threshold() -> None:
    """Pass-3 decide's output carries the resolved (block_threshold, blocking_enabled) it
    judged the finding against — the values a consumer passed in, echoed onto the decision so
    the sidecar can persist the exact boundary that applied."""
    d = review_kernel.pass3_decide(_verif(), block_threshold=0.7, blocking_enabled=True)
    assert d["block_threshold"] == 0.7
    assert d["blocking_enabled"] is True
    # a second, different posture round-trips its own values (not a hard-coded constant)
    d2 = review_kernel.pass3_decide(_verif(), block_threshold=0.95, blocking_enabled=False)
    assert d2["block_threshold"] == 0.95
    assert d2["blocking_enabled"] is False


def test_build_payload_v2_persists_evidence_scenarios_and_thresholds() -> None:
    """build_payload bumps to plan_review_result_v2 and persists, per finding, the Pass-1
    ``evidence``/``scenarios`` prose plus the resolved ``block_threshold``/``blocking_enabled``
    the Pass-3 decision applied."""
    verdict = {
        "verdict": "PASS",
        "ticket_id": "T-1",
        "ticket_type": "task",
        "advisory": [
            {
                "id": "fabc",
                "finding": "The retry budget is unbounded.",
                "suggested_fix": "Cap retries at 3.",
                "checklist_item": "- [ ] Bound retry budget.",
                "criteria": ["T5a"],
                "location": "Scope: retries",
                "tier": "LLM",
                "decision": "advisory",
                "priority": 0.4,
                "evidence": ["no cap stated in the plan", "the loop has no ceiling"],
                "scenarios": ["retry storm on a flapping dependency"],
                "block_threshold": 0.7,
                "blocking_enabled": True,
            }
        ],
        "coverage": {"metrics": {}},
        "coaching": [],
    }
    payload = sidecar.build_payload(verdict, material="m")
    assert payload["schema"] == "plan_review_result_v2"
    sf = payload["findings"][0]
    assert sf["evidence"] == ["no cap stated in the plan", "the loop has no ceiling"]
    assert sf["scenarios"] == ["retry storm on a flapping dependency"]
    assert sf["block_threshold"] == 0.7
    assert sf["blocking_enabled"] is True


def test_build_payload_v2_is_observability_only() -> None:
    """The v2 enrichment lands on the SIDECAR payload only; the surfaced verdict is
    byte-for-byte unchanged (no key added, removed, or mutated on the input findings)."""
    verdict = {
        "verdict": "PASS",
        "ticket_id": "T-2",
        "ticket_type": "task",
        "advisory": [
            {
                "id": "f1",
                "finding": "x",
                "criteria": ["T1"],
                "location": "L",
                "decision": "advisory",
                "evidence": ["e"],
                "scenarios": ["s"],
                "block_threshold": 0.7,
                "blocking_enabled": True,
            }
        ],
        "coverage": {"metrics": {}},
        "coaching": [],
    }
    before = copy.deepcopy(verdict)
    sidecar.build_payload(verdict, material="m")
    assert verdict == before


def test_build_payload_v2_pools_all_buckets_unchanged() -> None:
    """The all-buckets pooling (blocking + advisory + overflow + indeterminate + dropped)
    is unchanged by the v2 bump — every finding across every bucket is persisted."""
    verdict = {
        "verdict": "PASS",
        "ticket_id": "T-3",
        "ticket_type": "task",
        "blocking": [{"id": "b", "finding": "b", "criteria": [], "decision": "block"}],
        "advisory": [{"id": "a", "finding": "a", "criteria": [], "decision": "advisory"}],
        "overflow": [{"id": "o", "finding": "o", "criteria": [], "decision": "advisory"}],
        "indeterminate": [{"id": "i", "finding": "i", "criteria": [], "decision": "indeterminate"}],
        "dropped": [{"id": "d", "finding": "d", "criteria": [], "decision": "dropped"}],
        "coverage": {"metrics": {}},
        "coaching": [],
    }
    payload = sidecar.build_payload(verdict, material="m")
    ids = {f["id"] for f in payload["findings"]}
    assert ids == {"b", "a", "o", "i", "d"}


def test_pass3_decide_carries_threshold_on_every_decision_path() -> None:
    """Every return path of pass3_decide — indeterminate, cited-ref veto, low-validity drop,
    and the normal advisory/block paths — carries the resolved threshold pair, so a persisted
    dropped/indeterminate finding still records the boundary it was judged against."""
    # indeterminate (no verification)
    ind = review_kernel.pass3_decide(None, block_threshold=0.8, blocking_enabled=True)
    assert ind["decision"] == "indeterminate"
    assert ind["block_threshold"] == 0.8 and ind["blocking_enabled"] is True
    # cited-reference veto -> dropped
    veto = review_kernel.pass3_decide(
        _verif(binary={"cited_reference_accurate": "no"}),
        block_threshold=0.6,
        blocking_enabled=False,
    )
    assert veto["decision"] == "dropped"
    assert veto["block_threshold"] == 0.6 and veto["blocking_enabled"] is False
    # low-validity -> dropped
    low = review_kernel.pass3_decide(
        _verif(binary={q: "no" for q in review_kernel.GRADED_BINARY}),
        block_threshold=0.9,
        blocking_enabled=True,
    )
    assert low["decision"] == "dropped"
    assert low["block_threshold"] == 0.9 and low["blocking_enabled"] is True


def test_pass3_over_findings_threads_per_criterion_thresholds() -> None:
    """pass3_over_findings resolves each finding's threshold via the consumer-supplied
    resolver and stamps the resolved pair on each decided finding — two findings with
    different criteria carry independently-resolved boundaries."""
    findings = [
        {"finding": "a", "criteria": ["HI"]},
        {"finding": "b", "criteria": ["LO"]},
    ]
    verifs = {0: _verif(), 1: _verif()}

    def resolver(criteria):
        return (0.6, True) if "HI" in criteria else (0.95, False)

    decided = review_kernel.pass3_over_findings(findings, verifs, threshold_for=resolver)
    by = {f["finding"]: f for f in decided}
    assert by["a"]["block_threshold"] == 0.6 and by["a"]["blocking_enabled"] is True
    assert by["b"]["block_threshold"] == 0.95 and by["b"]["blocking_enabled"] is False
