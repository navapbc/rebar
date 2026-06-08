"""Contract tests for inbound_differ._OUTBOUND_TO_INBOUND_FIELD.

Asserts:
  1. Every outbound field name that differs from its inbound name appears in
     _OUTBOUND_TO_INBOUND_FIELD (at minimum: ``parent`` → ``parent_id``).
  2. _build_outbound_context applies the map — so outbound ``parent`` mutations
     are indexed under ``parent_id`` in the context dict, enabling the scalar
     suppression in compute_inbound_mutations to match correctly.

Per docs contract: any field whose outbound name differs from its inbound name
MUST appear in _OUTBOUND_TO_INBOUND_FIELD.  This test is the enforcement gate.
Canonical reference: 183fd51ac2; pending consolidation into _field_contract.py
per docs/designs/sync-hardening-proposal.md Item 3.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "inbound_differ.py"
)
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "outbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    key = f"_field_contract_{name}"
    if key in sys.modules:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def inbound_differ() -> ModuleType:
    return _load_module("inbound_differ", INBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    # Stub ADF to avoid heavy dependency in this contract test
    _ADF_KEY = "plugins.dso.scripts.dso_reconciler.adf"
    import types
    adf_stub = types.ModuleType(_ADF_KEY)
    adf_stub.adf_to_text = lambda x: str(x) if isinstance(x, str) else ""  # type: ignore[attr-defined]
    _prev = sys.modules.get(_ADF_KEY)
    sys.modules[_ADF_KEY] = adf_stub
    mod = _load_module("outbound_differ", OUTBOUND_DIFFER_PATH)
    if _prev is None:
        sys.modules.pop(_ADF_KEY, None)
    else:
        sys.modules[_ADF_KEY] = _prev
    return mod


# ---------------------------------------------------------------------------
# Test 1: parent → parent_id entry is present in the map
# ---------------------------------------------------------------------------


def test_outbound_to_inbound_field_contains_parent(inbound_differ: ModuleType) -> None:
    """_OUTBOUND_TO_INBOUND_FIELD must map 'parent' → 'parent_id'.

    This is the canonical asymmetric pair (outbound_differ emits 'parent' as
    the Jira REST field; inbound_differ emits 'parent_id' as the local field).
    Without this entry, bidirectional suppression in _build_outbound_context
    never fires for parent mutations and the two differs oscillate.
    """
    field_map = inbound_differ._OUTBOUND_TO_INBOUND_FIELD
    assert "parent" in field_map, (
        "_OUTBOUND_TO_INBOUND_FIELD must contain 'parent'. "
        "outbound_differ emits 'parent' (Jira REST); inbound_differ emits "
        "'parent_id' (local). The map is the canonicalization bridge. "
        "See 183fd51ac2 and docs/designs/sync-hardening-proposal.md Item 3."
    )
    assert field_map["parent"] == "parent_id", (
        f"_OUTBOUND_TO_INBOUND_FIELD['parent'] must be 'parent_id', "
        f"got {field_map['parent']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: _build_outbound_context applies the map for parent mutations
# ---------------------------------------------------------------------------


def test_build_outbound_context_canonicalizes_parent(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """_build_outbound_context must index outbound 'parent' mutations under
    the inbound name 'parent_id' so scalar-field suppression can match.

    Regression guard for bug 8b25: before the fix, the field set in the
    context carried 'parent' (outbound name); the suppression check keyed on
    'parent_id' (inbound name); they never matched → perpetual oscillation.
    """
    # Construct a minimal OutboundMutation with a parent field
    om = outbound_differ.OutboundMutation(
        local_id="probe-ticket-1",
        jira_key="DIG-5999",
        action="update",
        fields={"parent": "DIG-100"},  # outbound differ emits 'parent' key
        labels=[],
        comments=[],
    )

    ctx = inbound_differ._build_outbound_context([om])

    assert "DIG-5999" in ctx, (
        "_build_outbound_context must index the mutation by jira_key='DIG-5999'"
    )
    entry = ctx["DIG-5999"]
    assert "parent_id" in entry["fields"], (
        "After canonicalization via _OUTBOUND_TO_INBOUND_FIELD, the outbound "
        "field 'parent' must appear as 'parent_id' in the context's field set. "
        "Without this, the scalar suppression in compute_inbound_mutations "
        "never fires for parent mutations (bug 8b25 regression)."
    )
    assert "parent" not in entry["fields"], (
        "The raw outbound name 'parent' must NOT remain in the field set after "
        "canonicalization — only the inbound name 'parent_id' should be present."
    )


# ---------------------------------------------------------------------------
# Test 3: summary → title entry (bug 0702-3b6d-c1db-4ed3)
# ---------------------------------------------------------------------------


def test_outbound_to_inbound_field_contains_summary(
    inbound_differ: ModuleType,
) -> None:
    """_OUTBOUND_TO_INBOUND_FIELD must map 'summary' → 'title'.

    outbound_differ emits the title change under the Jira REST field name
    'summary'; inbound_differ emits it under the local field name 'title'.
    Bug 0702: this entry was missing, so an outbound title push did not
    suppress the inbound re-emission of the stale Jira title — the two differs
    oscillated on the title for bound-but-absent (out-of-window) keys.
    """
    field_map = inbound_differ._OUTBOUND_TO_INBOUND_FIELD
    assert field_map.get("summary") == "title", (
        "_OUTBOUND_TO_INBOUND_FIELD['summary'] must be 'title' so the scalar "
        "suppression matches the outbound 'summary' push against the inbound "
        f"'title' emission. Got {field_map.get('summary')!r}."
    )


def test_build_outbound_context_canonicalizes_summary(
    inbound_differ: ModuleType, outbound_differ: ModuleType
) -> None:
    """_build_outbound_context must index outbound 'summary' mutations under the
    inbound name 'title' so scalar-field suppression can match (bug 0702)."""
    om = outbound_differ.OutboundMutation(
        local_id="probe-ticket-2",
        jira_key="DIG-6001",
        action="update",
        fields={"summary": "New title"},
        labels=[],
        comments=[],
    )
    ctx = inbound_differ._build_outbound_context([om])
    entry = ctx["DIG-6001"]
    assert "title" in entry["fields"], (
        "After canonicalization, outbound 'summary' must appear as 'title' in "
        "the context's field set (bug 0702)."
    )
    assert "summary" not in entry["fields"], (
        "The raw outbound name 'summary' must NOT remain after canonicalization."
    )
