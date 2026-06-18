"""Unit tests for the workflow DSL parser + schema (WS-B1 / WS-B3).

Pure, I/O-free: parse strings, validate against the version-pinned JSON Schema,
and exercise the hardened YAML loader (anchors/merge-keys/1.1-bools rejected).
No store, no network.
"""

from __future__ import annotations

import pytest

from rebar import schemas
from rebar.llm.errors import (
    WorkflowParseError,
    WorkflowVersionError,
)
from rebar.llm.workflow import schema as wf

VALID = """\
schema_version: "1"
name: code_review
description: Review a ticket's changed code.
inputs:
  ticket_id:
    type: string
    required: true
steps:
  - id: fetch
    uses: fetch_ticket
    with:
      ticket_id: ${{ inputs.ticket_id }}
  - id: review
    prompt: code_quality
    needs: [fetch]
    with:
      diff: ${{ steps.fetch.outputs.diff }}
    output_schema: review_result
    mode: findings
  - id: gate
    uses: gate
    needs: [review]
    with:
      findings: ${{ steps.review.outputs.findings }}
"""


# ── parsing ───────────────────────────────────────────────────────────────────


def test_parse_valid_returns_dict() -> None:
    doc = wf.parse_workflow(VALID)
    assert doc["schema_version"] == "1"
    assert doc["name"] == "code_review"
    assert [s["id"] for s in doc["steps"]] == ["fetch", "review", "gate"]


def test_parse_rejects_non_mapping_top_level() -> None:
    with pytest.raises(WorkflowParseError, match="mapping at the top level"):
        wf.parse_workflow("- just\n- a\n- list\n")


def test_parse_rejects_empty() -> None:
    with pytest.raises(WorkflowParseError, match="empty"):
        wf.parse_workflow("\n\n")


def test_parse_rejects_over_byte_cap() -> None:
    big = "name: x\n" + ("# pad padding padding\n" * 20000)
    assert len(big.encode()) > wf.MAX_WORKFLOW_BYTES
    with pytest.raises(WorkflowParseError, match="over the .* cap"):
        wf.parse_workflow(big)


def test_parse_rejects_anchors_and_aliases() -> None:
    text = """\
schema_version: "1"
name: x
base: &b {a: 1}
steps:
  - id: s
    uses: noop
    with: *b
"""
    with pytest.raises(WorkflowParseError, match="anchors/aliases"):
        wf.parse_workflow(text)


def test_parse_rejects_merge_keys() -> None:
    # A merge with an inline mapping (no anchor) — still rejected.
    text = """\
schema_version: "1"
name: x
steps:
  - id: s
    uses: noop
    with:
      <<: {a: 1}
      b: 2
"""
    with pytest.raises(WorkflowParseError, match="merge keys"):
        wf.parse_workflow(text)


def test_parse_rejects_multiple_documents() -> None:
    with pytest.raises(WorkflowParseError):
        wf.parse_workflow('schema_version: "1"\nname: a\n---\nname: b\n')


def test_yaml_1_1_booleans_stay_strings() -> None:
    # In YAML 1.1, on/off/yes/no are booleans; the hardened loader (1.2 Core) keeps
    # them as plain strings, so a value like `on` is not silently flipped to True.
    doc = wf.parse_workflow(
        'schema_version: "1"\nname: x\nsteps:\n  - id: s\n    uses: u\n'
        "    with:\n      a: on\n      b: no\n      c: yes\n      d: true\n"
    )
    w = doc["steps"][0]["with"]
    assert w["a"] == "on"
    assert w["b"] == "no"
    assert w["c"] == "yes"
    assert w["d"] is True  # genuine 1.2-Core boolean


def test_parse_error_carries_line() -> None:
    with pytest.raises(WorkflowParseError) as ei:
        wf.parse_workflow("name: x\n  bad: : indent\n")
    assert ei.value.source == "<workflow>"


# ── schema validation ─────────────────────────────────────────────────────────


def test_validate_valid_is_empty() -> None:
    pytest.importorskip("jsonschema")
    assert wf.validate_document(wf.parse_workflow(VALID)) == []


def test_validate_collects_all_errors_in_one_pass() -> None:
    pytest.importorskip("jsonschema")
    bad = """\
schema_version: "1"
name: "Has Spaces And Caps"
steps:
  - id: s1
    uses: ok
    prompt: also_set
  - id: s2
    unknownkey: 1
    uses: ok
"""
    errors = wf.validate_document(wf.parse_workflow(bad))
    # name pattern, oneOf (uses+prompt), and additionalProperties should all fire.
    assert len(errors) >= 2
    blob = "\n".join(errors)
    assert "name" in blob


def test_validate_requires_at_least_one_step() -> None:
    pytest.importorskip("jsonschema")
    errors = wf.validate_document({"schema_version": "1", "name": "x", "steps": []})
    assert any("steps" in e for e in errors)


def test_schema_is_immutable_additional_properties_false() -> None:
    s = schemas.load(schemas.WORKFLOW_V1)
    assert s["additionalProperties"] is False
    assert s["$defs"]["step"]["additionalProperties"] is False
    assert s["properties"]["schema_version"]["const"] == "1"


# ── version resolution ────────────────────────────────────────────────────────


def test_declared_version_requires_string() -> None:
    with pytest.raises(WorkflowParseError, match="must be a quoted string"):
        wf.declared_version({"schema_version": 1, "name": "x", "steps": []})


def test_newer_version_is_upgrade_error() -> None:
    with pytest.raises(WorkflowVersionError, match="upgrade rebar"):
        wf.schema_name_for_version("999")


def test_unknown_version_is_error() -> None:
    with pytest.raises(WorkflowVersionError, match="unknown"):
        wf.schema_name_for_version("banana")


def test_current_version_resolves_to_v1_schema() -> None:
    assert wf.schema_name_for_version("1") == "workflow.v1"


# ── classification + serialization ────────────────────────────────────────────


def test_step_kind_classification() -> None:
    assert wf.step_kind({"id": "a", "uses": "x"}) == "scripted"
    assert wf.step_kind({"id": "b", "prompt": "p"}) == "agent"
    assert wf.step_kind({"id": "c", "type": "agent", "uses": "x"}) == "agent"


def test_dump_round_trips_through_parse() -> None:
    doc = wf.parse_workflow(VALID)
    again = wf.parse_workflow(wf.dump_workflow(doc))
    assert again == doc


def test_dump_is_deterministic_key_order() -> None:
    doc = wf.parse_workflow(VALID)
    out = wf.dump_workflow(doc)
    # schema_version is emitted before name before steps regardless of input order.
    assert out.index("schema_version") < out.index("name") < out.index("steps")


def test_content_hash_is_stable_and_order_independent() -> None:
    a = {"schema_version": "1", "name": "x", "steps": [{"id": "s", "uses": "u"}]}
    b = {"name": "x", "steps": [{"uses": "u", "id": "s"}], "schema_version": "1"}
    assert wf.content_hash(a) == wf.content_hash(b)
    assert len(wf.content_hash(a)) == 64
