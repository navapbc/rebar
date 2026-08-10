"""Happy-path oracle for ticket 8efe — the MCP hand-mirrored output models must
not under-declare a schema property their tool always returns.

The shipped defect: ``BridgeFsckOut`` declared three properties while
``bridge_fsck.schema.json`` has four — ``binding_drift`` (emitted unconditionally
by the fsck) was missing from the advertised MCP ``outputSchema``. This is the
minimal specification of the fix.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from rebar._mcp_models import BridgeFsckOut


def test_bridge_fsck_out_declares_the_new_top_level_contract() -> None:
    assert BridgeFsckOut is not None
    expected_fields = {
        "unknown_event_types",
        "binding_drift",
        "store_integrity",
    }
    assert set(BridgeFsckOut.model_fields) == expected_fields
    assert set(BridgeFsckOut.model_json_schema()["required"]) == expected_fields
