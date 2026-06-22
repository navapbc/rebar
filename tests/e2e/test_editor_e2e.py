"""Faithful E2E round-trip of the workflow visual editor against the REAL bpmn-io libs.

The unit suite (``tests/unit/workflow/test_bpmn.py``) round-trips through ``xml.etree``,
which preserves anything it is given. The browser editor does not — it reads/writes with
``bpmn-moddle`` and lays out with ``bpmn-auto-layout``. These tests drive those libraries
(via ``js/roundtrip.mjs``) so the IR<->BPMN contract is checked against the same code the
human's editor runs. Each test asserts the DESIRED behavior; they were written RED (they
reproduced the "branch arms vanish on Save" defect) and turned GREEN by the fix.
"""

from __future__ import annotations

import pytest

from rebar.llm.workflow.bpmn import REBAR_MODDLE_DESCRIPTOR, bpmn_to_ir, ir_to_bpmn
from rebar.llm.workflow.lint import lint_workflow
from rebar.llm.workflow.schema import dump_workflow

pytestmark = pytest.mark.e2e


def _branchy_ir() -> dict:
    return {
        "schema_version": "2",
        "name": "branchy",
        "steps": [
            {"id": "start", "uses": "noop"},
            {
                "id": "decide",
                "needs": ["start"],
                "branch": {
                    "when": "${{ steps.start.outputs.ok }}",
                    "then": [{"id": "approve", "uses": "emit"}],
                    "else": [{"id": "reject", "uses": "emit"}],
                },
            },
        ],
    }


def _editor_save(bpmn_harness, ir: dict) -> tuple[dict, list[str]]:
    """Mimic the editor Save: our IR -> BPMN -> REAL bpmn-moddle parse+serialize (what the
    browser writes on Save) -> our reconstruction. Returns (reconstructed_ir, lint_errors)."""
    saved_xml = bpmn_harness(ir_to_bpmn(ir), moddle=REBAR_MODDLE_DESCRIPTOR)["xml"]
    doc = bpmn_to_ir(saved_xml)
    errors = [str(f) for f in lint_workflow(dump_workflow(doc)) if f.severity == "error"]
    return doc, errors


# ── Serialization faithfulness (the @-id branch defect) ────────────────────────


def test_emitted_bpmn_has_no_illegal_ids(bpmn_harness):
    """Every id we emit must be a legal BPMN id (NCName). The real parser reports an
    `illegal ID` warning AND DROPS the element for anything it can't accept — which is how
    branch arms silently disappeared on Save."""
    resp = bpmn_harness(ir_to_bpmn(_branchy_ir()), moddle=REBAR_MODDLE_DESCRIPTOR)
    illegal = [w for w in resp["warnings"] if "illegal ID" in w]
    assert illegal == [], "emitted ids the real BPMN parser rejects:\n" + "\n".join(illegal)


def test_branch_arms_survive_real_editor_save(bpmn_harness):
    """A branch's then/else arms must survive a real editor Save round-trip. (Reproduced
    the user's `steps/.../branch: 'then' is a required property` rejection.)"""
    doc, errors = _editor_save(bpmn_harness, _branchy_ir())
    assert errors == [], f"editor Save would be rejected: {errors}"
    decide = next(s for s in doc["steps"] if s["id"] == "decide")
    assert set(decide["branch"]) >= {"when", "then", "else"}
    assert decide["branch"]["then"][0]["id"] == "approve"
    assert decide["branch"]["else"][0]["id"] == "reject"


def test_full_demo_round_trips_through_real_serializer(bpmn_harness):
    """The comprehensive sample (every construct) survives a real editor Save unchanged."""
    from pathlib import Path

    from rebar.llm.workflow.migrate import migrate_to_current
    from rebar.llm.workflow.schema import load_workflow

    wf = Path(__file__).resolve().parents[2] / ".rebar" / "workflows" / "roundtrip-demo.yaml"
    if not wf.is_file():
        pytest.skip("roundtrip-demo.yaml not present")
    ir = migrate_to_current(load_workflow(wf))
    doc, errors = _editor_save(bpmn_harness, ir)
    assert errors == [], f"demo Save rejected: {errors}"
    # All nine top-level steps survive with their kinds.
    assert [s["id"] for s in doc["steps"]] == [s["id"] for s in ir["steps"]]


def test_rebar_config_survives_real_parse(bpmn_harness):
    """The `<rebar:Config>` extension (carrying with/mode/loop config) must survive a real
    parse — the moddle descriptor is what prevents bpmn-io from stripping it."""
    ir = {
        "schema_version": "2",
        "name": "cfg",
        "steps": [{"id": "a", "uses": "op", "with": {"k": "v"}, "if": "${{ inputs.go }}"}],
    }
    resp = bpmn_harness(ir_to_bpmn(ir), moddle=REBAR_MODDLE_DESCRIPTOR)
    assert "rebar:Config" in resp["xml"]
    doc = bpmn_to_ir(resp["xml"])
    assert doc["steps"][0]["with"] == {"k": "v"}


# ── Layout faithfulness (the "all on one row / overlapping / center arrows" defects) ──


def test_auto_layout_keeps_every_step_and_avoids_overlap(bpmn_harness):
    """The editor lays out with bpmn-auto-layout. Feeding our emitted BPMN to it must (a)
    keep every step (nothing dropped to an illegal id) and (b) place top-level shapes
    without exact-overlap — the single-row collision (`fetch_commits`/`fetch_epic_graph`
    drawn on top of each other) is what this guards against."""
    import xml.etree.ElementTree as ET

    ir = {
        "schema_version": "2",
        "name": "fan",
        "steps": [
            {"id": "root", "uses": "a"},
            {"id": "left", "uses": "b", "needs": ["root"]},
            {"id": "right", "uses": "c", "needs": ["root"]},
            {"id": "join", "uses": "d", "needs": ["left", "right"]},
        ],
    }
    laid = bpmn_harness(ir_to_bpmn(ir), mode="layout")["xml"]
    root = ET.fromstring(laid)
    ns = {
        "di": "http://www.omg.org/spec/BPMN/20100524/DI",
        "dc": "http://www.omg.org/spec/DD/20100524/DC",
    }
    centers = []
    for shp in root.iter("{http://www.omg.org/spec/BPMN/20100524/DI}BPMNShape"):
        b = shp.find("dc:Bounds", ns)
        if b is not None:
            centers.append((shp.get("bpmnElement"), float(b.get("x")), float(b.get("y"))))
    coords = {(round(x), round(y)) for _id, x, y in centers}
    assert len(coords) == len(centers), f"shapes overlap exactly: {centers}"
    # parallel siblings `left`/`right` must not share a row (the core single-row bug)
    pos = {i: (x, y) for i, x, y in centers}
    assert pos["left"][1] != pos["right"][1], "parallel siblings drawn on the same row"
