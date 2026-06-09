"""Regression test for Bug d822: HEAD drift caused by in-loop bug-filing.

Bug summary:
  The reconciler's _apply_batch loop pins HEAD and re-checks it on every
  iteration to detect concurrent writers. But `_apply_inbound_conflict`
  (called from the inbound dispatch loop in `apply()`) directly invokes
  `_file_conflict_bug_ticket`, which shells out to `dso ticket create bug`.
  That CLI commits to the `tickets` orphan branch, advancing HEAD. While
  the inbound dispatch happens BEFORE the batch loop's pin in the current
  flow, ANY future caller that reaches the batch loop with an in-flight
  conflict — or any concurrent reconciler producing inbound conflicts —
  trips the drift detector spuriously.

  More fundamentally: the apply path should be commit-free. Bug filing is
  an out-of-band side effect that belongs AFTER the mutation loop, not
  during.

Fix (Option C from investigation d822):
  - `_apply_inbound_conflict` records a `pending_bug_ticket` directive
    in its ApplyResult payload INSTEAD of calling _file_conflict_bug_ticket.
  - `apply()` collects all pending_bug_ticket directives during the loop.
  - After `_apply_batch` returns, `apply()` files the pending tickets
    in a deferred step — outside the drift-guarded loop.

This regression test asserts the structural property: _apply_inbound_conflict
must NOT call _file_conflict_bug_ticket directly; it must return a
pending_bug_ticket directive in its result payload.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)
MUTATION_PATH = APPLIER_PATH.parent / "mutation.py"


def _load_mutation_module():
    canonical = "rebar_reconciler.mutation"
    if canonical in sys.modules:
        return sys.modules[canonical]
    spec = importlib.util.spec_from_file_location(canonical, MUTATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_applier():
    spec = importlib.util.spec_from_file_location(
        "applier_conflict_deferral", APPLIER_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_conflict_deferral"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


@pytest.fixture(scope="module")
def mut_mod():
    return _load_mutation_module()


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_ENV_ID", raising=False)
    monkeypatch.delenv("REBAR_AUTHOR", raising=False)
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("test-env-id", encoding="utf-8")
    return tmp_path


def _make_mutation(mut_mod, *, direction, action, target, payload=None):
    return mut_mod.Mutation(
        direction=direction,
        action=action,
        target=target,
        payload=payload or {},
        provenance={"source": "test"},
    )


def test_inbound_conflict_leaf_does_not_invoke_subprocess(
    applier, mut_mod, fixture_repo, monkeypatch
):
    """_apply_inbound_conflict must NOT spawn the ticket-CLI subprocess
    during its leaf dispatch. It must return a pending_bug_ticket directive
    in the result payload instead.

    Rationale: the apply loop's HEAD-drift detector treats the `tickets`
    orphan branch as quiescent. Any subprocess that commits to that branch
    during apply trips the detector. Bug-filing is an out-of-band effect
    that must be deferred until after the loop.
    """
    call_log: list[str] = []

    def _trap_filing(*_args, **_kwargs):
        call_log.append("filed")
        return "bug-id-mocked"

    monkeypatch.setattr(applier, "_file_conflict_bug_ticket", _trap_filing)

    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.conflict,
        target="DIG-99",
        payload={"local_id": "local-99", "reason": "dual-write divergence"},
    )

    result = applier._apply_inbound_conflict(mutation, repo_root=fixture_repo)

    # Structural assertion: the leaf must NOT call the bug-filing subprocess
    # directly. The current (buggy) implementation calls it; this assertion
    # FAILS before the fix and PASSES after.
    assert call_log == [], (
        f"_apply_inbound_conflict invoked _file_conflict_bug_ticket "
        f"{len(call_log)} time(s) during the leaf dispatch. After the fix, "
        f"bug filing must be deferred via a 'pending_bug_ticket' directive "
        f"in the result payload."
    )

    # The leaf must return a pending_bug_ticket directive so the caller can
    # file it after the apply loop completes.
    assert "pending_bug_ticket" in result.payload, (
        "expected 'pending_bug_ticket' in result.payload after deferred-filing fix"
    )

    pending = result.payload["pending_bug_ticket"]
    assert pending.get("local_id") == "local-99"
    assert pending.get("jira_key") == "DIG-99"
    # The directive must carry the same data _file_conflict_bug_ticket needs.
    assert "title" in pending
    assert "description" in pending


def _make_fake_acli_mod():
    """Build a minimal stub acli module so apply() can construct its client
    without importing the real acli-integration.py (which has a known
    `rebar_reconciler.adf` ModuleNotFoundError under test load)."""
    import types

    fake = types.ModuleType("acli_integration_stub")
    fake.AcliClient = lambda **_: types.SimpleNamespace(
        update_issue=lambda *a, **kw: {},
        add_label=lambda *a, **kw: None,
        remove_label=lambda *a, **kw: None,
        add_comment=lambda *a, **kw: None,
        search_issues=lambda *a, **kw: [],
        create_issue=lambda *a, **kw: {"key": "DIG-MOCK"},
    )
    return fake


def test_apply_files_pending_bug_tickets_after_apply_batch_returns(
    applier, mut_mod, fixture_repo, monkeypatch
):
    """Positive postcondition: when apply() processes an inbound conflict
    mutation, it MUST call _file_conflict_bug_ticket for the resulting
    pending_bug_ticket directive AFTER _apply_batch returns.

    Complements `test_inbound_conflict_leaf_does_not_invoke_subprocess`
    which verifies the leaf does NOT call directly. Without this positive
    test, the deferred-filing block at applier.py:apply() could be silently
    deleted and the negative test would still pass."""
    call_log: list[dict] = []

    def _capture_filing(cli_path, title, description, parent_id):
        call_log.append(
            {
                "title": title,
                "description": description,
                "parent_id": parent_id,
            }
        )
        return "bug-id-mocked"

    monkeypatch.setattr(applier, "_file_conflict_bug_ticket", _capture_filing)
    monkeypatch.setattr(applier, "_apply_batch", lambda *a, **kw: None)
    monkeypatch.setattr(applier, "_load_acli", lambda: _make_fake_acli_mod())

    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.conflict,
        target="DIG-101",
        payload={"local_id": "local-101", "reason": "test divergence"},
    )

    applier.apply([mutation], "test-pass", repo_root=fixture_repo)

    assert len(call_log) == 1, (
        f"apply() must call _file_conflict_bug_ticket exactly once for one "
        f"inbound conflict, got {len(call_log)} call(s). Without this call, "
        f"the deferred-filing block in apply() is broken — conflicts are "
        f"silently suppressed without an audit ticket."
    )
    assert "DIG-101" in call_log[0]["title"]
    assert "local-101" in call_log[0]["title"]


def test_apply_files_pending_bug_tickets_when_apply_batch_raises(
    applier, mut_mod, fixture_repo, monkeypatch
):
    """Critical correctness postcondition: if _apply_batch raises (e.g.
    HeadDriftError, RescheduleError), the deferred bug-filing block MUST
    still execute. Otherwise the conflict audit ticket is silently dropped
    — the conflict was already suppressed by the leaf's follow_on, but the
    operator loses visibility.

    Regression guard for the important finding in deep review of PR #425:
    "Deferred bug-filing is not guaranteed to execute when _apply_batch
    raises ... All collected pending_bug_ticket directives are silently
    dropped."

    The fix wraps _apply_batch in try/finally so the deferred-filing block
    runs unconditionally before the exception propagates."""
    call_log: list[dict] = []

    def _capture_filing(cli_path, title, description, parent_id):
        call_log.append({"title": title})
        return "bug-id-mocked"

    class _SimulatedDriftError(Exception):
        pass

    def _raising_apply_batch(*a, **kw):
        raise _SimulatedDriftError("simulated HeadDriftError")

    monkeypatch.setattr(applier, "_file_conflict_bug_ticket", _capture_filing)
    monkeypatch.setattr(applier, "_apply_batch", _raising_apply_batch)
    monkeypatch.setattr(applier, "_load_acli", lambda: _make_fake_acli_mod())

    # Pair an inbound conflict (produces pending_bug_ticket) with an
    # outbound mutation (forces _apply_batch into the call path).
    conflict_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.conflict,
        target="DIG-202",
        payload={"local_id": "local-202", "reason": "exception path test"},
    )
    outbound_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.create,
        target="local-203",
        payload={"summary": "outbound", "local_id": "local-203"},
    )

    # apply() should re-raise the SimulatedDriftError after deferred filing.
    with pytest.raises(_SimulatedDriftError):
        applier.apply([conflict_mut, outbound_mut], "test-pass", repo_root=fixture_repo)

    # The deferred-filing block MUST have run in the finally clause despite
    # the exception.
    assert len(call_log) == 1, (
        f"apply() did NOT file the pending bug ticket on the _apply_batch "
        f"exception path. Without try/finally, audit tickets are silently "
        f"dropped when the apply pipeline fails. Got {len(call_log)} "
        f"call(s) to _file_conflict_bug_ticket."
    )
    assert "DIG-202" in call_log[0]["title"]


def test_inbound_conflict_still_emits_suppress_pair_follow_on(
    applier, mut_mod, fixture_repo, monkeypatch
):
    """After deferring bug-filing, _apply_inbound_conflict must still emit
    the suppress_pair follow_on so reconcile_once can drop subsequent
    mutations on the same pair. (Regression guard for the existing
    test_inbound_conflict_emits_suppress_pair contract.)"""
    monkeypatch.setattr(applier, "_file_conflict_bug_ticket", lambda *a, **kw: "")

    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.conflict,
        target="DIG-7",
        payload={"local_id": "jira-dig-7", "reason": "dual-write divergence"},
    )
    result = applier._apply_typed(mutation, repo_root=fixture_repo)

    follow_on = result.payload["follow_on"]
    assert follow_on["kind"] == "suppress_pair"
    assert follow_on["jira_key"] == "DIG-7"
    assert follow_on["local_id"] == "jira-dig-7"
