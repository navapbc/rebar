"""Conflict filing runs after the apply batch so its ticket commit cannot trip HEAD-drift
checks. Pending directives survive the finally path, preserve pair suppression, and
re-raise batch errors."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from rebar.reconciler import ticket_planner
from rebar.reconciler.mutation_payloads import OutboundCreatePayload

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "applier.py"
MUTATION_PATH = APPLIER_PATH.parent / "mutation.py"


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


def _load_mutation_module():
    return importlib.import_module("rebar.reconciler.mutation")


def _load_applier():
    return importlib.import_module("rebar.reconciler.applier")


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


@pytest.fixture(scope="module")
def mut_mod():
    return _load_mutation_module()


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
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
    """The inbound conflict leaf returns a pending bug directive without spawning a
    ticket-writing subprocess inside the apply loop."""
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

    # The conflict leaf must return a directive without running the ticket CLI.
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
    without importing the real reconciler/acli.py (which has a known
    `rebar.reconciler.adapters.jira.adf` ModuleNotFoundError under test load)."""
    import types

    # S4: _load_acli returns the transport (client) directly.
    return types.SimpleNamespace(
        update_issue=lambda *a, **kw: {},
        add_label=lambda *a, **kw: None,
        remove_label=lambda *a, **kw: None,
        add_comment=lambda *a, **kw: None,
        search_issues=lambda *a, **kw: [],
        create_issue=lambda *a, **kw: {"key": "DIG-MOCK"},
    )


def test_apply_files_pending_bug_tickets_after_apply_batch_returns(
    applier, mut_mod, fixture_repo, monkeypatch
):
    """Apply files pending conflict bugs after the batch returns, rather than dropping the
    deferred directive."""
    call_log: list[dict] = []

    def _capture_filing(cli_path, pending):
        call_log.append(
            {
                "title": pending.get("title", ""),
                "description": pending.get("description", ""),
                "parent_id": pending.get("parent_id", ""),
            }
        )
        return "bug-id-mocked"

    monkeypatch.setattr(applier, "_file_conflict_bug_ticket", _capture_filing)
    monkeypatch.setattr(applier, "_apply_batch", lambda *a, **kw: None)
    monkeypatch.setattr(applier, "_load_acli", _make_fake_acli_mod)

    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.conflict,
        target="DIG-101",
        payload={"local_id": "local-101", "reason": "test divergence"},
    )

    applier.apply([mutation], "test-pass", repo_root=fixture_repo, ticket_plans=_plans([mutation]))

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
    """Apply files pending conflict bugs in finally even if the batch raises, then propagates
    the exception."""
    call_log: list[dict] = []

    def _capture_filing(cli_path, pending):
        call_log.append({"title": pending.get("title", "")})
        return "bug-id-mocked"

    class _SimulatedDriftError(Exception):
        pass

    def _raising_apply_batch(*a, **kw):
        raise _SimulatedDriftError("simulated HeadDriftError")

    monkeypatch.setattr(applier, "_file_conflict_bug_ticket", _capture_filing)
    monkeypatch.setattr(applier, "_apply_batch", _raising_apply_batch)
    monkeypatch.setattr(applier, "_load_acli", _make_fake_acli_mod)

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
        payload=OutboundCreatePayload(fields={"summary": "outbound"}, local_id="local-203"),
    )

    # apply() should re-raise the SimulatedDriftError after deferred filing.
    with pytest.raises(_SimulatedDriftError):
        applier.apply(
            [conflict_mut, outbound_mut],
            "test-pass",
            repo_root=fixture_repo,
            ticket_plans=_plans([conflict_mut, outbound_mut]),
        )

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
