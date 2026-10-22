"""Tests for HEAD-pin drift detection in applier.apply().

RED task 100b: verifies that:
  1. Drift detected mid-pass aborts after the first mutation (no further Jira calls).
  2. HeadDriftError is raised when HEAD changes mid-pass.
  3. Empty mutation list is a no-op (snapshot_head is NOT invoked).
  4. Stable HEAD (no drift) allows all mutations to be dispatched normally.

All tests mock snapshot_head to return controlled values.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler import ticket_planner
from rebar.reconciler.mutation import Mutation, MutationAction, MutationDirection
from rebar.reconciler.mutation_payloads import OutboundUpdatePayload

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "applier.py"


def _plans(mutations, *, mode=None, max_changes=None):
    """Ticket plans for the outbound ``mutations``, built as ``run_differs`` builds them.

    The batch form of ``applier.apply`` requires ``ticket_plans`` (ADR 0103 S3). Inbound
    mutations dispatch through ``_apply_typed``, so only the outbound ones are planned.
    """
    _, plans = ticket_planner.plan_pass(
        pass_id="test-pass",
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
        mode=str(getattr(mode, "value", mode) or ""),
        selection={"kind": None, "ids": []},
        limits={"max_changes": max_changes},
        mutations=[
            m
            for m in mutations
            if getattr(getattr(m, "direction", None), "value", None) == "outbound"
        ],
    )
    return plans


def _load_applier():
    # Force fresh load so module-level state doesn't bleed between test runs
    return importlib.import_module("rebar.reconciler.applier")


@pytest.fixture(autouse=True)
def _dso_project(monkeypatch):
    # DSO-keyed drift fixtures: pin the configured project to DSO (overriding the
    # conftest DIG default) so the bug-626d cross-project guard doesn't flag them.
    monkeypatch.setenv("JIRA_PROJECT", "DSO")


@pytest.fixture(scope="module")
def applier():
    """Load the applier module, failing all tests if absent."""
    if not APPLIER_PATH.exists():
        pytest.fail(
            f"applier.py not found at {APPLIER_PATH} — implement the module to make tests pass."
        )
    return _load_applier()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_acli_module() -> tuple[MagicMock, MagicMock]:
    """Return (transport, transport) — a transport-shaped fake with tracked calls.

    S4: ``_load_acli`` returns the backend transport DIRECTLY, so the seam's
    ``return_value`` is the transport itself; both tuple slots are the same object so
    the ``mock_acli_mod, mock_client = _make_mock_acli_module()`` call sites keep
    working unchanged.
    """
    mock_client = MagicMock()
    mock_client.create_issue = MagicMock(return_value={"key": "DSO-1"})
    mock_client.update_issue = MagicMock(return_value={"key": "DSO-2"})
    mock_client.transition_issue = MagicMock(return_value=None)
    mock_client.search_issues = MagicMock(return_value=[])

    return mock_client, mock_client


def _make_snapshot_head(head_sequence: list[str]):
    """Return a ``snapshot_head`` stand-in that pops from head_sequence."""
    call_iter = iter(head_sequence)

    def _snapshot_head(_repo_root):
        return next(call_iter)

    return _snapshot_head


def _update(key: str, **fields) -> Mutation:
    return Mutation(
        MutationDirection.outbound,
        MutationAction.update,
        key,
        OutboundUpdatePayload(changed_fields=fields),
        {},
    )


def _delete(key: str) -> Mutation:
    return Mutation(MutationDirection.outbound, MutationAction.delete, key, {}, {})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_drift_mid_pass_raises_head_drift_error(tmp_path, applier):
    """HeadDriftError is raised when HEAD changes between the first and second mutation."""
    # HEAD is stable for the initial pin + first iteration check,
    # then drifts before the second mutation's iteration check.
    sha_stable = "aabbccdd" * 5  # 40 chars
    sha_drifted = "11223344" * 5  # 40 chars

    # Sequence: pin (stable), iter-1 check (stable), iter-2 check (drifted)
    head_seq = [sha_stable, sha_stable, sha_drifted]
    mock_acli_mod, _mock_client = _make_mock_acli_module()
    snapshot_head = _make_snapshot_head(head_seq)

    mutations = [_update("DSO-10", summary="first"), _update("DSO-11", summary="second")]

    with (
        patch.object(applier, "_load_acli", return_value=mock_acli_mod),
        patch.object(applier, "snapshot_head", snapshot_head),
    ):
        with pytest.raises(applier.HeadDriftError):
            applier.apply(
                mutations, "pass-drift-01", repo_root=tmp_path, ticket_plans=_plans(mutations)
            )


def test_drift_mid_pass_no_jira_call_after_drift(tmp_path, applier):
    """No Jira call is issued for mutations after the drift is detected."""
    sha_stable = "aabbccdd" * 5
    sha_drifted = "11223344" * 5

    # pin, iter-1 (stable → first mutation runs), iter-2 (drifted → abort)
    head_seq = [sha_stable, sha_stable, sha_drifted]
    mock_acli_mod, mock_client = _make_mock_acli_module()
    snapshot_head = _make_snapshot_head(head_seq)

    mutations = [_update("DSO-10", summary="first"), _update("DSO-11", summary="second")]

    with (
        patch.object(applier, "_load_acli", return_value=mock_acli_mod),
        patch.object(applier, "snapshot_head", snapshot_head),
    ):
        with pytest.raises(applier.HeadDriftError):
            applier.apply(
                mutations, "pass-drift-02", repo_root=tmp_path, ticket_plans=_plans(mutations)
            )

    # Only the first mutation's update_issue should have been called
    # F3: fields unpacked as kwargs (real signature: update_issue(key, **kwargs))
    mock_client.update_issue.assert_called_once_with("DSO-10", summary="first")


def test_empty_mutations_no_head_check(tmp_path, applier):
    """Empty mutation list skips the drift guard: no explicit snapshot_head calls from apply().

    The apply() function must not call snapshot_head for the PIN step or the
    per-iteration check when the mutations list is empty.
    """
    mock_acli_mod, _mock_client = _make_mock_acli_module()

    # Tracks calls made directly by apply() itself.
    direct_snapshot_calls: list = []

    # A snapshot_head stand-in that tracks any direct drift checks.
    def snapshot_head(_repo_root):
        direct_snapshot_calls.append(1)
        return "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    with (
        patch.object(applier, "_load_acli", return_value=mock_acli_mod),
        patch.object(applier, "snapshot_head", snapshot_head),
    ):
        manifest_path = applier.apply(
            [], "pass-empty-01", repo_root=tmp_path, ticket_plans=_plans([])
        )

    assert direct_snapshot_calls == [], (
        f"snapshot_head (drift guard) should not be called for empty mutations, "
        f"got {len(direct_snapshot_calls)} calls"
    )
    assert manifest_path.exists(), "Manifest must still be written for empty mutations"


def test_stable_head_all_mutations_dispatched(tmp_path, applier):
    """When HEAD is stable throughout, all mutations are dispatched normally."""
    sha_stable = "aabbccdd" * 5

    # pin + N iteration checks (3 mutations = pin + 3 iteration checks = 4 calls)
    head_seq = [sha_stable] * 4
    mock_acli_mod, mock_client = _make_mock_acli_module()
    snapshot_head = _make_snapshot_head(head_seq)

    mutations = [
        _update("DSO-20", summary="alpha"),
        _update("DSO-21", summary="beta"),
        _delete("DSO-22"),
    ]

    with (
        patch.object(applier, "_load_acli", return_value=mock_acli_mod),
        patch.object(applier, "snapshot_head", snapshot_head),
    ):
        manifest_path = applier.apply(
            mutations, "pass-stable-01", repo_root=tmp_path, ticket_plans=_plans(mutations)
        )

    # All three mutations should have been dispatched
    assert mock_client.update_issue.call_count == 2, (
        f"Expected 2 update_issue calls, got {mock_client.update_issue.call_count}"
    )
    # delete_one now calls client.delete_issue, not transition_issue (which
    # does not exist on AcliClient).
    mock_client.delete_issue.assert_called_once_with("DSO-22")
    assert manifest_path.exists(), "Manifest must be written after successful pass"
