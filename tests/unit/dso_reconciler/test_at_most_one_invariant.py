"""Tests for invariants.check_at_most_one_dso_local_id().

Tests cover:
  - test_a_clean_snapshot: no violations → no writes or ticket-cli calls
  - test_b_single_violation: one violation → one append + one ticket-cli call +
    patch_bug_filed called with returned bug id
  - test_c_dedup_window: same key violated twice within 24h → exactly one ticket-cli call
  - test_d_cap_at_5: 7 violations with cap=5 → exactly 5 writes + 5 calls + 2 skipped
  - test_reconcile_once_invokes_invariant_after_fetch: reconcile_once() calls
    check_at_most_one_dso_local_id exactly once with the post-fetch snapshot
  - test_end_to_end_second_write_produces_one_alert_one_bug: two passes; pass 2 has
    a violation → exactly one BRIDGE_ALERT file entry + one ticket-cli call
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
INVARIANTS_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "invariants.py"
)


def _load_invariants() -> ModuleType:
    spec = importlib.util.spec_from_file_location("invariants", INVARIANTS_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["invariants"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


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


def _make_subprocess_result(
    returncode: int = 0, stdout: str = "abcd-1234-5678-90ef"
):
    """Build a mock subprocess.CompletedProcess result for ticket-create.sh.

    The default stdout uses a canonical 16-hex dso ticket ID
    (four groups of four lowercase hex digits) so that
    invariants._extract_ticket_id's regex matches the returned value.
    Tests that override stdout MUST pass a canonical-format ID for the
    same reason — non-canonical strings cause _extract_ticket_id to
    return the empty string, which the production code treats as
    bug-filing failure and rolls the alert back.
    """
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout + "\n"
    return result


# ---------------------------------------------------------------------------
# Test (a): clean snapshot → no writes or ticket-cli calls
# ---------------------------------------------------------------------------


def test_a_clean_snapshot(tmp_path, invariants):
    """A snapshot with no issues having multiple dso_local_ids produces no side effects."""
    snapshot = {
        "PROJ-100": {"dso_local_ids": ["local-abc"]},
        "PROJ-200": {"dso_local_ids": []},
        "PROJ-300": {"dso_local_ids": ["local-xyz"]},
        "PROJ-400": {},  # no dso_local_ids key at all
    }

    mock_store = _make_alert_store_mock(deduped_keys=set())

    with patch.object(invariants, "_load_alert_store", return_value=mock_store):
        with patch("invariants.subprocess.run") as mock_run:
            result = invariants.check_at_most_one_dso_local_id(
                snapshot, repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    assert result == []
    mock_store.append.assert_not_called()
    mock_run.assert_not_called()
    mock_store.patch_bug_filed.assert_not_called()


# ---------------------------------------------------------------------------
# Test (b): single violation → one append, one ticket-cli call, one patch_bug_filed
# ---------------------------------------------------------------------------


def test_b_single_violation(tmp_path, invariants):
    """One issue with two dso_local_ids triggers one append, one ticket-cli call, one patch."""
    snapshot = {
        "PROJ-100": {"dso_local_ids": ["local-aaa", "local-bbb"]},
    }

    mock_store = _make_alert_store_mock(deduped_keys=set())
    # Canonical 16-hex format required: invariants._extract_ticket_id uses
    # the regex \b[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}\b and
    # returns "" on a miss — at which point production rolls the alert back
    # and the assertions on append/run/patch_bug_filed below all fail.
    mock_proc_result = _make_subprocess_result(
        returncode=0, stdout="abcd-1234-5678-90ef"
    )

    with patch.object(invariants, "_load_alert_store", return_value=mock_store):
        with patch("invariants.subprocess.run", return_value=mock_proc_result) as mock_run:
            result = invariants.check_at_most_one_dso_local_id(
                snapshot, repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    # One violation returned
    assert len(result) == 1
    assert result[0]["jira_key"] == "PROJ-100"
    assert result[0]["dso_local_ids"] == ["local-aaa", "local-bbb"]

    # Exactly one append call
    mock_store.append.assert_called_once()
    append_record = mock_store.append.call_args[0][0]
    assert append_record["jira_key"] == "PROJ-100"
    assert "key" in append_record
    assert "timestamp_ns" in append_record

    # Exactly one subprocess.run call (ticket-cli). The rebar dispatcher is the
    # ticket CLI itself, so the bug-filing command is `rebar create bug ...`
    # (no `ticket` subcommand prefix as in the legacy dso shim).
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
        "PROJ-100": {"dso_local_ids": ["local-aaa", "local-bbb"]},
    }

    # Simulate the key already being deduped (i.e., within the 24h window)
    mock_store = _make_alert_store_mock(deduped_keys={dedup_key})

    with patch.object(invariants, "_load_alert_store", return_value=mock_store):
        with patch("invariants.subprocess.run") as mock_run:
            result = invariants.check_at_most_one_dso_local_id(
                snapshot, repo_root=tmp_path, ticket_cli="/fake/dso"
            )

    # Deduped: no violation filed
    assert result == []
    mock_store.append.assert_not_called()
    mock_run.assert_not_called()
    mock_store.patch_bug_filed.assert_not_called()


def test_c_legacy_dedup_key_still_recognized(tmp_path, invariants):
    """An alert filed under the legacy 'at-most-one:<key>' format (pre prefix
    change) is recognized by the backward-compat lookup added in commit
    e04e0b289c so the violation is NOT re-filed under the new key during the
    transition window. Regression for finding 4 of PR #332 cycle-3 review.
    """
    legacy_dedup_key = "at-most-one:PROJ-LEGACY"
    snapshot = {
        "PROJ-LEGACY": {"dso_local_ids": ["legacy-a", "legacy-b"]},
    }

    # Store has the LEGACY key only — not the new prefixed format.
    mock_store = _make_alert_store_mock(deduped_keys={legacy_dedup_key})

    with patch.object(invariants, "_load_alert_store", return_value=mock_store):
        with patch("invariants.subprocess.run") as mock_run:
            result = invariants.check_at_most_one_dso_local_id(
                snapshot, repo_root=tmp_path, ticket_cli="/fake/dso"
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
        f"PROJ-{100 + i * 100}": {"dso_local_ids": [f"local-a{i}", f"local-b{i}"]}
        for i in range(7)
    }

    mock_store = _make_alert_store_mock(deduped_keys=set())
    # Canonical 16-hex format required (see _make_subprocess_result docstring).
    mock_proc_result = _make_subprocess_result(
        returncode=0, stdout="cafe-babe-dead-beef"
    )

    with patch.object(invariants, "_load_alert_store", return_value=mock_store):
        with patch("invariants.subprocess.run", return_value=mock_proc_result) as mock_run:
            result = invariants.check_at_most_one_dso_local_id(
                snapshot, repo_root=tmp_path, ticket_cli="/fake/dso"
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

RECONCILE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "reconcile.py"
)


_RECONCILE_COLLAB_KEYS = (
    "reconcile_fetcher",
    "reconcile_differ",
    "reconcile_applier",
    "reconcile_health",
    "reconcile_invariants",
)


def _load_reconcile() -> ModuleType:
    """Load a fresh reconcile module, evicting only the 'reconcile' entry itself.

    Collaborator stubs (reconcile_fetcher, etc.) must be pre-registered in
    sys.modules BEFORE calling this helper so that reconcile._load() finds them.
    This function intentionally does NOT evict collaborators.
    """
    sys.modules.pop("reconcile", None)
    spec = importlib.util.spec_from_file_location("reconcile", RECONCILE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reconcile"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _make_stub_fetcher(snapshot: dict, pass_id: str, tmp_path: Path) -> ModuleType:
    """Return a stub reconcile_fetcher that writes snapshot to tmp_path and returns the path."""
    snap_dir = tmp_path / "bridge_state" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_file = snap_dir / f"{pass_id}.json"
    snap_file.write_text(json.dumps(snapshot))

    stub = types.ModuleType("reconcile_fetcher")

    def _fetch(pid, repo_root):  # noqa: ANN001
        # Write the snapshot for this pass
        out_dir = repo_root / "bridge_state" / "snapshots"
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

    def _apply(mutations, pass_id, repo_root, **kwargs):  # noqa: ANN001
        # Bug 85a1: reconcile_once now passes binding_store=; accept and
        # ignore for stub purposes.
        manifest = repo_root / "bridge_state" / "manifests" / f"{pass_id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"mutations": len(mutations)}))
        return manifest

    stub.apply = _apply
    return stub


def _make_stub_health() -> ModuleType:
    stub = types.ModuleType("reconcile_health")
    stub.record_pass = lambda **kwargs: None
    # reconcile_once() calls count_open_by_type() before record_pass(); stub must expose both.
    stub.count_open_by_type = lambda repo_root=None: {}
    return stub


# ---------------------------------------------------------------------------
# Test (e): reconcile_once() calls check_at_most_one_dso_local_id exactly once
# ---------------------------------------------------------------------------


def test_reconcile_once_invokes_invariant_after_fetch(tmp_path):
    """reconcile_once() must call check_at_most_one_dso_local_id exactly once
    with the post-fetch snapshot dict, BEFORE computing mutations.

    All collaborators (fetcher, differ, applier, health, invariants) are mocked
    via sys.modules so reconcile._load() picks them up.
    """
    pass_id = "inv-invoke-test"
    snapshot = {
        "JIRA-1": {"summary": "clean issue", "dso_local_ids": ["local-abc"]},
    }

    # Build stubs
    stub_fetcher = _make_stub_fetcher(snapshot, pass_id, tmp_path)
    stub_differ = _make_stub_differ()
    stub_applier = _make_stub_applier(tmp_path)
    stub_health = _make_stub_health()

    # Build invariants stub with a trackable mock
    stub_invariants = types.ModuleType("reconcile_invariants")
    invariants_mock = MagicMock(return_value=[])
    stub_invariants.check_at_most_one_dso_local_id = invariants_mock
    stub_invariants.check_dual_identity_complete = lambda prev, curr: (set(), [])
    stub_invariants.report_schema_drift = lambda *a, **kw: None

    # Pre-register all stubs BEFORE loading reconcile so reconcile._load() finds them
    for key in _RECONCILE_COLLAB_KEYS:
        sys.modules.pop(key, None)
    sys.modules["reconcile_fetcher"] = stub_fetcher
    sys.modules["reconcile_differ"] = stub_differ
    sys.modules["reconcile_applier"] = stub_applier
    sys.modules["reconcile_health"] = stub_health
    sys.modules["reconcile_invariants"] = stub_invariants

    reconcile_mod = _load_reconcile()
    try:
        reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
    finally:
        # Clean up injected stubs
        for key in _RECONCILE_COLLAB_KEYS + ("reconcile",):
            sys.modules.pop(key, None)

    # check_at_most_one_dso_local_id must have been called exactly once
    assert invariants_mock.call_count == 1, (
        f"Expected check_at_most_one_dso_local_id to be called once, "
        f"got {invariants_mock.call_count}"
    )

    # The first positional argument must be the post-fetch snapshot dict
    called_snapshot = invariants_mock.call_args[0][0]
    assert called_snapshot == snapshot, (
        f"check_at_most_one_dso_local_id was called with wrong snapshot: "
        f"{called_snapshot!r} != {snapshot!r}"
    )

    # repo_root must be passed as keyword argument
    called_kwargs = invariants_mock.call_args[1]
    assert "repo_root" in called_kwargs, (
        "check_at_most_one_dso_local_id must be called with repo_root kwarg"
    )


# ---------------------------------------------------------------------------
# Test (f): end-to-end — pass 2 violation → one BRIDGE_ALERT entry + one ticket-cli call
# ---------------------------------------------------------------------------


def test_end_to_end_second_write_produces_one_alert_one_bug(tmp_path):
    """Two passes through reconcile_once() using the real invariants + alert_store.

    Pass 1: clean snapshot (no violations) → no alerts, no ticket-cli calls.
    Pass 2: 'JIRA-42' has dso_local_ids=['id1','id2'] (violation) → exactly one
    alert entry in bridge_state/alerts/ and exactly one ticket-cli subprocess call.

    Strategy: load the real invariants module once, patch subprocess.run on it, then
    pre-register it under 'reconcile_invariants' in sys.modules so that reconcile._load()
    reuses the same patched instance for both passes.
    """
    pass_id = "e2e-alert-test"

    snapshot_clean = {
        "JIRA-1": {"summary": "clean", "dso_local_ids": ["local-only"]},
    }
    snapshot_violation = {
        "JIRA-1": {"summary": "clean", "dso_local_ids": ["local-only"]},
        "JIRA-42": {"summary": "dup", "dso_local_ids": ["id1", "id2"]},
    }

    def _run_pass_e2e(snapshot: dict, invariants_instance: ModuleType) -> None:
        """Run one reconciler pass with all collaborator stubs injected.

        invariants_instance is pre-registered in sys.modules by the caller
        so reconcile._load() returns it without loading a fresh copy.
        """
        stub_fetcher = _make_stub_fetcher(snapshot, pass_id, tmp_path)
        stub_differ = _make_stub_differ()
        stub_applier = _make_stub_applier(tmp_path)
        stub_health = _make_stub_health()

        # Evict reconcile and all collaborators except invariants (managed by caller)
        keys_to_evict = [k for k in _RECONCILE_COLLAB_KEYS if k != "reconcile_invariants"]
        keys_to_evict.append("reconcile")
        for key in keys_to_evict:
            sys.modules.pop(key, None)

        sys.modules["reconcile_fetcher"] = stub_fetcher
        sys.modules["reconcile_differ"] = stub_differ
        sys.modules["reconcile_applier"] = stub_applier
        sys.modules["reconcile_health"] = stub_health
        # reconcile_invariants is already registered by caller — do not touch it

        reconcile_mod = _load_reconcile()
        try:
            reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            for key in keys_to_evict:
                sys.modules.pop(key, None)

    # Load the real invariants module once, patch subprocess on it
    invariants_mod = _load_invariants()

    with patch.object(invariants_mod, "subprocess") as mock_subproc:
        mock_subproc.run.return_value = MagicMock(returncode=0, stdout="bug-e2e-001\n")

        # Pre-register the patched invariants under the name reconcile._load() uses
        sys.modules["reconcile_invariants"] = invariants_mod
        try:
            # Pass 1: clean snapshot — no violations expected
            _run_pass_e2e(snapshot_clean, invariants_mod)
            calls_after_pass1 = mock_subproc.run.call_count

            # Pass 2: snapshot with a violation for JIRA-42
            _run_pass_e2e(snapshot_violation, invariants_mod)
            calls_after_pass2 = mock_subproc.run.call_count
        finally:
            sys.modules.pop("reconcile_invariants", None)

    # Pass 1 must have produced zero ticket-cli calls
    assert calls_after_pass1 == 0, (
        f"Pass 1 (clean) should produce 0 ticket-cli calls, got {calls_after_pass1}"
    )

    # Pass 2 must have produced exactly one ticket-cli call
    assert calls_after_pass2 == 1, (
        f"Pass 2 (violation) should produce exactly 1 ticket-cli call, "
        f"got {calls_after_pass2}"
    )

    # Exactly one at-most-one invariant BRIDGE_ALERT entry must exist.
    # Note: binding-commit-failure alerts may also be present (the test
    # environment has no .tickets-tracker git repo, so the commit step
    # fails gracefully — this is expected and does not affect the invariant
    # being tested here).
    alerts_dir = tmp_path / "bridge_state" / "bridge_alerts"
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
        r for r in all_alert_lines
        if r.get("jira_key") or "at-most-one" in r.get("key", "")
    ]
    assert len(invariant_alerts) == 1, (
        f"Expected exactly 1 at-most-one invariant BRIDGE_ALERT entry, "
        f"found {len(invariant_alerts)}: {invariant_alerts!r} "
        f"(all alerts: {all_alert_lines!r})"
    )
    assert invariant_alerts[0].get("jira_key") == "JIRA-42", (
        f"Alert should be for JIRA-42, got: {invariant_alerts[0]!r}"
    )
