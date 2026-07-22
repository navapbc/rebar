from __future__ import annotations

from rebar.llm.plan_review import prerequisites


def test_normalize_coverage_accepts_standard_runner_envelope_metadata() -> None:
    prerequisite_id = "aaaa-bbbb-cccc-dddd"
    raw = {
        "records": [
            {
                "prerequisite_id": prerequisite_id,
                "disposition": "finding",
                "findings": [
                    {
                        "finding": "The plans conflict.",
                        "criteria": ["prerequisite-consistency"],
                        "prerequisite_id": prerequisite_id,
                    }
                ],
            }
        ],
        "runner": "pydantic_ai",
        "model": "anthropic:claude-opus-4-8",
        "trace_id": None,
        "_usage": {"input_tokens": 100, "output_tokens": 20, "requests": 1},
    }

    normalized = prerequisites.normalize_coverage_records(raw, [prerequisite_id])

    assert normalized[0]["disposition"] == "finding"
    assert normalized[0]["findings"][0]["prerequisite_id"] == prerequisite_id


def test_normalize_coverage_still_rejects_unknown_record_fields() -> None:
    prerequisite_id = "aaaa-bbbb-cccc-dddd"
    raw = {
        "records": [
            {
                "prerequisite_id": prerequisite_id,
                "disposition": "consistent",
                "findings": [],
                "unexpected_inside_contract": True,
            }
        ],
        "runner": "pydantic_ai",
    }

    assert prerequisites.normalize_coverage_records(raw, [prerequisite_id]) == [
        {
            "prerequisite_id": prerequisite_id,
            "disposition": "indeterminate",
            "findings": [],
            "reason_code": "output-invalid",
        }
    ]
