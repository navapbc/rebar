"""Canonical rebar-id label write authorization across applier paths.

The audit rejects unauthorized leaf and action combinations, permits outbound create and
inbound label cleanup, and ignores non-label mutations. The bypass environment setting
takes precedence over typed config. Property writes remain outside label enforcement.
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
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
MUTATION_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"
ERRORS_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_errors.py"


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
# Minimal label-mutation shape for direct audit tests.
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


def test_apply_raises_for_unauthorized_rebar_id_label_mutation(applier, errors_mod):
    """Canonical inbound-update Mutation identity raises RebarIdLabelWriteError."""
    # Build from the canonical module because typed dispatch checks Mutation identity.
    canonical_mut = sys.modules.get("rebar_reconciler.mutation") or applier._load_mutation_module()
    mut = _make_inbound_update_mutation_with_rebar_id_label(canonical_mut)
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
    non_rebar_id_mut = _MockLabelMutation(payload="some-other-label", action="create")
    applier._audit_rebar_id_label_writes("inbound_update", [non_rebar_id_mut])

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
    """REBAR_UNSAFE_ID_GUARD_BYPASS=true logs a WARNING instead of raising."""
    assert hasattr(applier, "_audit_rebar_id_label_writes"), (
        "_audit_rebar_id_label_writes not found in applier"
    )
    mut = _MockLabelMutation(payload="rebar-id-warn-test", action="create")
    with patch.dict(os.environ, {"REBAR_UNSAFE_ID_GUARD_BYPASS": "true"}):
        with caplog.at_level(logging.WARNING):
            # Should NOT raise in warn mode
            applier._audit_rebar_id_label_writes("inbound_update", [mut])

    # Check that a warning was logged with the required fields
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "Expected at least one WARNING log record in warn mode"
    log_text = " ".join(r.getMessage() for r in warning_records)
    assert "REBAR_ID_GUARD" in log_text, f"Expected 'REBAR_ID_GUARD' in warning; got: {log_text!r}"
    assert "inbound_update" in log_text, f"Expected leaf name in warning; got: {log_text!r}"
    assert "rebar-id-warn-test" in log_text, f"Expected payload in warning; got: {log_text!r}"


# ---------------------------------------------------------------------------
# Test 6 — guard mode precedence: env var > config > default raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_val", "config_val", "expected_raises"),
    [
        # (a) env=true + config=false → bypass behavior (env wins, no raise)
        ("true", "raise", False),
        # (b) env=false + config=true → raise behavior (env wins)
        ("false", "warn", True),
        # (c) env unset + config=true → bypass (config fallback)
        (None, "warn", False),
        # (d) env unset + config unset → raise (default)
        (None, None, True),
    ],
    ids=[
        "env_true_beats_config_false",
        "env_false_beats_config_true",
        "config_true_when_env_unset",
        "default_raise_when_both_unset",
    ],
)
def test_guard_mode_precedence(
    applier, errors_mod, env_val, config_val, expected_raises, tmp_path, monkeypatch
):
    """REBAR_UNSAFE_ID_GUARD_BYPASS overrides the typed reconciler config key.

    The default mode raises when neither source is set.
    """
    import rebar.config as _cfg

    assert hasattr(applier, "_audit_rebar_id_label_writes"), (
        "_audit_rebar_id_label_writes not found in applier"
    )
    mut = _MockLabelMutation(payload="rebar-id-prec-test", action="create")

    # config layer: the typed `[reconciler] id_guard_bypass_unsafe` key in rebar.toml
    # under a tmp project root.
    monkeypatch.delenv("REBAR_UNSAFE_ID_GUARD_BYPASS", raising=False)
    if config_val is not None:
        bypass = "true" if config_val == "warn" else "false"
        (tmp_path / "rebar.toml").write_text(
            f"[reconciler]\nid_guard_bypass_unsafe = {bypass}\n", encoding="utf-8"
        )
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    # env layer: REBAR_UNSAFE_ID_GUARD_BYPASS, when set, must beat the file.
    if env_val is not None:
        monkeypatch.setenv("REBAR_UNSAFE_ID_GUARD_BYPASS", env_val)
    _cfg.reset_config_cache()

    # Use applier.RebarIdLabelWriteError to avoid importlib module-identity mismatch.
    if expected_raises:
        with pytest.raises(applier.RebarIdLabelWriteError):
            applier._audit_rebar_id_label_writes("inbound_update", [mut])
    else:
        applier._audit_rebar_id_label_writes("inbound_update", [mut])


# ---------------------------------------------------------------------------
# The leaf and action matrix permits outbound_create/create and
# inbound_clean_label/delete. All other label writes raise.
# inbound_repair_property targets a property and bypasses label enforcement.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-leaf test 1 — outbound_create (AUTHORIZED: create)
# ---------------------------------------------------------------------------


def test_outbound_create_may_write_rebar_id_label(applier):
    """Outbound create may add one rebar-id label without changing the mutation list."""
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
    """A property-target rebar-id value does not count as a label write."""
    # Property-target mutations do not invoke the rebar-id label guard.
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
    """Outbound create authorization does not permit a rebar-id label delete."""
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
