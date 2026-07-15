"""Story a3db: pass-4 coaching lossless persistence in the review sidecars.

The plan-review sidecar's _slim previously stored each coaching note as only
{move_id, finding_refs}, dropping move_name, subject, and the rendered coaching prose — so a
downstream audit UI could not re-render the note. Persist the FULL coaching record
{move_id, move_name, subject, finding_refs, coaching} in the plan-review sidecar; the
code-review sidecar already persists the full array (regression-locked here).
"""

from __future__ import annotations

import pytest

from rebar.llm.code_review import sidecar as code_sidecar
from rebar.llm.plan_review import sidecar as plan_sidecar

pytestmark = pytest.mark.unit


def _coaching_note() -> dict:
    return {
        "move_id": "clarify-scope",
        "move_name": "Clarify the scope boundary",
        "subject": "the retry-budget section",
        "finding_refs": ["fabc", "fdef"],
        "coaching": "Consider stating the retry ceiling explicitly so the boundary is testable.",
    }


def test_plan_review_sidecar_persists_full_coaching_note() -> None:
    """The plan-review sidecar payload persists every pass-4 coaching field — move_id, move_name,
    subject, finding_refs, and the rendered coaching prose — not just {move_id, finding_refs}."""
    verdict = {
        "verdict": "PASS",
        "ticket_id": "T-1",
        "ticket_type": "task",
        "advisory": [],
        "coverage": {"metrics": {}},
        "coaching": [_coaching_note()],
    }
    payload = plan_sidecar.build_payload(verdict, material="m")
    c = payload["coaching"][0]
    assert c["move_id"] == "clarify-scope"
    assert c["move_name"] == "Clarify the scope boundary"
    assert c["subject"] == "the retry-budget section"
    assert c["finding_refs"] == ["fabc", "fdef"]
    assert c["coaching"] == (
        "Consider stating the retry ceiling explicitly so the boundary is testable."
    )


def test_plan_review_coaching_missing_fields_degrade_to_none() -> None:
    """A coaching note missing a field yields None for it (schema-tolerant), never KeyError."""
    verdict = {
        "verdict": "PASS",
        "ticket_id": "T-2",
        "ticket_type": "task",
        "advisory": [],
        "coverage": {"metrics": {}},
        "coaching": [{"move_id": "m1", "finding_refs": ["f1"]}],
    }
    c = plan_sidecar.build_payload(verdict, material="m")["coaching"][0]
    assert c["move_id"] == "m1"
    assert c["finding_refs"] == ["f1"]
    assert c["move_name"] is None and c["subject"] is None and c["coaching"] is None


def test_code_review_sidecar_persists_full_coaching_note() -> None:
    """Regression lock: the code-review sidecar payload already persists the full coaching record
    shape (it stores the coaching array verbatim) — assert the fields survive."""
    verdict = {
        "verdict": "PASS",
        "blocking": [],
        "advisory": [],
        "coaching": [_coaching_note()],
    }
    payload = code_sidecar.build_payload(verdict, target_ticket="T1")
    c = payload["coaching"][0]
    assert c["move_id"] == "clarify-scope"
    assert c["move_name"] == "Clarify the scope boundary"
    assert c["subject"] == "the retry-budget section"
    assert c["finding_refs"] == ["fabc", "fdef"]
    assert c["coaching"].startswith("Consider stating the retry ceiling")
