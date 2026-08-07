"""Held-out oracle for ticket 8efe — edge/contract/E2E cases the implementer does
NOT see while implementing against the happy path.

Covers the durable drift gate (``rebar.schemas.check_mcp_models``), the
``VerifySignatureResultOut`` requiredness contract (AC2), and the end-to-end
client-facing MCP ``outputSchema`` (the real advertised shape).
"""

from __future__ import annotations

import asyncio

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
def test_mcp_bridge_fsck_outputschema_advertises_binding_drift() -> None:
    from rebar.mcp_server import build_server

    tools = {t.name: t for t in asyncio.run(build_server().list_tools())}
    out = tools["bridge_fsck"].outputSchema or {}
    assert "binding_drift" in out.get("properties", {}), (
        "the client-facing MCP outputSchema for bridge_fsck still under-declares "
        "binding_drift, a field the tool always returns"
    )
