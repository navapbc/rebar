"""Binding baselines, rather than previous snapshots, always arbitrate outbound field
diffs. These tests preserve the former enabled-flag behavior and cold-start
reporting."""

from __future__ import annotations

from typing import Any

from rebar.reconciler.adapters.jira.backend import JiraBackend
from rebar.reconciler.outbound_field_diff import compute_update_fields


def _diff(
    ticket: dict,
    jira: dict,
    *,
    binding_store: Any,
    jira_key: str,
    local_id: str,
    prev_jira_fields: dict | None = None,
    conflict_sink: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Run the live outbound field diff with the Jira backend's mappers."""
    backend = JiraBackend(transport=object())
    return compute_update_fields(
        ticket,
        jira,
        inbound_mapper=backend.inbound,
        outbound_mapper=backend.outbound,
        binding_store=binding_store,
        local_id=local_id,
        jira_key=jira_key,
        prev_snapshot={jira_key: prev_jira_fields} if prev_jira_fields else None,
        conflict_sink=conflict_sink,
    )


class _FakeStore:
    """Minimal binding_store: get_baseline plus the confirmed/pending predicates
    the cold-start diagnostic consults."""

    def __init__(self, baseline, *, bound: bool = True, pending: bool = False):
        self._baseline = baseline
        self._bound = bound
        self._pending = pending

    def get_baseline(self, local_id):
        return self._baseline

    def is_bound(self, local_id):
        return self._bound

    def is_pending(self, local_id):
        return self._pending


def _ticket(**ov) -> dict:
    t = {
        "ticket_id": "loc-1",
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "",
    }
    t.update(ov)
    return t


def _jira(**ov) -> dict:
    f = {
        "summary": "T",
        "description": "D",
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": None,
    }
    f.update(ov)
    return f


# NOTE (story d6bd): the former ``test_flag_off_ignores_baseline_byte_for_byte``
# exercised the flag=OFF prev_snapshot-arbitration path, which no longer exists —
# removed. Arbitration is now unconditionally baseline-based.


def test_conflict_keys_on_baseline() -> None:
    """Both-sides-conflict (Site B) is arbitrated against the baseline: local and
    Jira both differ from the baseline ancestor -> conflict recorded."""
    store = _FakeStore({"description": "D"})  # true ancestor: neither side matches
    conflict_sink: list[tuple[str, str]] = []
    _diff(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-2",
        prev_jira_fields={"description": "local-edit"},  # would NOT conflict if consulted
        conflict_sink=conflict_sink,
        local_id="loc-1",
    )
    assert ("KEY-2", "description") in conflict_sink


def test_dryrun_arbitration_pinned_to_baseline() -> None:
    """With a baseline equal to local but Jira edited and prev_jira_fields stale,
    dry-run must suppress the outbound field. Consulting prev_jira_fields instead
    would emit it."""
    store = _FakeStore({"description": "local-edit"})
    changed = _diff(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-3",
        prev_jira_fields={"description": "STALE"},  # differs from baseline+local
        local_id="loc-1",
    )
    assert "description" not in changed, "baseline is the arbitration ancestor (Jira wins)"


def test_none_baseline_local_wins() -> None:
    """get_baseline returns None (no ancestor recorded): local-wins — the changed
    field is emitted and NO both-sides conflict is recorded."""
    store = _FakeStore(None)
    conflict_sink: list[tuple[str, str]] = []
    changed = _diff(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-5",
        prev_jira_fields={"description": "prev"},
        conflict_sink=conflict_sink,
        local_id="loc-1",
    )
    assert changed.get("description") == "local-edit"  # local-wins
    assert ("KEY-5", "description") not in conflict_sink  # no ancestor -> no conflict


# --- Cold-start observability (story d6bd) -------------------------------------


def test_cold_start_recon_fires_for_confirmed_none_baseline(capsys) -> None:
    """A CONFIRMED binding whose baseline is still None is in the warm-up window:
    emit exactly one ``RECON: baseline_cold_start local_id=<id>`` line to stderr."""
    store = _FakeStore(None, bound=True, pending=False)
    _diff(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-6",
        prev_jira_fields=None,
        local_id="loc-1",
    )
    err = capsys.readouterr().err
    assert "RECON: baseline_cold_start local_id=loc-1" in err
    assert err.count("baseline_cold_start") == 1  # once per binding per pass


def test_cold_start_recon_silent_once_baseline_exists(capsys) -> None:
    """No cold-start line once a baseline exists for the binding."""
    store = _FakeStore({"description": "D"}, bound=True, pending=False)
    _diff(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-7",
        prev_jira_fields=None,
        local_id="loc-1",
    )
    assert "baseline_cold_start" not in capsys.readouterr().err


def test_cold_start_recon_silent_for_pending_binding(capsys) -> None:
    """A still-PENDING binding (not yet confirmed) is not counted as cold-start."""
    store = _FakeStore(None, bound=True, pending=True)
    _diff(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-8",
        prev_jira_fields=None,
        local_id="loc-1",
    )
    assert "baseline_cold_start" not in capsys.readouterr().err
