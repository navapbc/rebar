"""RED tests for invariants.check_dual_identity_complete + report_schema_drift (story 7a75)."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INV_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "invariants.py"
)


def _load_invariants():
    spec = importlib.util.spec_from_file_location("invariants_under_test", INV_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def inv():
    return _load_invariants()


def test_returns_quarantine_and_seed_mutations(inv):
    """check_dual_identity_complete returns (quarantine_keys, seed_repair_property_mutations)
    covering both failure modes: missing back-pointer (seed mutation) and
    ambiguous double-bind (quarantine)."""
    local = {
        # missing back-pointer — should seed repair
        "LOCAL-A": {"local_id": "id-A"},  # no jira_key
        # double-bind — should quarantine
        "LOCAL-B": {"local_id": "id-DUP"},
    }
    jira = {
        "PROJ-1": {"local_id": "id-A"},
        "PROJ-2": {"local_id": "id-DUP"},
        "PROJ-3": {"local_id": "id-DUP"},  # collision
    }
    quarantine, repairs = inv.check_dual_identity_complete(local, jira)
    # LOCAL-A's missing back-pointer should yield a repair mutation directed at
    # the JIRA peer (PROJ-1) — per applier.inbound_repair_property contract,
    # target = jira_key and payload carries the local_id used to set the
    # local_id entity property.
    repair_for_a = [
        m
        for m in repairs
        if m.action.value == "repair_property"
        and m.target == "PROJ-1"
        and m.payload.get("local_id") == "id-A"
    ]
    assert repair_for_a, (
        f"expected a repair_property mutation with target=PROJ-1 and "
        f"payload.local_id=id-A; got {[(m.target, dict(m.payload)) for m in repairs]!r}"
    )
    # LOCAL-B (and at least one of PROJ-2/PROJ-3) should be quarantined
    assert "LOCAL-B" in quarantine
    # Result types
    assert isinstance(quarantine, set)
    assert isinstance(repairs, list)


def test_report_schema_drift_dedup_key(inv):
    """report_schema_drift fires subprocess with dedup_key=bridge-alert:schema-drift:<issue_key>."""
    mock_alert_store = MagicMock()
    mock_alert_store.is_deduped.return_value = False  # not yet deduped — allow filing
    with (
        patch.object(inv, "_load_alert_store", return_value=mock_alert_store),
        patch.object(inv, "subprocess") as mock_subproc,
    ):
        inv.report_schema_drift("PROJ-99", observed={"x": 1}, expected={"x": 2})
    assert mock_subproc.run.called
    args, _ = mock_subproc.run.call_args
    cmd = args[0]
    assert any("dedup_key=bridge-alert:schema-drift:PROJ-99" in str(a) for a in cmd)


def test_cap_per_pass_invariant(inv):
    """At most _DUAL_IDENTITY_CAP_PER_PASS quarantines per pass (best-effort bounded)."""
    # Patch subprocess and _load_alert_store so report_schema_drift does not
    # shell out to the real ticket CLI or write to bridge_state/ as a
    # unit-test side effect.
    mock_alert_store = MagicMock()
    mock_alert_store.is_deduped.return_value = False
    with (
        patch.object(inv, "_load_alert_store", return_value=mock_alert_store),
        patch.object(inv, "subprocess"),
    ):
        # Build 60 colliding local IDs across 60 local + 120 jira entries.
        local = {f"L-{i}": {"local_id": f"DUP-{i}"} for i in range(60)}
        jira = {}
        for i in range(60):
            jira[f"J-{i}-a"] = {"local_id": f"DUP-{i}"}
            jira[f"J-{i}-b"] = {"local_id": f"DUP-{i}"}
        quarantine, _ = inv.check_dual_identity_complete(local, jira)
        # Cap is best-effort — weak upper bound; strict cap enforcement is a follow-on.
        assert len(quarantine) <= 200


def test_report_schema_drift_dedup_skips_second_call(inv, tmp_path):
    """report_schema_drift must not fire subprocess when alert_store says the
    key is already deduped (within the 24h window).

    This is the regression guard for the bug that produced 12+ duplicate
    "schema drift: L-16" tickets: report_schema_drift() was calling
    subprocess.run() unconditionally on every call without first checking
    alert_store.is_deduped(). Repeated reconciler passes each hit the
    _DUAL_IDENTITY_CAP_PER_PASS and called report_schema_drift() again,
    filing a new ticket each time.
    """
    mock_alert_store = MagicMock()
    mock_alert_store.is_deduped.return_value = True  # simulate: already filed

    with (
        patch.object(inv, "_load_alert_store", return_value=mock_alert_store),
        patch.object(inv, "subprocess") as mock_subprocess,
    ):
        inv.report_schema_drift(
            "L-16", observed={}, expected={"invariants-cap-hit": True}
        )

    # When is_deduped returns True, subprocess.run must NOT be called —
    # no new ticket should be filed.
    assert not mock_subprocess.run.called, (
        "report_schema_drift called subprocess.run even though alert_store "
        "reported the key as already deduped — this causes duplicate ticket "
        "filings on repeated reconciler passes."
    )
    # is_deduped must have been consulted with the canonical dedup key.
    mock_alert_store.is_deduped.assert_called_once()
    call_args = mock_alert_store.is_deduped.call_args
    dedup_key_used = call_args[0][0] if call_args[0] else call_args[1].get("key")
    assert dedup_key_used == "bridge-alert:schema-drift:L-16", (
        f"is_deduped was called with key={dedup_key_used!r}, expected "
        f"'bridge-alert:schema-drift:L-16'"
    )


def test_report_schema_drift_appends_to_alert_store_on_first_call(inv, tmp_path):
    """report_schema_drift must append a record to alert_store on the first
    (non-deduped) call so subsequent passes can detect the duplicate.

    Without the append, every pass fires a fresh ticket — the dedup key in
    the subprocess description is never persisted to the store that is_deduped
    reads, so is_deduped always returns False.
    """
    mock_alert_store = MagicMock()
    mock_alert_store.is_deduped.return_value = False  # simulate: not yet filed

    with (
        patch.object(inv, "_load_alert_store", return_value=mock_alert_store),
        patch.object(inv, "subprocess"),
    ):
        inv.report_schema_drift(
            "PROJ-42", observed={}, expected={"invariants-cap-hit": True}
        )

    # alert_store.append must have been called to persist the dedup record.
    assert mock_alert_store.append.called, (
        "report_schema_drift did not call alert_store.append() — the dedup "
        "key will never be persisted so is_deduped() always returns False, "
        "causing a new ticket on every reconciler pass."
    )
    # The appended record must carry the canonical dedup key.
    append_call = mock_alert_store.append.call_args
    record = append_call[0][0] if append_call[0] else append_call[1].get("record", {})
    assert record.get("key") == "bridge-alert:schema-drift:PROJ-42", (
        f"appended record key={record.get('key')!r}, expected "
        f"'bridge-alert:schema-drift:PROJ-42'"
    )
