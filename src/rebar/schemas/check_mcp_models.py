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

import sys

# Maps model class name → schema base name (without .schema.json suffix).
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
    for model_name, schema_name in MODEL_SCHEMAS.items():
        cls = getattr(_mcp_models, model_name, None)
        if cls is None:
            continue
        schema = schemas.load(schema_name)
        schema_props = list(schema.get("properties", {}).keys())
        model_field_names = list(cls.model_fields.keys())
        permissive = PERMISSIVE_OMISSIONS.get(model_name, set())
        missing = missing_declarations(model_field_names, schema_props, permissive)
        if missing:
            result[model_name] = missing
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  ``--check``: exit 1 and print offenders on drift."""
    argv = list(sys.argv[1:] if argv is None else argv)
    drift = find_drift()
    if not drift:
        print("src/rebar/_mcp_models.py: MCP model schema drift gate OK — no missing declarations")
        return 0
    for model_name, missing in drift.items():
        schema_name = MODEL_SCHEMAS[model_name]
        print(
            f"DRIFT: {model_name} is missing declarations for: {missing}\n"
            f"  Add these fields to {model_name} in src/rebar/_mcp_models.py\n"
            f"  (canonical schema: src/rebar/schemas/{schema_name}.schema.json)",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
