"""Behavioral `idea ↔ IDEA` reconciler translation (story tawny-herb-bug).

Beyond the static map-parity assertions in ``test_config.py``, this pins the
observable translation behavior end-to-end:

- outbound: a local ``idea`` ticket's field payload carries Jira ``status="IDEA"``;
- inbound: Jira ``IDEA`` resolves to local ``idea`` (NOT the ``open`` fallback);
- preflight: the status-mapping scan does not abort (or even warn) on a local
  ``idea`` update mutation, because ``idea`` is now a mapped status.

The ``rebar_reconciler`` package is importable via the sibling conftest, which
puts ``src/rebar/_engine`` on ``sys.path``.
"""

from __future__ import annotations

from rebar_reconciler import inbound_translate, outbound_fields, reconcile


def test_outbound_idea_ticket_yields_jira_idea():
    ticket = {"title": "An idea", "ticket_type": "epic", "status": "idea", "priority": 2}
    fields = outbound_fields._map_local_to_jira_fields(ticket)
    assert fields["status"] == "IDEA"


def test_inbound_jira_idea_yields_local_idea():
    assert inbound_translate._jira_status_to_local("IDEA") == "idea"
    # sanity: an unknown status still falls back to open (regression guard)
    assert inbound_translate._jira_status_to_local("Nonexistent") == "open"


def test_inbound_translate_recognizes_idea_vocabulary():
    assert "idea" in inbound_translate._LOCAL_STATUS_VALUES


def test_preflight_does_not_abort_on_idea():
    mutations = [
        {"action": "update", "direction": "outbound", "key": "t1", "fields": {"status": "idea"}}
    ]
    # Must not raise (idea is a mapped status) — returns None normally.
    assert reconcile.preflight_status_mapping(mutations) is None
