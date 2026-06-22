"""IR <-> BPMN serializer (00da): lossless round-trip preserving step ids, agent
metadata, conditionals (exclusiveGateway), loops (standardLoopCharacteristics),
multi-instance map (multiInstanceLoopCharacteristics), and exact config; a registered
`rebar` moddle descriptor; deterministic auto-layout.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from rebar.llm.workflow import bpmn
from rebar.llm.workflow import schema as S

# Property fixtures: each must round-trip IR -> BPMN -> IR as the IDENTITY. They cover
# the constructs + corners the example tests don't (one-armed branch, while/no-cond
# loops, nested control, unsorted needs, if-guard, explicit type, agent config,
# XML-hostile values).
_ROUND_TRIP_CASES = {
    "unsorted_needs": {
        "schema_version": "2",
        "name": "a",
        "steps": [
            {"id": "a", "uses": "o"},
            {"id": "b", "uses": "o"},
            {"id": "c", "needs": ["b", "a"], "uses": "o"},
        ],
    },
    "if_guard_and_type": {
        "schema_version": "2",
        "name": "a",
        "steps": [
            {"id": "a", "type": "scripted", "uses": "o"},
            {"id": "b", "needs": ["a"], "uses": "o", "if": "${{ steps.a.outputs.ok }}"},
        ],
    },
    "one_armed_branch": {
        "schema_version": "2",
        "name": "a",
        "inputs": {"x": {"type": "boolean"}},
        "steps": [
            {"id": "g", "branch": {"when": "${{ inputs.x }}", "then": [{"id": "t", "uses": "o"}]}}
        ],
    },
    "while_loop": {
        "schema_version": "2",
        "name": "a",
        "steps": [
            {
                "id": "L",
                "loop": {
                    "max_iterations": 5,
                    "while": "${{ steps.w.outputs.go }}",
                    "body": [{"id": "w", "uses": "o"}],
                },
            }
        ],
    },
    "no_condition_loop": {
        "schema_version": "2",
        "name": "a",
        "steps": [{"id": "L", "loop": {"max_iterations": 3, "body": [{"id": "w", "uses": "o"}]}}],
    },
    "agent_config": {
        "schema_version": "2",
        "name": "a",
        "steps": [
            {
                "id": "r",
                "prompt": "p",
                "mode": "structured",
                "output_schema": "review_result",
                "model": "anthropic:claude-opus-4-8",
            }
        ],
    },
    "nested_map_in_loop_in_branch": {
        "schema_version": "2",
        "name": "a",
        "inputs": {"x": {"type": "boolean"}, "xs": {"type": "array"}},
        "steps": [
            {
                "id": "g",
                "branch": {
                    "when": "${{ inputs.x }}",
                    "then": [
                        {
                            "id": "L",
                            "loop": {
                                "max_iterations": 2,
                                "body": [
                                    {
                                        "id": "M",
                                        "map": {
                                            "over": "${{ inputs.xs }}",
                                            "as": "i",
                                            "body": [{"id": "w", "uses": "o"}],
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "else": [{"id": "e", "uses": "o"}],
                },
            }
        ],
    },
    "xml_hostile_values": {
        "schema_version": "2",
        "name": "a",
        "steps": [{"id": "a", "uses": "o", "with": {"v": 'a & b < c > "d" 日本 ${{x}}'}}],
    },
}


def _full_v2() -> dict:
    return {
        "schema_version": "2",
        "name": "demo",
        "inputs": {"items": {"type": "array", "required": True}},
        "steps": [
            {"id": "start", "uses": "noop"},
            {
                "id": "refine",
                "needs": ["start"],
                "loop": {
                    "max_iterations": 3,
                    "until": "${{ steps.attempt.outputs.done }}",
                    "var": "i",
                    "body": [{"id": "attempt", "prompt": "refine", "with": {"n": "${{ loop.i }}"}}],
                },
            },
            {
                "id": "gate",
                "needs": ["refine"],
                "branch": {
                    "when": "${{ steps.start.outputs.ok }}",
                    "then": [{"id": "approve", "uses": "emit", "mode": "text"}],
                    "else": [{"id": "reject", "uses": "emit"}],
                },
            },
            {
                "id": "fanout",
                "needs": ["gate"],
                "map": {
                    "over": "${{ inputs.items }}",
                    "as": "item",
                    "index_var": "ix",
                    "max_concurrency": 4,
                    "body": [{"id": "process", "prompt": "proc", "with": {"x": "${{ map.item }}"}}],
                },
            },
        ],
    }


def _norm(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True)


def _q(prefix, tag):
    return f"{{{bpmn._NS[prefix]}}}{tag}"


# ── Round-trip identity ────────────────────────────────────────────────────────


def test_round_trip_is_lossless():
    doc = _full_v2()
    back = bpmn.bpmn_to_ir(bpmn.ir_to_bpmn(doc))
    assert _norm(back) == _norm(doc)


@pytest.mark.parametrize("name", sorted(_ROUND_TRIP_CASES))
def test_round_trip_identity_property(name):
    doc = _ROUND_TRIP_CASES[name]
    back = bpmn.bpmn_to_ir(bpmn.ir_to_bpmn(doc))
    assert _norm(back) == _norm(doc), f"{name} did not round-trip"


def test_editor_save_with_start_end_events_does_not_fabricate_needs():
    # bpmn-js reintroduces start/end events + flows on save; those must NOT become
    # `needs` edges (they reference non-existent steps).
    ir = {
        "schema_version": "2",
        "name": "a",
        "steps": [{"id": "a", "uses": "o"}, {"id": "b", "needs": ["a"], "uses": "o"}],
    }
    root = ET.fromstring(bpmn.ir_to_bpmn(ir).encode("utf-8"))
    proc = root.find(_q("bpmn", "process"))
    ET.SubElement(proc, _q("bpmn", "startEvent"), {"id": "Start_1"})
    ET.SubElement(proc, _q("bpmn", "endEvent"), {"id": "End_1"})
    ET.SubElement(
        proc, _q("bpmn", "sequenceFlow"), {"id": "sf0", "sourceRef": "Start_1", "targetRef": "a"}
    )
    ET.SubElement(
        proc, _q("bpmn", "sequenceFlow"), {"id": "sf1", "sourceRef": "b", "targetRef": "End_1"}
    )
    back = bpmn.bpmn_to_ir(ET.tostring(root, encoding="unicode"))
    steps = {s["id"]: s for s in back["steps"]}
    assert "needs" not in steps["a"]  # start-event flow dropped, not a fabricated dep
    assert steps["b"]["needs"] == ["a"]


def test_bare_subprocess_is_a_clear_error():
    # A sub-process with neither loop characteristic is not a rebar construct.
    xml = (
        '<?xml version="1.0"?><bpmn:definitions '
        'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">'
        '<bpmn:process id="p"><bpmn:subProcess id="sp"/></bpmn:process></bpmn:definitions>'
    )
    with pytest.raises(ValueError, match="characteristics"):
        bpmn.bpmn_to_ir(xml)


def test_subprocess_children_follow_xsd_order():
    # extensionElements MUST precede loopCharacteristics, which precedes child flow
    # elements (BPMN 2.0 XSD) — else a strict validator / bpmn-js import can choke.
    doc = {
        "schema_version": "2",
        "name": "a",
        "steps": [{"id": "L", "loop": {"max_iterations": 2, "body": [{"id": "w", "uses": "o"}]}}],
    }
    root = ET.fromstring(bpmn.ir_to_bpmn(doc).encode("utf-8"))
    sp = next(e for e in root.iter() if e.get("id") == "L")
    kinds = [k.tag.split("}")[-1] for k in list(sp)]
    assert kinds.index("extensionElements") < kinds.index("standardLoopCharacteristics")
    assert kinds.index("standardLoopCharacteristics") < kinds.index("scriptTask")


def test_round_trip_preserves_step_ids():
    doc = _full_v2()
    back = bpmn.bpmn_to_ir(bpmn.ir_to_bpmn(doc))
    assert [s["id"] for s in back["steps"]] == ["start", "refine", "gate", "fanout"]
    # nested ids survive too
    assert back["steps"][1]["loop"]["body"][0]["id"] == "attempt"
    assert {s["id"] for s in back["steps"][2]["branch"]["then"]} == {"approve"}


def test_emitted_bpmn_is_wellformed_and_maps_constructs():
    xml = bpmn.ir_to_bpmn(_full_v2())
    root = ET.fromstring(xml.encode("utf-8"))
    proc = root.find(_q("bpmn", "process"))
    by_id = {e.get("id"): e for e in proc.iter() if e.get("id")}
    # scripted -> scriptTask, agent -> serviceTask, branch -> exclusiveGateway
    assert by_id["start"].tag == _q("bpmn", "scriptTask")
    assert by_id["attempt"].tag == _q("bpmn", "serviceTask")
    assert by_id["gate"].tag == _q("bpmn", "exclusiveGateway")
    # loop -> subProcess + standardLoopCharacteristics
    assert by_id["refine"].tag == _q("bpmn", "subProcess")
    assert by_id["refine"].find(_q("bpmn", "standardLoopCharacteristics")) is not None
    # map -> subProcess + multiInstanceLoopCharacteristics; concurrency -> isSequential=false
    mi = by_id["fanout"].find(_q("bpmn", "multiInstanceLoopCharacteristics"))
    assert mi is not None and mi.get("isSequential") == "false"


def test_agent_metadata_survives_as_typed_extension():
    xml = bpmn.ir_to_bpmn(_full_v2())
    root = ET.fromstring(xml.encode("utf-8"))
    agent = None
    for el in root.iter(_q("rebar", "Agent")):
        agent = el
        break
    assert agent is not None
    # The CONTRACT is that agent metadata survives the extension round-trip (the POC
    # gotcha) — the prompt is the load-bearing field. provider/tools are editor-facing
    # display defaults, so assert present/non-empty rather than pinning the exact default.
    assert agent.get("prompt") == "refine"
    assert agent.get("provider") and agent.get("tools")


def test_serial_map_is_sequential_multiinstance():
    doc = {
        "schema_version": "2",
        "name": "serialmap",
        "inputs": {"xs": {"type": "array"}},
        "steps": [
            {
                "id": "m",
                "map": {
                    "over": "${{ inputs.xs }}",
                    "as": "x",
                    "body": [{"id": "w", "uses": "noop"}],
                },
            }
        ],
    }
    root = ET.fromstring(bpmn.ir_to_bpmn(doc).encode("utf-8"))
    mi = next(root.iter(_q("bpmn", "multiInstanceLoopCharacteristics")))
    assert mi.get("isSequential") == "true"  # default max_concurrency 1 -> sequential
    assert bpmn.bpmn_to_ir(bpmn.ir_to_bpmn(doc)) == doc


# ── Visual edits map back to the IR (the editing use-case) ─────────────────────


def test_human_rename_maps_back_to_uses():
    # A scriptTask's `name` is the step's `uses`; renaming it in the editor changes
    # the IR's uses on round-trip.
    doc = {"schema_version": "2", "name": "r", "steps": [{"id": "a", "uses": "old_op"}]}
    xml = bpmn.ir_to_bpmn(doc)
    edited = xml.replace('name="old_op"', 'name="new_op"')
    back = bpmn.bpmn_to_ir(edited)
    assert back["steps"][0]["uses"] == "new_op"


def test_added_task_appears_in_the_ir():
    # Simulate the editor adding a scriptTask wired after `a`; it shows up as a new step.
    doc = {
        "schema_version": "2",
        "name": "r",
        "steps": [{"id": "a", "uses": "op"}, {"id": "b", "needs": ["a"], "uses": "op2"}],
    }
    root = ET.fromstring(bpmn.ir_to_bpmn(doc).encode("utf-8"))
    proc = root.find(_q("bpmn", "process"))
    added = ET.SubElement(proc, _q("bpmn", "scriptTask"), {"id": "c", "name": "op3"})
    ext = ET.SubElement(added, _q("bpmn", "extensionElements"))
    ET.SubElement(ext, _q("rebar", "Config"), {"value": "{}"})
    ET.SubElement(
        proc, _q("bpmn", "sequenceFlow"), {"id": "f_b_c", "sourceRef": "b", "targetRef": "c"}
    )
    back = bpmn.bpmn_to_ir(ET.tostring(root, encoding="unicode"))
    ids = {s["id"]: s for s in back["steps"]}
    assert "c" in ids and ids["c"]["uses"] == "op3" and ids["c"]["needs"] == ["b"]


# ── Determinism, descriptor, real workflow ─────────────────────────────────────


def test_serialization_is_deterministic():
    doc = _full_v2()
    assert bpmn.ir_to_bpmn(doc) == bpmn.ir_to_bpmn(doc)


def test_moddle_descriptor_file_matches_constant():
    p = Path(bpmn.__file__).resolve().parent / "rebar.moddle.json"
    assert p.is_file(), "rebar.moddle.json must ship for the bpmn-js editor side"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk == bpmn.REBAR_MODDLE_DESCRIPTOR
    # The descriptor must declare the namespace the serializer emits + the Agent type
    # the POC proved is needed for extension survival.
    assert on_disk["uri"] == bpmn.REBAR
    assert any(t["name"] == "Agent" for t in on_disk["types"])
    assert any(t["name"] == "Config" for t in on_disk["types"])


def test_packaged_example_round_trips():
    # The shipped code_review example (v1) -> migrate to v2 -> BPMN -> IR is lossless.
    example = Path(S.__file__).resolve().parent / "examples" / "code_review.yaml"
    doc = S.parse_workflow(example.read_text(encoding="utf-8"))
    from rebar.llm.workflow.migrate import migrate_to_current

    doc = migrate_to_current(doc)
    back = bpmn.bpmn_to_ir(bpmn.ir_to_bpmn(doc))
    assert _norm(back) == _norm(doc)


def test_unmappable_element_is_a_loud_error_not_a_silent_drop():
    # An element type the IR has no mapping for (a userTask) must FAIL the read loudly, so
    # the editor Save is rejected rather than silently discarding the user's node (which
    # would let them close the editor believing it was saved). Data loss > inconvenience.
    xml = (
        '<?xml version="1.0"?><bpmn:definitions '
        'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">'
        '<bpmn:process id="p">'
        '<bpmn:scriptTask id="a" name="op"/>'
        '<bpmn:userTask id="human" name="approve"/>'  # foreign to the rebar IR
        "</bpmn:process></bpmn:definitions>"
    )
    with pytest.raises(ValueError, match="userTask.*does not map"):
        bpmn.bpmn_to_ir(xml)


def test_branch_gateway_missing_an_arm_round_trips_as_one_armed():
    # A bpmn-js edit that deletes the else-arm sub-process leaves a one-armed branch
    # (when + then only) — a valid IR shape, reconstructed without fabricating an else.
    doc = {
        "schema_version": "2",
        "name": "oa",
        "inputs": {"x": {"type": "boolean"}},
        "steps": [
            {"id": "g", "branch": {"when": "${{ inputs.x }}", "then": [{"id": "t", "uses": "o"}]}}
        ],
    }
    xml = bpmn.ir_to_bpmn(doc)
    back = bpmn.bpmn_to_ir(xml)
    assert "else" not in back["steps"][0]["branch"]
    assert back["steps"][0]["branch"]["then"][0]["id"] == "t"


def test_emitted_ids_are_legal_bpmn_ncnames():
    # Regression for the editor Save defect: branch-arm ids used '@', which is a legal XML
    # attribute char but an ILLEGAL BPMN id (NCName) — the real bpmn-io parser DROPS such
    # elements, silently deleting the branch. No emitted id may contain '@' (or ':').
    import re

    xml = bpmn.ir_to_bpmn(_full_v2())
    ids = re.findall(r'\bid="([^"]+)"', xml) + re.findall(r'bpmnElement="([^"]+)"', xml)
    bad = [i for i in ids if "@" in i or ":" in i]
    assert bad == [], f"emitted ids illegal as BPMN NCNames: {bad}"


def test_branch_arms_recovered_after_simulated_bpmnjs_id_rewrite():
    # The editor reconstructs branch arms from the gateway sequence flow + the <rebar:Config
    # _role> marker, NOT from the arm id — so even if bpmn-js regenerates the arm ids on
    # Save (as it does for ids it considers non-canonical), the then/else arms survive.
    doc = {
        "schema_version": "2",
        "name": "rw",
        "inputs": {"x": {"type": "boolean"}},
        "steps": [
            {
                "id": "g",
                "branch": {
                    "when": "${{ inputs.x }}",
                    "then": [{"id": "t", "uses": "o"}],
                    "else": [{"id": "e", "uses": "o"}],
                },
            }
        ],
    }
    xml = bpmn.ir_to_bpmn(doc)
    # Simulate bpmn-js assigning fresh ids to the arm sub-processes (role marker + the
    # gateway->arm flow targetRef are what carry the meaning, and they move together).
    xml = xml.replace("g.then", "SubProcess_0aa").replace("g.else", "SubProcess_0bb")
    back = bpmn.bpmn_to_ir(xml)
    branch = back["steps"][0]["branch"]
    assert branch["then"][0]["id"] == "t"
    assert branch["else"][0]["id"] == "e"


def test_layout_has_no_overlaps_edges_and_expanded_subprocesses():
    # The generated DI must be readable: parallel siblings get distinct positions (no
    # single-row collision), every needs/gateway edge has a BPMNEdge, and control bodies
    # are expanded sub-processes whose children sit INSIDE the parent bounds.
    import xml.etree.ElementTree as ET

    root = ET.fromstring(bpmn.ir_to_bpmn(_full_v2()))
    DI = "{" + bpmn._NS["bpmndi"] + "}"
    DC = "{" + bpmn._NS["dc"] + "}"
    boxes, expanded = {}, []
    for shp in root.iter(f"{DI}BPMNShape"):
        b = shp.find(f"{DC}Bounds")
        if b is None:
            continue
        keys = ("x", "y", "width", "height")
        boxes[shp.get("bpmnElement")] = tuple(float(b.get(k)) for k in keys)
        if shp.get("isExpanded") == "true":
            expanded.append(shp.get("bpmnElement"))
    # no two shapes share an exact top-left (the overlap bug)
    tops = [(round(x), round(y)) for (x, y, _w, _h) in boxes.values()]
    assert len(set(tops)) == len(tops), "shapes overlap exactly"
    # edges exist for the flows
    assert sum(1 for _ in root.iter(f"{DI}BPMNEdge")) > 0
    # loop/map sub-processes are expanded with children contained
    assert "refine" in expanded and "fanout" in expanded
    rx, ry, rw, rh = boxes["refine"]
    ax, ay, aw, ah = boxes["attempt"]
    assert rx <= ax and ry <= ay and ax + aw <= rx + rw and ay + ah <= ry + rh


def test_generic_task_maps_to_scripted_not_dropped():
    # A plain bpmn:task (what the editor palette draws by default) must round-trip as a
    # scripted step, so a user's freshly-drawn node is never silently lost on Save.
    xml = (
        '<?xml version="1.0"?><bpmn:definitions '
        'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">'
        '<bpmn:process id="p"><bpmn:task id="testaroo" name="testaroo"/></bpmn:process>'
        "</bpmn:definitions>"
    )
    doc = bpmn.bpmn_to_ir(xml)
    assert doc["steps"][0] == {"id": "testaroo", "uses": "testaroo"}


def test_start_and_end_events_emitted_and_ignored_on_read():
    # The diagram gets a start event -> root(s) and sink(s) -> an end event (so the entry
    # point is visible); reconstruction ignores them, so the round-trip is unaffected.
    doc = {"schema_version": "2", "name": "se", "steps": [{"id": "a", "uses": "x"}]}
    xml = bpmn.ir_to_bpmn(doc)
    assert "bpmn:startEvent" in xml and "bpmn:endEvent" in xml
    back = bpmn.bpmn_to_ir(xml)
    assert [s["id"] for s in back["steps"]] == ["a"]  # no synthetic start/end step
    assert "needs" not in back["steps"][0]  # the start->a flow is not a dependency


def test_branch_gateway_and_flows_are_labelled():
    # The gateway carries its condition and the arms are labelled then/else, so the decision
    # logic is visible on the canvas (labels are display-only; not read back).
    doc = {
        "schema_version": "2", "name": "b", "inputs": {"x": {"type": "boolean"}},
        "steps": [{"id": "g", "branch": {"when": "${{ inputs.x }}",
                                         "then": [{"id": "t", "uses": "o"}],
                                         "else": [{"id": "e", "uses": "o"}]}}],
    }
    xml = bpmn.ir_to_bpmn(doc)
    assert 'name="${{ inputs.x }}"' in xml  # gateway labelled with the condition
    assert 'name="true"' in xml and 'name="false"' in xml  # arm flows labelled
    # the else arm flow is the gateway's default flow (slash marker)
    assert 'default="flow_g.else"' in xml


def test_branch_continuation_flow_is_labelled_after():
    # A step that runs after a branch (needs the gateway) gets an "after" label on its
    # incoming flow, so the gateway's post-branch continuation reads as a continuation,
    # not a third decision outcome.
    doc = {
        "schema_version": "2", "name": "c", "inputs": {"x": {"type": "boolean"}},
        "steps": [
            {"id": "g", "branch": {"when": "${{ inputs.x }}", "then": [{"id": "t", "uses": "o"}]}},
            {"id": "after_step", "uses": "o", "needs": ["g"]},
        ],
    }
    xml = bpmn.ir_to_bpmn(doc)
    assert 'name="after"' in xml
