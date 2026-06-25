"""Editor support for the v3 `batch` step (A4): the IR<->BPMN round-trip preserves the batch
step's authored, prompt-library-backed `criteria` list (plus the finder/budget/ladder) and its
`when` overlays losslessly, so the editor can render + ADD / REMOVE / EDIT a criterion. The
criteria ride in <rebar:Config>; the inspector surfaces each criterion prompt's contract.
"""

from __future__ import annotations

import copy

import pytest

from rebar.llm.workflow import bpmn
from rebar.llm.workflow import lint as _lint
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import schema as _schema
from rebar.llm.workflow.editor_contracts import resolve_contracts

pytestmark = pytest.mark.unit


def _doc(criteria: list[dict]) -> dict:
    return {
        "schema_version": "3",
        "name": "rt",
        "steps": [
            {"id": "t", "uses": "overlay_triggers", "with": {"text": "x"}},
            {
                "id": "finders",
                "needs": ["t"],
                "batch": {
                    "prompt": "code-quality",
                    "criteria": criteria,
                    "usd_budget": 2.0,
                    "model_ladder": ["claude-haiku-4-5", "claude-sonnet-4-6"],
                },
            },
        ],
    }


def _roundtrip_batch(doc: dict) -> dict:
    back = bpmn.bpmn_to_ir(bpmn.ir_to_bpmn(doc))
    return next(s for s in back["steps"] if s["id"] == "finders")["batch"]


_BASE_CRITERIA = [
    {"prompt": "ticket-quality"},
    {"prompt": "security", "when": "${{ steps.t.outputs.security }}"},
    {"prompt": "tests"},
]


def test_batch_round_trip_is_lossless():
    doc = _doc(copy.deepcopy(_BASE_CRITERIA))
    rt = _roundtrip_batch(doc)
    assert rt == doc["steps"][1]["batch"]  # finder + criteria + when + budget + ladder verbatim


def test_add_criterion_round_trips():
    doc = _doc(copy.deepcopy(_BASE_CRITERIA))
    doc["steps"][1]["batch"]["criteria"].append({"prompt": "spec-alignment"})
    rt = _roundtrip_batch(doc)
    assert [c["prompt"] for c in rt["criteria"]] == [
        "ticket-quality",
        "security",
        "tests",
        "spec-alignment",
    ]


def test_remove_criterion_round_trips():
    doc = _doc([c for c in copy.deepcopy(_BASE_CRITERIA) if c["prompt"] != "security"])
    rt = _roundtrip_batch(doc)
    assert [c["prompt"] for c in rt["criteria"]] == ["ticket-quality", "tests"]


def test_edit_criterion_prompt_and_when_round_trips():
    crit = copy.deepcopy(_BASE_CRITERIA)
    crit[1] = {"prompt": "spec-alignment", "when": "${{ steps.t.outputs.has_children }}"}
    rt = _roundtrip_batch(_doc(crit))
    edited = rt["criteria"][1]
    assert edited["prompt"] == "spec-alignment"
    assert edited["when"] == "${{ steps.t.outputs.has_children }}"


def test_reconstructed_batch_is_schema_valid_and_lint_clean():
    doc = _doc(copy.deepcopy(_BASE_CRITERIA))
    back = bpmn.bpmn_to_ir(bpmn.ir_to_bpmn(doc))
    back = _migrate.migrate_to_current(back)
    assert _schema.validate_document(back) == []
    # The reference linter (frame/expr integrity) is clean on the reconstructed doc.
    assert [str(f) for f in _lint.lint_document(back) if f.severity != "warning"] == []


def test_inspector_surfaces_batch_finder_and_criteria_contracts():
    doc = _doc(copy.deepcopy(_BASE_CRITERIA))
    contracts = resolve_contracts(doc)
    # The finder + each criterion prompt are presented to the inspector (one library).
    for pid in ("code-quality", "ticket-quality", "security", "tests"):
        assert pid in contracts, f"inspector missing the {pid!r} contract"


def test_prompt_step_if_overlay_round_trips():
    # The existing `if:` overlay predicate is non-structural, so it round-trips via Config.
    doc = {
        "schema_version": "3",
        "name": "rt",
        "steps": [
            {"id": "t", "uses": "overlay_triggers", "with": {"text": "x"}},
            {
                "id": "review",
                "needs": ["t"],
                "prompt": "code-quality",
                "if": "${{ steps.t.outputs.security }}",
            },
        ],
    }
    back = bpmn.bpmn_to_ir(bpmn.ir_to_bpmn(doc))
    review = next(s for s in back["steps"] if s["id"] == "review")
    assert review["if"] == "${{ steps.t.outputs.security }}"
