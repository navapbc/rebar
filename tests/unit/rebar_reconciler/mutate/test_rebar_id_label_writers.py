"""Tests for the rebar-id label write authorization guard in applier.py.

Covers _audit_rebar_id_label_writes and its integration into apply():

  1. test_unauthorized_leaf_raises_rebar_id_label_write_error
     — direct call to _audit_rebar_id_label_writes with an unauthorized leaf
     and a rebar-id-* label create mutation (target='label') raises
     RebarIdLabelWriteError.

  2. test_authorized_leaves_pass_audit
     — inbound_clean_label (delete) and outbound_create (create) pass through
     _audit_rebar_id_label_writes without raising, even when target='label' and
     payload starts with 'rebar-id-'.

  3. test_apply_raises_for_unauthorized_rebar_id_label_mutation (behavioral)
     — apply() with inbound_update Mutation carrying a rebar-id-* label in
     payload raises RebarIdLabelWriteError after wiring.

  4. test_audit_ignores_non_rebar_id_label_mutations
     — mutations where target!='label' or payload doesn't start with 'rebar-id-'
     do NOT trigger the guard from an unauthorized leaf.

  5. test_warn_mode_logs_and_does_not_raise
     — REBAR_ID_GUARD_MODE=warn logs a WARNING instead of raising.

  6. test_guard_mode_precedence
     — env var REBAR_ID_GUARD_MODE takes precedence over config; default
     is 'raise'.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)
MUTATION_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"
)
ERRORS_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_errors.py"
)


# ---------------------------------------------------------------------------
# Module loaders
# ---------------------------------------------------------------------------


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_applier():
    """Load applier under the canonical 'applier' module name."""
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mut_mod():
    return _load(MUTATION_PATH, "rebar_reconciler_mutation_guard")


@pytest.fixture(scope="module")
def errors_mod():
    return _load(ERRORS_PATH, "rebar_reconciler_errors_guard")


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


# ---------------------------------------------------------------------------
# Simple mock objects for label-mutation structs
#
# _MockLabelMutation represents a single label-mutation event:
#   target  = 'label'          (the surface being mutated — always 'label')
#   payload = 'rebar-id-...'     (the label value string)
#   action  = 'create'|'update'|'delete'
# ---------------------------------------------------------------------------


class _MockLabelMutation:
    """Minimal label-mutation descriptor for direct audit tests."""

    def __init__(self, payload: str, action: str, target: str = "label"):
        self.target = target
        self.payload = payload
        self.action = action

    def __repr__(self) -> str:
        return (
            f"_MockLabelMutation(target={self.target!r}, "
            f"payload={self.payload!r}, action={self.action!r})"
        )


# ---------------------------------------------------------------------------
# Test 1 — unauthorized leaf raises RebarIdLabelWriteError (direct audit call)
# ---------------------------------------------------------------------------


def test_unauthorized_leaf_raises_rebar_id_label_write_error(applier, errors_mod):
    """_audit_rebar_id_label_writes with unauthorized leaf + rebar-id-* create mutation raises."""
    assert hasattr(applier, "_audit_rebar_id_label_writes"), (
        "_audit_rebar_id_label_writes not found in applier — implement the function"
    )
    # Use applier.RebarIdLabelWriteError to avoid importlib module-identity mismatch.
    assert hasattr(applier, "RebarIdLabelWriteError"), (
        "RebarIdLabelWriteError must be re-exported from applier"
    )
    mut = _MockLabelMutation(payload="rebar-id-abc123", action="create")
    with pytest.raises(applier.RebarIdLabelWriteError) as exc_info:
        applier._audit_rebar_id_label_writes("inbound_update", [mut])

    assert "inbound_update" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 2 — authorized leaves pass audit without raising
# ---------------------------------------------------------------------------


def test_authorized_leaves_pass_audit(applier):
    """inbound_clean_label (delete) and outbound_create (create) do not raise."""
    assert hasattr(applier, "_audit_rebar_id_label_writes"), (
        "_audit_rebar_id_label_writes not found in applier"
    )
    # inbound_clean_label: authorized for delete
    clean_label_mut = _MockLabelMutation(payload="rebar-id-xyz789", action="delete")
    # Should not raise
    applier._audit_rebar_id_label_writes("inbound_clean_label", [clean_label_mut])

    # outbound_create: authorized for create
    create_mut = _MockLabelMutation(payload="rebar-id-newid", action="create")
    # Should not raise
    applier._audit_rebar_id_label_writes("outbound_create", [create_mut])


# ---------------------------------------------------------------------------
# Test 3 — behavioral RED→GREEN: apply() raises through for unauthorized leaf
# ---------------------------------------------------------------------------


def _make_inbound_update_mutation_with_rebar_id_label(mut_mod):
    """Build an inbound update Mutation whose payload signals a rebar-id-* label write."""
    # The payload uses target='label' convention at the dict level so the
    # apply()-wired audit can detect the label write.
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target="JIRA-99",
        payload={"target": "label", "label": "rebar-id-test-ticket", "action": "create"},
        provenance={"source": "test"},
    )


def test_apply_raises_for_unauthorized_rebar_id_label_mutation(
    applier, mut_mod, errors_mod
):
    """BEHAVIORAL GREEN: apply() with inbound_update + rebar-id-* label mutation raises RebarIdLabelWriteError.

    After wiring _audit_rebar_id_label_writes into apply(), this call must raise.
    (Before wiring: this test fails — that is the RED state.)
    """
    mut = _make_inbound_update_mutation_with_rebar_id_label(mut_mod)
    # Use applier.RebarIdLabelWriteError to avoid importlib module-identity mismatch.
    with pytest.raises(applier.RebarIdLabelWriteError):
        applier.apply(mut, client=None)


# ---------------------------------------------------------------------------
# Test 4 — non-rebar-id label mutations from unauthorized leaves do not raise
# ---------------------------------------------------------------------------


def test_audit_ignores_non_rebar_id_label_mutations(applier):
    """Non-rebar-id-* payloads and non-label targets from unauthorized leaves do not raise."""
    assert hasattr(applier, "_audit_rebar_id_label_writes"), (
        "_audit_rebar_id_label_writes not found in applier"
    )
    # Payload does not start with 'rebar-id-' — should not raise
    non_dso_mut = _MockLabelMutation(payload="some-other-label", action="create")
    applier._audit_rebar_id_label_writes("inbound_update", [non_dso_mut])

    # target != 'label' — should not raise even if payload starts with 'rebar-id-'
    non_label_target_mut = _MockLabelMutation(
        target="JIRA-11",
        payload="rebar-id-something",
        action="create",
    )
    applier._audit_rebar_id_label_writes("inbound_update", [non_label_target_mut])


# ---------------------------------------------------------------------------
# Test 5 — warn mode: logs warning, does NOT raise
# ---------------------------------------------------------------------------


def test_warn_mode_logs_and_does_not_raise(applier, errors_mod, caplog):
    """REBAR_ID_GUARD_MODE=warn logs a WARNING instead of raising."""
    assert hasattr(applier, "_audit_rebar_id_label_writes"), (
        "_audit_rebar_id_label_writes not found in applier"
    )
    mut = _MockLabelMutation(payload="rebar-id-warn-test", action="create")
    with patch.dict(os.environ, {"REBAR_ID_GUARD_MODE": "warn"}):
        with caplog.at_level(logging.WARNING):
            # Should NOT raise in warn mode
            applier._audit_rebar_id_label_writes("inbound_update", [mut])

    # Check that a warning was logged with the required fields
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "Expected at least one WARNING log record in warn mode"
    log_text = " ".join(r.getMessage() for r in warning_records)
    assert "REBAR_ID_GUARD" in log_text, (
        f"Expected 'REBAR_ID_GUARD' in warning; got: {log_text!r}"
    )
    assert "inbound_update" in log_text, (
        f"Expected leaf name in warning; got: {log_text!r}"
    )
    assert "rebar-id-warn-test" in log_text, (
        f"Expected payload in warning; got: {log_text!r}"
    )


# ---------------------------------------------------------------------------
# Test 6 — guard mode precedence: env var > config > default raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_val,config_val,expected_raises",
    [
        # (a) env=warn + config=raise → warn behavior (env wins, no raise)
        ("warn", "raise", False),
        # (b) env=raise + config=warn → raise behavior (env wins)
        ("raise", "warn", True),
        # (c) env unset + config=warn → warn (config fallback)
        (None, "warn", False),
        # (d) env unset + config unset → raise (default)
        (None, None, True),
    ],
    ids=[
        "env_warn_beats_config_raise",
        "env_raise_beats_config_warn",
        "config_warn_when_env_unset",
        "default_raise_when_both_unset",
    ],
)
def test_guard_mode_precedence(
    applier, errors_mod, env_val, config_val, expected_raises
):
    """env var REBAR_ID_GUARD_MODE takes precedence over .rebar/config.conf key."""
    assert hasattr(applier, "_audit_rebar_id_label_writes"), (
        "_audit_rebar_id_label_writes not found in applier"
    )
    mut = _MockLabelMutation(payload="rebar-id-prec-test", action="create")

    # Save and restore REBAR_ID_GUARD_MODE cleanly
    original_env = os.environ.pop("REBAR_ID_GUARD_MODE", None)
    try:
        if env_val is not None:
            os.environ["REBAR_ID_GUARD_MODE"] = env_val
        # else: env var remains absent

        # Patch the internal config-reader if it exists
        _config_patcher = None
        if hasattr(applier, "_get_rebar_id_guard_mode_from_config"):
            _config_patcher = patch.object(
                applier,
                "_get_rebar_id_guard_mode_from_config",
                return_value=config_val,
            )
            _config_patcher.start()

        try:
            # Use applier.RebarIdLabelWriteError to avoid importlib module-identity mismatch.
            if expected_raises:
                with pytest.raises(applier.RebarIdLabelWriteError):
                    applier._audit_rebar_id_label_writes("inbound_update", [mut])
            else:
                applier._audit_rebar_id_label_writes("inbound_update", [mut])
        finally:
            if _config_patcher is not None:
                _config_patcher.stop()
    finally:
        # Restore original env state
        if original_env is not None:
            os.environ["REBAR_ID_GUARD_MODE"] = original_env
        else:
            os.environ.pop("REBAR_ID_GUARD_MODE", None)


# ---------------------------------------------------------------------------
# Per-leaf test matrix (9 tests, one per applier leaf)
#
# Canonical leaf names come from applier._LEAF_NAMES:
#   outbound_create, outbound_update, outbound_delete, outbound_probe,
#   outbound_conflict, inbound_create, inbound_update, inbound_clean_label,
#   inbound_repair_property
#
# Authorization:
#   AUTHORIZED (no raise):
#     - outbound_create  → create action permitted
#     - inbound_clean_label → delete action permitted
#   UNAUTHORIZED (raises RebarIdLabelWriteError):
#     - all other 7 leaves when they produce a rebar-id-* label mutation
#
# For inbound_repair_property: this leaf writes a PROPERTY FIELD (target='property'),
# NOT a label. The test asserts that a property-field mutation does NOT trigger the
# guard (target != 'label' so _is_rebar_id_label_write_mutation returns False).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-leaf test 1 — outbound_create (AUTHORIZED: create)
# ---------------------------------------------------------------------------


def test_outbound_create_may_write_rebar_id_label(applier):
    """outbound_create is the only authorized leaf for rebar-id label CREATE.

    Assertion: _audit_rebar_id_label_writes does NOT raise, and the mutation list
    passed in is exactly the one mutation (no implicit extra writes possible via
    the audit itself).
    """
    mut = _MockLabelMutation(payload="rebar-id-abc-outbound-create", action="create")
    # Should not raise — outbound_create is authorized for create
    applier._audit_rebar_id_label_writes("outbound_create", [mut])
    # AC amendment: confirm audit does not inject additional mutations
    mutations = [mut]
    applier._audit_rebar_id_label_writes("outbound_create", mutations)
    assert mutations == [mut], "audit must not mutate the input list"


# ---------------------------------------------------------------------------
# Per-leaf test 2 — inbound_clean_label (AUTHORIZED: delete)
# ---------------------------------------------------------------------------


def test_inbound_clean_label_may_delete_rebar_id_label(applier):
    """inbound_clean_label is the only authorized leaf for rebar-id label DELETE.

    Assertion: _audit_rebar_id_label_writes does NOT raise for a delete mutation,
    and the mutation list is unchanged (no implicit extra writes).
    """
    mut = _MockLabelMutation(payload="rebar-id-stale-label", action="delete")
    mutations = [mut]
    # Should not raise — inbound_clean_label is authorized for delete
    applier._audit_rebar_id_label_writes("inbound_clean_label", mutations)
    assert mutations == [mut], "audit must not mutate the input list"


# ---------------------------------------------------------------------------
# Per-leaf tests 3–9 — UNAUTHORIZED leaves (must raise RebarIdLabelWriteError)
# ---------------------------------------------------------------------------


def test_outbound_update_must_not_write_rebar_id_label(applier):
    """outbound_update is UNAUTHORIZED for rebar-id label writes.

    Passes a create mutation with a rebar-id-* payload through
    _audit_rebar_id_label_writes; expects RebarIdLabelWriteError.
    """
    mut = _MockLabelMutation(payload="rebar-id-should-not-write", action="create")
    with pytest.raises(applier.RebarIdLabelWriteError) as exc_info:
        applier._audit_rebar_id_label_writes("outbound_update", [mut])
    assert "outbound_update" in str(exc_info.value)


def test_outbound_delete_must_not_write_rebar_id_label(applier):
    """outbound_delete is UNAUTHORIZED for rebar-id label writes.

    Passes a create mutation with a rebar-id-* payload through
    _audit_rebar_id_label_writes; expects RebarIdLabelWriteError.
    """
    mut = _MockLabelMutation(payload="rebar-id-forbidden-write", action="create")
    with pytest.raises(applier.RebarIdLabelWriteError) as exc_info:
        applier._audit_rebar_id_label_writes("outbound_delete", [mut])
    assert "outbound_delete" in str(exc_info.value)


def test_outbound_probe_must_not_write_rebar_id_label(applier):
    """outbound_probe is UNAUTHORIZED for rebar-id label writes.

    Passes a create mutation with a rebar-id-* payload through
    _audit_rebar_id_label_writes; expects RebarIdLabelWriteError.
    """
    mut = _MockLabelMutation(payload="rebar-id-probe-forbidden", action="create")
    with pytest.raises(applier.RebarIdLabelWriteError) as exc_info:
        applier._audit_rebar_id_label_writes("outbound_probe", [mut])
    assert "outbound_probe" in str(exc_info.value)


def test_outbound_conflict_must_not_write_rebar_id_label(applier):
    """outbound_conflict is UNAUTHORIZED for rebar-id label writes.

    Passes a create mutation with a rebar-id-* payload through
    _audit_rebar_id_label_writes; expects RebarIdLabelWriteError.
    """
    mut = _MockLabelMutation(payload="rebar-id-conflict-forbidden", action="create")
    with pytest.raises(applier.RebarIdLabelWriteError) as exc_info:
        applier._audit_rebar_id_label_writes("outbound_conflict", [mut])
    assert "outbound_conflict" in str(exc_info.value)


def test_inbound_create_authorized_for_create_action(applier):
    """inbound_create is AUTHORIZED for rebar-id label create (dedup write-back).

    Passes a create mutation with a rebar-id-* payload through
    _audit_rebar_id_label_writes; expects NO error (authorized).
    """
    mut = _MockLabelMutation(payload="rebar-id-inbound-create-allowed", action="create")
    # Should NOT raise -- inbound_create is authorized for create action.
    applier._audit_rebar_id_label_writes("inbound_create", [mut])


def test_inbound_create_unauthorized_for_delete_action(applier):
    """inbound_create is UNAUTHORIZED for rebar-id label delete.

    Even though inbound_create is authorized for create, it must not
    be allowed to delete rebar-id labels.
    """
    mut = _MockLabelMutation(payload="rebar-id-inbound-create-forbidden", action="delete")
    with pytest.raises(applier.RebarIdLabelWriteError) as exc_info:
        applier._audit_rebar_id_label_writes("inbound_create", [mut])
    assert "inbound_create" in str(exc_info.value)


def test_inbound_update_must_not_write_rebar_id_label(applier):
    """inbound_update is UNAUTHORIZED for rebar-id label writes.

    Passes a create mutation with a rebar-id-* payload through
    _audit_rebar_id_label_writes; expects RebarIdLabelWriteError.
    """
    mut = _MockLabelMutation(payload="rebar-id-inbound-update-forbidden", action="create")
    with pytest.raises(applier.RebarIdLabelWriteError) as exc_info:
        applier._audit_rebar_id_label_writes("inbound_update", [mut])
    assert "inbound_update" in str(exc_info.value)


def test_inbound_repair_property_must_not_write_rebar_id_label(applier):
    """inbound_repair_property writes a PROPERTY FIELD, NOT a label.

    This leaf uses target='property' (not 'label'), so _is_rebar_id_label_write_mutation
    returns False and _audit_rebar_id_label_writes does NOT raise — this is the expected
    behavior (the leaf is neither authorized nor unauthorized for label writes; it simply
    never produces label-surface mutations).

    The test constructs a mutation with target='property' to reflect the actual
    behavior of this leaf: it calls set_issue_property(), which operates on entity
    properties, not labels. Even if payload starts with 'rebar-id-', the non-label
    target means the guard is not triggered.
    """
    # NOTE: inbound_repair_property writes to target='property', not target='label'.
    # The guard only fires when target='label' AND payload starts with 'rebar-id-'.
    # A property-field mutation with a rebar-id-* value is NOT a label write.
    property_mut = _MockLabelMutation(
        target="property",  # property surface, NOT label
        payload="rebar-id-local-ticket-id",
        action="create",
    )
    mutations = [property_mut]
    # Should NOT raise — property-field mutations do not trigger the label-write guard
    applier._audit_rebar_id_label_writes("inbound_repair_property", mutations)
    assert mutations == [property_mut], "audit must not mutate the input list"


# ---------------------------------------------------------------------------
# Per-action enforcement (Cluster B, item 4): authorized leaf + WRONG action
# must still raise.
# ---------------------------------------------------------------------------


def test_outbound_create_attempting_delete_action_raises(applier):
    """outbound_create is authorized for `create` ONLY; a `delete` on a rebar-id
    label from the same leaf is still UNAUTHORIZED and must raise.

    Per-action enforcement closes the gap where _AUTHORIZED_REBAR_ID_LABEL_ACTIONS
    was previously a dead constant — the leaf-name check alone would have let
    an authorized writer perform any action, defeating the per-action contract.
    """
    mut = _MockLabelMutation(payload="rebar-id-mismatched-action", action="delete")
    with pytest.raises(applier.RebarIdLabelWriteError) as exc_info:
        applier._audit_rebar_id_label_writes("outbound_create", [mut])
    assert "outbound_create" in str(exc_info.value)
    assert "delete" in str(exc_info.value)


def test_inbound_clean_label_attempting_create_action_raises(applier):
    """inbound_clean_label is authorized for `delete` ONLY; a `create` on a
    rebar-id label is UNAUTHORIZED and must raise."""
    mut = _MockLabelMutation(payload="rebar-id-wrong-action", action="create")
    with pytest.raises(applier.RebarIdLabelWriteError) as exc_info:
        applier._audit_rebar_id_label_writes("inbound_clean_label", [mut])
    assert "inbound_clean_label" in str(exc_info.value)
    assert "create" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Cluster B item 3: audit fires on the legacy batch dispatch path too.
# ---------------------------------------------------------------------------


def test_batch_audit_view_detects_rebar_id_label_in_fields(applier):
    """_BatchAuditView surfaces a rebar-id-* label hidden in batch_mutation['fields']['labels']
    so _audit_rebar_id_label_writes can enforce the contract on the legacy path.
    """
    batch_mut = {
        "action": "update",
        "key": "PROJ-1",
        "fields": {"labels": ["unrelated", "rebar-id-sneaky"]},
    }
    view = applier._BatchAuditView(batch_mut)
    assert view.target == "label"
    assert view.payload == "rebar-id-sneaky"
    assert view.action == "update"

    # And the audit must raise when handed this view under outbound_update.
    with pytest.raises(applier.RebarIdLabelWriteError):
        applier._audit_rebar_id_label_writes("outbound_update", [view])


def test_batch_audit_view_passes_clean_batch(applier):
    """A batch mutation with no rebar-id-* label in its fields must NOT raise the guard."""
    batch_mut = {
        "action": "update",
        "key": "PROJ-2",
        "fields": {"labels": ["regular", "another"], "title": "x"},
    }
    view = applier._BatchAuditView(batch_mut)
    # Synthesised target empty → not a label write
    assert view.target == ""
    # Should not raise — no rebar-id-* label in the batch
    applier._audit_rebar_id_label_writes("outbound_update", [view])
