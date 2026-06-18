"""Golden freeze of the workflow DSL contract surface (epic a88f follow-up).

The DSL's central promise (schema.py) is that each version ships ONE *immutable*
JSON Schema at a stable ``$id``: ``workflow.v1.schema.json`` must NEVER change its
meaning — a breaking change is a NEW ``workflow.v2.schema.json`` + a migration
shim, never an edit to v1. Nothing enforced that. This test freezes:

  * the normalized content hash of ``workflow.v1.schema.json`` — an edit that
    changes its meaning fails here, forcing a v2 bump instead of silent drift;
  * the ``workflow_run`` schema's *contract invariants* (required floor +
    permissive ``additionalProperties``) — that schema is meant to gain optional
    fields, so we pin the invariants, not the bytes;
  * the packaged ``code_review`` example's version + step shape, and that it still
    parses + lints clean.

If you intentionally change v1, the failure message tells you to bump to v2.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import rebar.schemas as schemas
from rebar.llm.workflow import lint as L
from rebar.llm.workflow import schema as S

_SCHEMA_DIR = Path(schemas.__file__).resolve().parent

# Frozen normalized-content hash of workflow.v1.schema.json. DO NOT update this to
# match an edit to v1 — if a change is intentional and breaking, add
# workflow.v2.schema.json + a migrate shim and freeze THAT instead.
_V1_GOLDEN_SHA256 = "45ddb6634003f48043cd8127ec473996191783806b070a85c273a1ec20e361de"


def _normalized_hash(path: Path) -> str:
    obj = json.loads(path.read_text(encoding="utf-8"))
    norm = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def test_workflow_v1_schema_is_frozen() -> None:
    actual = _normalized_hash(_SCHEMA_DIR / "workflow.v1.schema.json")
    assert actual == _V1_GOLDEN_SHA256, (
        "workflow.v1.schema.json changed. v1 is IMMUTABLE — a breaking DSL change "
        "must ship as workflow.v2.schema.json + a migrate shim (bump "
        "CURRENT_SCHEMA_VERSION), not an edit to v1. If this change is genuinely "
        "non-semantic, update _V1_GOLDEN_SHA256 deliberately."
    )


def test_workflow_run_schema_contract_invariants() -> None:
    obj = json.loads((_SCHEMA_DIR / "workflow_run.schema.json").read_text(encoding="utf-8"))
    # The required floor the CLI + MCP reads both satisfy — never tighten without
    # updating both surfaces (this is the cross-interface contract).
    assert set(obj["required"]) == {"run_id", "status"}
    # Permissive by design so the evolving run record never breaks the contract.
    assert obj["additionalProperties"] is True


def test_code_review_example_shape_frozen() -> None:
    example = Path(S.__file__).resolve().parent / "examples" / "code_review.yaml"
    text = example.read_text(encoding="utf-8")
    doc = S.parse_workflow(text)
    assert doc["schema_version"] == "1"
    assert [s["id"] for s in doc["steps"]] == ["fetch", "review", "gate", "comment"]
    # The shipped example must always validate + lint clean (it is the demonstrator).
    assert [e for e in S.validate_document(doc) if not e.startswith("note:")] == []
    assert [str(f) for f in L.lint_workflow(text) if f.severity != "warning"] == []


def test_input_schema_registry_lists_v1() -> None:
    # The DSL version this build understands is wired into the schema registry.
    assert schemas.WORKFLOW_V1 in schemas.INPUT_SCHEMAS
    assert S.CURRENT_SCHEMA_VERSION == "1"
