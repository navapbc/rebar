"""Check that duplicate local IDs for one Jira key produce one alert and one bug, while
clean snapshots do nothing. The invariant runs after fetch and before diff; legacy
dedup keys remain recognized.
"""

from __future__ import annotations

import importlib
import json
import types
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler import scratch_retention

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
INVARIANTS_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "invariants.py"


def _load_invariants() -> ModuleType:
    return importlib.import_module("rebar.reconciler.invariants")


@pytest.fixture(scope="module")
def invariants() -> ModuleType:
    """Load the invariants module, failing all tests if absent."""
    if not INVARIANTS_PATH.exists():
        pytest.fail(
            f"invariants.py not found at {INVARIANTS_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_invariants()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_alert_store_mock(deduped_keys: set[str] | None = None):
    """Return a mock alert_store module with controllable dedup behavior."""
    if deduped_keys is None:
        deduped_keys = set()

    mock_store = MagicMock()
    mock_store.is_deduped.side_effect = lambda key, repo_root: key in deduped_keys
    mock_store.append.return_value = None
    mock_store.patch_bug_filed.return_value = None
    return mock_store


def _make_subprocess_result(returncode: int = 0, stdout: str = "abcd-1234-5678-90ef"):
    """Return ticket-create output with a canonical 16-hex ID, so the parser sees the
    same shape as the CLI and can reject malformed IDs.
    """
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout + "\n"
    return result


# ---------------------------------------------------------------------------
# Test (a): clean snapshot → no writes or ticket-cli calls
# ---------------------------------------------------------------------------


def test_a_clean_snapshot(tmp_path, invariants):
    """A snapshot with no issues having multiple local_ids produces no side effects."""
    snapshot = {
        "PROJ-100": {"local_ids": ["local-abc"]},
        "PROJ-200": {"local_ids": []},
        "PROJ-300": {"local_ids": ["local-xyz"]},
        "PROJ-400": {},  # no local_ids key at all
    }

    mock_store = _make_alert_store_mock(deduped_keys=set())

    with patch.object(invariants, "_load_alert_store", return_value=mock_store):
        with patch("rebar.reconciler.invariants.subprocess.run") as mock_run:
            result = invariants.check_at_most_one_local_id(
                snapshot, repo_root=tmp_path, ticket_cli="/fake/rebar"
            )

    assert result == []
    mock_store.append.assert_not_called()
    mock_run.assert_not_called()
    mock_store.patch_bug_filed.assert_not_called()


# ---------------------------------------------------------------------------
# Test (b): single violation → one append, one ticket-cli call, one patch_bug_filed
# ---------------------------------------------------------------------------


def test_b_single_violation(tmp_path, invariants):
    """One issue with two local_ids triggers one append, one ticket-cli call, one patch."""
    snapshot = {
        "PROJ-100": {"local_ids": ["local-aaa", "local-bbb"]},
    }

    mock_store = _make_alert_store_mock(deduped_keys=set())
    # The parser requires a canonical 16-hex ID; a miss rolls back the alert.
    mock_proc_result = _make_subprocess_result(returncode=0, stdout="abcd-1234-5678-90ef")

    with patch.object(invariants, "_load_alert_store", return_value=mock_store):
        with patch(
            "rebar.reconciler.invariants.subprocess.run", return_value=mock_proc_result
        ) as mock_run:
            result = invariants.check_at_most_one_local_id(
                snapshot, repo_root=tmp_path, ticket_cli="/fake/rebar"
            )

    # One violation returned
    assert len(result) == 1
    assert result[0]["jira_key"] == "PROJ-100"
    assert result[0]["local_ids"] == ["local-aaa", "local-bbb"]

    # Exactly one append call
    mock_store.append.assert_called_once()
    append_record = mock_store.append.call_args[0][0]
    assert append_record["jira_key"] == "PROJ-100"
    assert "key" in append_record
    assert "timestamp_ns" in append_record

    # Exactly one subprocess.run call (ticket-cli). The rebar dispatcher is the
    # ticket CLI itself, so the bug-filing command is `rebar create bug ...`
    # (no `ticket` subcommand prefix as in the legacy ticket CLI).
    mock_run.assert_called_once()
    cli_args = mock_run.call_args[0][0]
    assert "create" in cli_args and "bug" in cli_args
    assert "create" in cli_args
    assert "bug" in cli_args

    # patch_bug_filed called with the returned bug id
    mock_store.patch_bug_filed.assert_called_once()
    patch_args = mock_store.patch_bug_filed.call_args[0]
    assert patch_args[1] == "abcd-1234-5678-90ef"


# ---------------------------------------------------------------------------
# Test (c): same key violated twice within 24h dedup window → one ticket-cli call
# ---------------------------------------------------------------------------


def test_c_dedup_window(tmp_path, invariants):
    """Second call for the same jira_key within 24h dedup window skips ticket-cli."""
    dedup_key = "bridge-alert:at-most-one:PROJ-100"
    snapshot = {
        "PROJ-100": {"local_ids": ["local-aaa", "local-bbb"]},
    }

    # Simulate the key already being deduped (i.e., within the 24h window)
    mock_store = _make_alert_store_mock(deduped_keys={dedup_key})

    with patch.object(invariants, "_load_alert_store", return_value=mock_store):
        with patch("rebar.reconciler.invariants.subprocess.run") as mock_run:
            result = invariants.check_at_most_one_local_id(
                snapshot, repo_root=tmp_path, ticket_cli="/fake/rebar"
            )

    # Deduped: no violation filed
    assert result == []
    mock_store.append.assert_not_called()
    mock_run.assert_not_called()
    mock_store.patch_bug_filed.assert_not_called()


def test_c_legacy_dedup_key_still_recognized(tmp_path, invariants):
    """A legacy at-most-one dedup key suppresses re-filing the same violation under the
    current key.
    """
    legacy_dedup_key = "at-most-one:PROJ-LEGACY"
    snapshot = {
        "PROJ-LEGACY": {"local_ids": ["legacy-a", "legacy-b"]},
    }

    # Store has the LEGACY key only — not the new prefixed format.
    mock_store = _make_alert_store_mock(deduped_keys={legacy_dedup_key})

    with patch.object(invariants, "_load_alert_store", return_value=mock_store):
        with patch("rebar.reconciler.invariants.subprocess.run") as mock_run:
            result = invariants.check_at_most_one_local_id(
                snapshot, repo_root=tmp_path, ticket_cli="/fake/rebar"
            )

    # Backward-compat path: legacy-keyed alert recognized → no re-file.
    assert result == []
    mock_store.append.assert_not_called()
    mock_run.assert_not_called()
    mock_store.patch_bug_filed.assert_not_called()


# ---------------------------------------------------------------------------
# Test (d): 7 violations with cap=5 → exactly 5 writes + 5 calls + 2 capped
# ---------------------------------------------------------------------------


def test_d_cap_at_5(tmp_path, invariants):
    """With 7 violations and cap=5, exactly 5 are processed and 2 are skipped."""
    snapshot = {
        f"PROJ-{100 + i * 100}": {"local_ids": [f"local-a{i}", f"local-b{i}"]} for i in range(7)
    }

    mock_store = _make_alert_store_mock(deduped_keys=set())
    # Canonical 16-hex format required (see _make_subprocess_result docstring).
    mock_proc_result = _make_subprocess_result(returncode=0, stdout="cafe-babe-dead-beef")

    with patch.object(invariants, "_load_alert_store", return_value=mock_store):
        with patch(
            "rebar.reconciler.invariants.subprocess.run", return_value=mock_proc_result
        ) as mock_run:
            result = invariants.check_at_most_one_local_id(
                snapshot, repo_root=tmp_path, ticket_cli="/fake/rebar"
            )

    # Exactly 5 violations filed (cap enforced)
    assert len(result) == 5

    # Exactly 5 append calls
    assert mock_store.append.call_count == 5

    # Exactly 5 ticket-cli subprocess calls
    assert mock_run.call_count == 5

    # Exactly 5 patch_bug_filed calls
    assert mock_store.patch_bug_filed.call_count == 5


# ---------------------------------------------------------------------------
# Paths for reconcile.py integration tests
# ---------------------------------------------------------------------------

RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "reconcile.py"


def _load_reconcile() -> ModuleType:
    """The shared reconcile module."""
    return importlib.import_module("rebar.reconciler.reconcile")


def _stubbed_load(reconcile_mod: ModuleType, stubs: dict[str, ModuleType]):
    """A ``reconcile._load`` replacement that serves ``stubs`` by sibling key."""
    real_load = reconcile_mod._load

    def _load(name: str, relpath: str) -> ModuleType:
        return stubs[name] if name in stubs else real_load(name, relpath)

    return _load


def _make_stub_fetcher(snapshot: dict, pass_id: str, tmp_path: Path) -> ModuleType:
    """Return a stub reconcile_fetcher that writes snapshot to tmp_path and returns the path."""
    snap_dir = scratch_retention.snapshots_dir(tmp_path)
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_file = snap_dir / f"{pass_id}.json"
    snap_file.write_text(json.dumps(snapshot))

    stub = types.ModuleType("reconcile_fetcher")

    def _fetch(pid, repo_root):
        # Write the snapshot for this pass
        out_dir = scratch_retention.snapshots_dir(repo_root)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{pid}.json"
        out.write_text(json.dumps(snapshot))
        return out

    stub.fetch_snapshot = _fetch
    return stub


def _make_stub_differ() -> ModuleType:
    stub = types.ModuleType("reconcile_differ")
    stub.compute_mutations = lambda prev, curr, **kwargs: []
    return stub


def _make_stub_applier(tmp_path: Path) -> ModuleType:
    stub = types.ModuleType("reconcile_applier")

    def _apply(mutations, pass_id, repo_root, **kwargs):
        # Bug 85a1: reconcile_once now passes binding_store=; accept and
        # ignore for stub purposes.
        manifest = scratch_retention.bridge_scratch_dir(repo_root) / "manifests" / f"{pass_id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"mutations": len(mutations)}))
        return manifest

    stub.apply = _apply
    return stub


def _make_stub_health() -> ModuleType:
    stub = types.ModuleType("reconcile_health")
    return stub


# ---------------------------------------------------------------------------
# Test (e): reconcile_once() calls check_at_most_one_local_id exactly once
# ---------------------------------------------------------------------------


def test_reconcile_once_invokes_invariant_after_fetch(tmp_path):
    """Run the invariant exactly once on the fetched snapshot before mutation planning."""
    pass_id = "inv-invoke-test"
    snapshot = {
        "JIRA-1": {"summary": "clean issue", "local_ids": ["local-abc"]},
    }

    # Build stubs
    stub_fetcher = _make_stub_fetcher(snapshot, pass_id, tmp_path)
    stub_differ = _make_stub_differ()
    stub_applier = _make_stub_applier(tmp_path)
    stub_health = _make_stub_health()

    # Build invariants stub with a trackable mock
    stub_invariants = types.ModuleType("reconcile_invariants")
    invariants_mock = MagicMock(return_value=[])
    stub_invariants.check_at_most_one_local_id = invariants_mock
    stub_invariants.check_dual_identity_complete = lambda prev, curr: (set(), [])
    stub_invariants.report_schema_drift = lambda *a, **kw: None

    stubs = {
        "reconcile_fetcher": stub_fetcher,
        "reconcile_differ": stub_differ,
        "reconcile_applier": stub_applier,
        "reconcile_health": stub_health,
        "reconcile_invariants": stub_invariants,
    }
    reconcile_mod = _load_reconcile()
    with patch.object(reconcile_mod, "_load", _stubbed_load(reconcile_mod, stubs)):
        reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)

    # check_at_most_one_local_id must have been called exactly once
    assert invariants_mock.call_count == 1, (
        f"Expected check_at_most_one_local_id to be called once, got {invariants_mock.call_count}"
    )

    # The first positional argument must be the post-fetch snapshot dict
    called_snapshot = invariants_mock.call_args[0][0]
    assert called_snapshot == snapshot, (
        f"check_at_most_one_local_id was called with wrong snapshot: "
        f"{called_snapshot!r} != {snapshot!r}"
    )

    # repo_root must be passed as keyword argument
    called_kwargs = invariants_mock.call_args[1]
    assert "repo_root" in called_kwargs, (
        "check_at_most_one_local_id must be called with repo_root kwarg"
    )


# ---------------------------------------------------------------------------
# Test (f): end-to-end — pass 2 violation → one BRIDGE_ALERT entry + one ticket-cli call
# ---------------------------------------------------------------------------


def test_end_to_end_second_write_produces_one_alert_one_bug(tmp_path):
    """A clean pass records no alert or ticket CLI call; a violating second pass
    records one alert and one ticket CLI call.
    """
    pass_id = "e2e-alert-test"

    snapshot_clean = {
        "JIRA-1": {"summary": "clean", "local_ids": ["local-only"]},
    }
    snapshot_violation = {
        "JIRA-1": {"summary": "clean", "local_ids": ["local-only"]},
        "JIRA-42": {"summary": "dup", "local_ids": ["id1", "id2"]},
    }

    def _run_pass_e2e(snapshot: dict, invariants_instance: ModuleType) -> None:
        """Run one reconciler pass with all collaborator stubs injected."""
        stubs = {
            "reconcile_fetcher": _make_stub_fetcher(snapshot, pass_id, tmp_path),
            "reconcile_differ": _make_stub_differ(),
            "reconcile_applier": _make_stub_applier(tmp_path),
            "reconcile_health": _make_stub_health(),
            "reconcile_invariants": invariants_instance,
        }
        reconcile_mod = _load_reconcile()
        with patch.object(reconcile_mod, "_load", _stubbed_load(reconcile_mod, stubs)):
            reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)

    # Load the real invariants module once, patch subprocess on it
    invariants_mod = _load_invariants()

    with patch.object(invariants_mod, "subprocess") as mock_subproc:
        mock_subproc.run.return_value = MagicMock(returncode=0, stdout="bug-e2e-001\n")

        # Pass 1: clean snapshot — no violations expected
        _run_pass_e2e(snapshot_clean, invariants_mod)
        calls_after_pass1 = mock_subproc.run.call_count

        # Pass 2: snapshot with a violation for JIRA-42
        _run_pass_e2e(snapshot_violation, invariants_mod)
        calls_after_pass2 = mock_subproc.run.call_count

    # Pass 1 must have produced zero ticket-cli calls
    assert calls_after_pass1 == 0, (
        f"Pass 1 (clean) should produce 0 ticket-cli calls, got {calls_after_pass1}"
    )

    # Pass 2 must have produced exactly one ticket-cli call
    assert calls_after_pass2 == 1, (
        f"Pass 2 (violation) should produce exactly 1 ticket-cli call, got {calls_after_pass2}"
    )

    # Count only at-most-one alerts; this fixture can also emit binding-commit
    # failures because it has no git-backed tickets tracker.
    alerts_dir = scratch_retention.alerts_dir(tmp_path)
    alert_files = list(alerts_dir.glob("*.jsonl")) if alerts_dir.is_dir() else []
    all_alert_lines = []
    for af in alert_files:
        for line in af.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                    all_alert_lines.append(rec)
                except json.JSONDecodeError:
                    pass  # malformed alert line — skip

    invariant_alerts = [
        r for r in all_alert_lines if r.get("jira_key") or "at-most-one" in r.get("key", "")
    ]
    assert len(invariant_alerts) == 1, (
        f"Expected exactly 1 at-most-one invariant BRIDGE_ALERT entry, "
        f"found {len(invariant_alerts)}: {invariant_alerts!r} "
        f"(all alerts: {all_alert_lines!r})"
    )
    assert invariant_alerts[0].get("jira_key") == "JIRA-42", (
        f"Alert should be for JIRA-42, got: {invariant_alerts[0]!r}"
    )
