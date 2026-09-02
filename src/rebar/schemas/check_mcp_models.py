"""Validate that ``src/rebar/_mcp_models.py`` hand-mirrored pydantic models stay in
sync with their canonical JSON Schemas in ``src/rebar/schemas/*.schema.json``.

Unlike ``gen_types``, which regenerates a derived file, this is a VALIDATOR: the
pydantic models are hand-written and cannot be auto-regenerated.  The module checks
for *missing declarations* — schema properties that no model field covers — and fails
the build when any are found.

Entry point::

    python -m rebar.schemas.check_mcp_models --check   # exit 1 if drift detected
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SCHEMA_DIR = Path(__file__).resolve().parent

# Explicit model → schema mappings, for the cases the naming convention cannot derive.
# Everything else is DISCOVERED (see `model_schemas`): a hand-maintained registry is how
# this gate came to cover 11 of 32 models while silently ignoring the rest (mirror F5).
MODEL_SCHEMAS: dict[str, str] = {
    "BridgeAccessCheckOut": "bridge_access_check",
    "BridgeControlOut": "bridge_control",
    "BridgeFsckOut": "bridge_fsck",
    "BridgeRunOut": "bridge_run",
    "BridgeStatusOut": "bridge_status",
    "FsckOut": "fsck",
    "SignResultOut": "sign_result",
    "VerifySignatureResultOut": "verify_signature_result",
    "GroundingInfoOut": "grounding_info",
    "PlanReviewStatusOut": "plan_review_status",
    "WorkflowRunOut": "workflow_run",
}

# Schema properties a model DELIBERATELY omits (permissive models serving multiple
# call shapes).  Only WorkflowRunOut needs omissions: it is a single extra="allow"
# model for both get_workflow_status and get_workflow_result; the six call-specific
# fields are intentionally undeclared.
PERMISSIVE_OMISSIONS: dict[str, set[str]] = {
    "WorkflowRunOut": {
        "steps",
        "outputs",
        "terminal_output",
        "terminal_step",
        "error",
        "truncated",
    },
}


#: Known-missing declarations, baselined so this gate is green at rest while any NEW
#: omission fails — the ratchet shape check_complexity_baseline.py and
#: check_mechanism_delta.py already use.
#:
#: These four models were invisible to the gate until its registry became derived, and
#: they are ALREADY DRIFTED: 41 canonical schema properties they do not declare. Closing
#: those gaps changes the MCP tools' published `outputSchema`, i.e. the wire contract
#: clients read, so it is deliberately NOT bundled into this gate change. Tracked in
#: ticket 3a02-66ea-9229-470c (lowly-lawrencium-ovenbird); delete each entry as it lands.
BASELINED_OMISSIONS: dict[str, set[str]] = {
    "CreateResultOut": {
        "title",
    },
    "GateResultOut": {
        "ac_items",
        "criteria_count",
        "file_impact",
        "keyword_count",
        "line_count",
    },
    "NextBatchOut": {
        "available_pool",
        "batch",
        "batch_size",
        "epic_title",
        "skipped_blocked",
        "skipped_blocked_story",
        "skipped_design_awaiting",
        "skipped_in_progress",
        "skipped_manual_awaiting",
        "skipped_needs_planning",
        "skipped_overlap",
        "tasks",
    },
    "TicketStateOut": {
        "attestations",
        "author",
        "bridge_alerts",
        "bridge_project",
        "close_class",
        "close_reason",
        "completion_expectation",
        "created_at",
        "creation_channel",
        "creation_channel_inferred",
        "detected_by",
        "env_id",
        "force_close_reason",
        "last_reopened_at",
        "preconditions_summary",
        "repos",
        "reverts",
        "source_author",
        "source_created_at",
        "source_env",
        "source_id",
        "updated_at",
        "verify_commands",
    },
}


def _snake(model_name: str) -> str:
    """``TicketStateOut`` -> ``ticket_state``; the convention every schema file follows."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", model_name.removesuffix("Out")).lower()


def model_schemas() -> dict[str, str]:
    """Every public ``*Out`` model paired with its canonical schema, DISCOVERED not listed.

    A model is covered when ``<snake_case>.schema.json`` exists; one with no schema has
    nothing to be checked against and is skipped. Explicit :data:`MODEL_SCHEMAS` entries
    win, so a name the convention cannot derive is still expressible.

    Deriving this is the point: the previous hand-list omitted 21 of 32 models, including
    both models carrying the ``PinStatus`` copies, and nothing reported the omission.
    """
    from rebar import _mcp_models

    found = dict(MODEL_SCHEMAS)
    for name in dir(_mcp_models):
        if not name.endswith("Out") or name.startswith("_") or name in found:
            continue
        schema_name = _snake(name)
        if (_SCHEMA_DIR / f"{schema_name}.schema.json").exists():
            found[name] = schema_name
    return found


def enum_mismatches(model_schema: dict, schema: dict) -> dict[str, dict[str, list[str]]]:
    """Properties whose allowed VALUES differ between the model and its canonical schema.

    The gate was previously a property-NAME set difference only, so a model and its schema
    could advertise different enums for the same property and nothing said so — which is
    exactly how the hand-copied ``PinStatus`` / ``TargetPinStatus`` / ``FileImpactScope``
    enums in the import-free ``_mcp_models`` leaf were free to drift.
    """
    out: dict[str, dict[str, list[str]]] = {}
    model_props = model_schema.get("properties", {})
    for prop, spec in schema.get("properties", {}).items():
        want = spec.get("enum")
        got = model_props.get(prop, {}).get("enum")
        if want is None or got is None:
            continue
        if sorted(map(str, want)) != sorted(map(str, got)):
            out[prop] = {"schema": sorted(map(str, want)), "model": sorted(map(str, got))}
    return out


def missing_declarations(
    model_fields: list[str] | set[str],
    schema_properties: list[str] | set[str],
    permissive: list[str] | set[str],
) -> list[str]:
    """Return schema properties not declared in the model and not explicitly omitted.

    A pure function: ``sorted(set(schema_properties) - set(model_fields) - set(permissive))``.
    """
    return sorted(set(schema_properties) - set(model_fields) - set(permissive))


def find_drift() -> dict[str, list[str]]:
    """Check every hand-mirrored model against its canonical JSON Schema.

    Returns a dict mapping model name → list of missing property names.  An empty
    dict means no drift was detected.  Models whose class resolved to ``None``
    (pydantic / mcp extra absent) are silently skipped.
    """
    from rebar import _mcp_models, schemas

    result: dict[str, list[str]] = {}
    for model_name, schema_name in model_schemas().items():
        cls = getattr(_mcp_models, model_name, None)
        if cls is None:
            continue
        schema = schemas.load(schema_name)
        schema_props = list(schema.get("properties", {}).keys())
        model_field_names = list(cls.model_fields.keys())
        permissive = PERMISSIVE_OMISSIONS.get(model_name, set())
        permissive = permissive | BASELINED_OMISSIONS.get(model_name, set())
        missing = missing_declarations(model_field_names, schema_props, permissive)
        if missing:
            result[model_name] = missing
        for prop, sides in enum_mismatches(cls.model_json_schema(), schema).items():
            result.setdefault(model_name, []).append(
                f"enum mismatch on {prop!r}: schema={sides['schema']} model={sides['model']}"
            )
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  ``--check``: exit 1 and print offenders on drift."""
    argv = list(sys.argv[1:] if argv is None else argv)
    drift = find_drift()
    if not drift:
        print("src/rebar/_mcp_models.py: MCP model schema drift gate OK — no missing declarations")
        return 0
    for model_name, missing in drift.items():
        schema_name = model_schemas()[model_name]
        print(
            f"DRIFT: {model_name} is missing declarations for: {missing}\n"
            f"  Add these fields to {model_name} in src/rebar/_mcp_models.py\n"
            f"  (canonical schema: src/rebar/schemas/{schema_name}.schema.json)",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
