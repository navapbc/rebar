"""Held-out oracle for ticket 8efe — edge/contract/E2E cases the implementer does
NOT see while implementing against the happy path.

Covers the durable drift gate (``rebar.schemas.check_mcp_models``), the
``VerifySignatureResultOut`` requiredness contract (AC2), and the end-to-end
client-facing MCP ``outputSchema`` (the real advertised shape).
"""

from __future__ import annotations

import asyncio
import sys

import pytest

pytest.importorskip("pydantic")

from rebar import schemas


# ── the recurring drift gate ──────────────────────────────────────────────────
def test_drift_gate_reports_no_drift_on_current_tree() -> None:
    """Every registered hand-mirrored model declares all of its schema's
    properties (minus documented permissive omissions) — the gate is green."""
    from rebar.schemas import check_mcp_models

    drift = check_mcp_models.find_drift()
    assert drift == {}, f"MCP models under-declare schema properties: {drift}"


def test_drift_gate_registers_the_hand_mirrored_models() -> None:
    # The five models that carried hand-sync "Mirrors ..." comments are the gate's
    # charter — bridge_fsck is the one that shipped the binding_drift defect.
    from rebar.schemas import check_mcp_models

    assert "BridgeFsckOut" in check_mcp_models.MODEL_SCHEMAS
    assert check_mcp_models.MODEL_SCHEMAS["BridgeFsckOut"] == "bridge_fsck"


def test_missing_declarations_has_teeth() -> None:
    """The pure detector flags an under-declared property and clears once it is
    declared or explicitly exempted — so the gate can never be a tautology."""
    from rebar.schemas import check_mcp_models

    assert check_mcp_models.missing_declarations({"a"}, {"a", "b"}, set()) == ["b"]
    assert check_mcp_models.missing_declarations({"a", "b"}, {"a", "b"}, set()) == []
    assert check_mcp_models.missing_declarations({"a"}, {"a", "b"}, {"b"}) == []


def test_drift_gate_cli_check_exits_zero() -> None:
    from rebar.schemas import check_mcp_models

    assert check_mcp_models.main(["--check"]) == 0


# ── AC2: VerifySignatureResultOut requiredness matches its schema ─────────────
def test_verify_signature_required_set_matches_schema() -> None:
    from rebar._mcp_models import VerifySignatureResultOut

    schema_required = set(schemas.load("verify_signature_result")["required"])
    model_required = {
        name for name, f in VerifySignatureResultOut.model_fields.items() if f.is_required()
    }
    assert model_required == schema_required


# ── end-to-end: the real advertised MCP outputSchema ──────────────────────────
def test_mcp_bridge_fsck_outputschema_advertises_exact_new_contract() -> None:
    from rebar.mcp_server import build_server

    tools = {t.name: t for t in asyncio.run(build_server().list_tools())}
    out = tools["bridge_fsck"].outputSchema or {}
    assert set(out.get("properties", {})) == {
        "unknown_event_types",
        "binding_drift",
        "store_integrity",
    }
    assert set(out.get("required", [])) == {
        "unknown_event_types",
        "binding_drift",
        "store_integrity",
    }

    item_schema = out["properties"]["store_integrity"]["items"]
    assert item_schema["type"] == "object"


# ── mirror F5: the registry is derived, and the check reads enum VALUES ──────


def test_the_registry_is_derived_not_hand_listed() -> None:
    """AC2. The hand-list covered 11 of 32 public *Out models; deriving it brings in every
    model that has a canonical schema, including the nine that were silently uncovered."""
    from rebar.schemas.check_mcp_models import MODEL_SCHEMAS, model_schemas

    derived = model_schemas()
    assert set(MODEL_SCHEMAS) <= set(derived), "explicit entries must still win"
    newly_covered = {
        "ClaimResultOut",
        "ClarityResultOut",
        "CreateResultOut",
        "DepsGraphOut",
        "GateResultOut",
        "NextBatchOut",
        "SearchResultOut",
        "TicketStateOut",
        "ValidateReportOut",
    }
    assert newly_covered <= set(derived), (
        f"the derived registry lost coverage: {sorted(newly_covered - set(derived))}"
    )


def test_a_model_with_no_schema_file_is_excluded_without_error() -> None:
    """AC4. A model with nothing to compare against is skipped, not reported as drift."""
    from rebar.schemas.check_mcp_models import _snake, model_schemas

    derived = model_schemas()
    for model_name in derived:
        assert _snake(model_name) or derived[model_name], model_name
    assert "BridgeAccessStepOut" not in derived, (
        "a model with no <snake_case>.schema.json must not be registered"
    )


def test_a_new_model_with_a_schema_is_picked_up_with_no_gate_edit(monkeypatch) -> None:
    """AC3. The whole point of deriving: coverage must not depend on remembering to edit."""
    import types as _types

    from rebar import _mcp_models
    from rebar.schemas import check_mcp_models

    class _Fake:
        pass

    stub = _types.SimpleNamespace(
        **{n: getattr(_mcp_models, n) for n in dir(_mcp_models) if n.endswith("Out")}
    )
    # `fsck` has a schema file, so a model named for it must be discovered automatically.
    stub.FsckOut = _Fake
    monkeypatch.setattr(check_mcp_models, "MODEL_SCHEMAS", {})
    monkeypatch.setitem(sys.modules, "rebar._mcp_models", stub)
    assert check_mcp_models.model_schemas().get("FsckOut") == "fsck"


def test_enum_values_are_compared_not_only_property_names() -> None:
    """AC1/AC6. The gate was a property-NAME set difference, so the hand-copied PinStatus,
    TargetPinStatus and FileImpactScope enums in the import-free _mcp_models leaf could
    drift in their VALUES with nothing to say so."""
    from rebar.schemas.check_mcp_models import enum_mismatches

    model = {"properties": {"scope": {"enum": ["paths", "none"]}}}
    schema = {"properties": {"scope": {"enum": ["undeclared", "paths", "none"]}}}
    found = enum_mismatches(model, schema)
    assert "scope" in found
    assert found["scope"]["schema"] == ["none", "paths", "undeclared"]
    assert found["scope"]["model"] == ["none", "paths"]

    agreeing = {"properties": {"scope": {"enum": ["a", "b"]}}}
    assert enum_mismatches(agreeing, {"properties": {"scope": {"enum": ["b", "a"]}}}) == {}


def test_a_property_without_an_enum_on_either_side_is_not_compared() -> None:
    """Only genuine enum pairs are compared; a free-form string is not 'drifted'."""
    from rebar.schemas.check_mcp_models import enum_mismatches

    assert (
        enum_mismatches(
            {"properties": {"s": {"type": "string"}}}, {"properties": {"s": {"enum": ["x"]}}}
        )
        == {}
    )


def test_the_gate_is_green_at_rest() -> None:
    """AC5. A more capable gate that fails on the committed tree is not shippable; the known
    gaps are baselined against their own ticket and anything NEW fails."""
    from rebar.schemas.check_mcp_models import find_drift

    assert find_drift() == {}


def test_the_baseline_names_its_tracking_ticket_and_is_not_open_ended() -> None:
    """A baseline with no ticket is just a suppression. This one must stay attributable."""
    from pathlib import Path as _P

    source = _P(check_mcp_models_path()).read_text(encoding="utf-8")
    assert "3a02-66ea-9229-470c" in source, "the baseline must name the ticket that drains it"


def check_mcp_models_path() -> str:
    import rebar.schemas.check_mcp_models as mod

    return mod.__file__
