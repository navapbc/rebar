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


def test_slim_persists_finding_prose_for_re_grounding() -> None:
    """child e344: _slim persists the finding PROSE (finding / suggested_fix /
    checklist_item) into the SIDECAR finding so a remediation re-review can re-ground
    its Pass-2 novelty sub-call — while leaving the surfaced verdict byte-unchanged."""
    verdict = {
        "verdict": "PASS",
        "ticket_id": "T-2",
        "ticket_type": "task",
        "advisory": [
            {
                "id": "fdef",
                "finding": "The retry budget is unbounded.",
                "suggested_fix": "Cap retries at 3 with backoff.",
                "checklist_item": "- [ ] Bound the retry budget.",
                "criteria": ["T5a"],
                "location": "Scope: retries",
                "tier": "LLM",
                "decision": "advisory",
                "priority": 0.4,
                # story 4e19: evidence/scenarios are now PERSISTED (lossless v2 audit record)
                "scenarios": ["retry storm"],
                "evidence": ["no cap stated"],
                # _agentic stays a runtime-only carrier that _slim deliberately drops
                "_agentic": True,
            }
        ],
        "coverage": {"metrics": {}},
        "coaching": [],
    }
    before = copy.deepcopy(verdict)
    payload = sidecar.build_payload(verdict, material="m")

    # surfaced verdict is byte-for-byte unchanged (no key added/removed, no mutation)
    assert verdict == before

    sf = payload["findings"][0]
    # the three prose fields AC1 requires are persisted to the sidecar event
    assert sf["finding"] == "The retry budget is unbounded."
    assert sf["suggested_fix"] == "Cap retries at 3 with backoff."
    assert sf["checklist_item"] == "- [ ] Bound the retry budget."
    # story 4e19: evidence + scenarios are now persisted in the lossless v2 record
    assert sf["scenarios"] == ["retry storm"]
    assert sf["evidence"] == ["no cap stated"]
    # a runtime-only carrier is still NOT persisted (lean-sidecar field-selection principle)
    assert "_agentic" not in sf


# ── (WS9, epic cite-stone-sea) cohort field ─────────────────────────────────────
def test_cohort_persists_through_slim_and_missing_is_unknown() -> None:
    """cohort round-trips into the SIDECAR finding; a finding WITHOUT a cohort reads back None —
    offline analysis treats a MISSING cohort as 'unknown', never as an empty/isolated set."""
    verdict = {
        "verdict": "PASS",
        "ticket_id": "T-9",
        "ticket_type": "task",
        "advisory": [
            {
                "id": "with",
                "finding": "x",
                "criteria": ["G3"],
                "location": "L",
                "decision": "advisory",
                "cohort": ["G3", "G4"],
            },
            {
                "id": "without",
                "finding": "y",
                "criteria": ["F1"],
                "location": "L",
                "decision": "advisory",
            },
        ],
        "coverage": {"metrics": {}},
        "coaching": [],
    }
    payload = sidecar.build_payload(verdict, material="m")
    byid = {f["id"]: f for f in payload["findings"]}
    assert byid["with"]["cohort"] == ["G3", "G4"]
    assert byid["without"]["cohort"] is None  # missing => unknown


def test_cohort_stamped_by_finder_paths() -> None:
    """pass1_chunk stamps the sorted chunk id set; pass1_isf stamps the singleton ['ISF'];
    pass1_container stamps the sorted container criteria ids."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import passes
    from rebar.llm.runner import FakeRunner

    cfg = LLMConfig()
    ch = passes.pass1_chunk(
        FakeRunner(findings=[{"finding": "x", "criteria": ["G3"], "location": "L"}]),
        cfg,
        plan="P",
        chunk=[{"id": "G3"}, {"id": "F1"}],
    )
    assert ch and ch[0]["cohort"] == ["F1", "G3"]

    isf = passes.pass1_isf(
        FakeRunner(findings=[{"finding": "y", "criteria": ["ISF"], "location": "L"}]),
        cfg,
        plan="P",
        session_log_text="log",
    )
    assert isf and isf[0]["cohort"] == ["ISF"]

    cont = passes.pass1_container(
        FakeRunner(findings=[{"finding": "z", "criteria": ["G3"], "location": ""}]),
        cfg,
        parent_plan="P",
        children=[{"id": "c1", "title": "t", "description": "d"}],
        criteria=[{"id": "G3"}, {"id": "G4"}],
        sibling_roster="",
    )
    assert cont and cont[0]["cohort"] == ["G3", "G4"]


def test_cohort_contamination_rate_skips_missing() -> None:
    """The blocking-tier contamination-rate script: cohort>1 = contaminated, missing = unknown
    (excluded from the denominator), advisory findings ignored."""
    import importlib.util
    from pathlib import Path

    path = Path(sidecar.__file__).parents[4] / "scripts" / "plan_review_contamination_rate.py"
    spec = importlib.util.spec_from_file_location("_contam", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    r = mod.contamination_rate(
        [
            {"decision": "block", "cohort": ["G3", "G4"]},  # contaminated (>1)
            {"decision": "block", "cohort": ["F1"]},  # isolated
            {"decision": "block"},  # missing => unknown, excluded
            {"decision": "advisory", "cohort": ["A", "B"]},  # not blocking-tier
        ]
    )
    assert r["blocking_with_cohort"] == 2 and r["contaminated"] == 1
    assert r["unknown"] == 1 and r["rate"] == 0.5
