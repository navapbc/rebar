"""Tests for applier.inbound_repair_property — story 7a75-53f5 / task 44e6-4916.

Covers DD-3:
  (inbound, repair_property) failure → applier removes the orphan
  ``dso-id-<local_id>`` label AND emits a follow-on schema-drift signal in
  the SAME pass; fault-injection asserts both side effects (sc-7).

Import-direction guarantee (F6): applier.py MUST NOT import invariants —
schema-drift is communicated via a 'follow_on' payload that reconcile.py
routes to invariants in the next iteration.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def _mutation(target: str = "DIG-42", local_id: str = "abc-123"):
    """Build a minimal mutation stub exposing .target and .payload."""
    return types.SimpleNamespace(
        target=target,
        payload={"local_id": local_id},
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path(applier):
    """set_issue_property called once; no label removal; no schema-drift signal."""
    client = MagicMock()
    mutation = _mutation()

    result = applier.inbound_repair_property(mutation, client)

    assert result["status"] == "ok"
    assert result["key"] == "DIG-42"
    # No schema-drift signal on success
    assert result.get("follow_on") is None
    # set_issue_property called exactly once with expected args
    client.set_issue_property.assert_called_once_with("DIG-42", "dso_local_id", "abc-123")
    # No label removal on success
    client.remove_label.assert_not_called()


# ---------------------------------------------------------------------------
# Failure: side effects
# ---------------------------------------------------------------------------


def test_failure_cleans_label_and_signals_drift(applier):
    """set_issue_property raises → remove_label called AND follow_on signal emitted."""
    client = MagicMock()
    client.set_issue_property.side_effect = RuntimeError("simulated property write failure")
    mutation = _mutation(target="DIG-99", local_id="local-99")

    result = applier.inbound_repair_property(mutation, client)

    # Outcome dict shape
    assert result["status"] == "repair_property_failed"
    assert result["key"] == "DIG-99"

    # Label cleanup attempted exactly once with the correct format
    client.remove_label.assert_called_once_with("DIG-99", "dso-id-local-99")

    # Follow-on schema-drift signal present at top level
    follow_on = result["follow_on"]
    assert follow_on is not None
    assert follow_on["kind"] == "schema_drift_signal"
    assert follow_on["issue_key"] == "DIG-99"
    assert "repair_property_failed" in follow_on["reason"]
    assert "simulated property write failure" in follow_on["reason"]
    # label_remove succeeded → no error recorded
    assert follow_on["label_remove_error"] is None


# ---------------------------------------------------------------------------
# Failure: resilience when remove_label itself raises
# ---------------------------------------------------------------------------


def test_failure_resilient_to_label_remove_error(applier):
    """remove_label raising must NOT prevent the follow-on schema-drift signal."""
    client = MagicMock()
    client.set_issue_property.side_effect = RuntimeError("primary failure")
    client.remove_label.side_effect = RuntimeError("label removal failure")
    mutation = _mutation(target="DIG-7", local_id="loc-7")

    # Must not raise — the function must swallow remove_label errors
    result = applier.inbound_repair_property(mutation, client)

    assert result["status"] == "repair_property_failed"
    assert result["key"] == "DIG-7"

    # remove_label was still attempted
    client.remove_label.assert_called_once_with("DIG-7", "dso-id-loc-7")

    # Follow-on signal still emitted, with the label_remove_error captured
    follow_on = result["follow_on"]
    assert follow_on is not None
    assert follow_on["kind"] == "schema_drift_signal"
    assert follow_on["issue_key"] == "DIG-7"
    assert "label removal failure" in (follow_on["label_remove_error"] or "")


# ---------------------------------------------------------------------------
# Import-direction guarantee (F6): applier.py must not import invariants
# ---------------------------------------------------------------------------


def test_applier_does_not_import_invariants():
    """Applier source must not import invariants — preserves upstream contract."""
    source = APPLIER_PATH.read_text()
    for line in source.splitlines():
        stripped = line.lstrip()
        # Allow comments referencing 'invariants'
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("from .invariants"), (
            f"applier.py must not import from invariants: {line!r}"
        )
        assert not stripped.startswith("from invariants"), (
            f"applier.py must not import from invariants: {line!r}"
        )
        assert not stripped.startswith("import invariants"), (
            f"applier.py must not import invariants: {line!r}"
        )
