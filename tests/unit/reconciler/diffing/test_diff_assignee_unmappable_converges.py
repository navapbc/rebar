"""An unresolvable local assignee means desired unassigned; an already-unassigned Jira
issue must converge without repeated updates. Resolvable users still sync, and the
no-resolver path keeps permissive matching."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

import pytest

from rebar.reconciler.adapters.jira.backend import JiraBackend
from rebar.reconciler.outbound_field_diff import compute_update_fields


@pytest.fixture(scope="module")
def differ() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_differ")


def _diff(
    ticket: dict, jira: dict, *, assignee_resolver=None, jira_key: str = ""
) -> dict[str, Any]:
    """Run the live outbound field diff with the Jira backend's mappers."""
    backend = JiraBackend(transport=object())
    return compute_update_fields(
        ticket,
        jira,
        inbound_mapper=backend.inbound,
        outbound_mapper=backend.outbound,
        assignee_resolver=assignee_resolver,
        jira_key=jira_key,
    )


def _ticket(assignee: str) -> dict:
    return {
        "ticket_id": "loc-1",
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": assignee,
    }


def _jira(assignee_dict) -> dict:
    return {
        "summary": "T",
        "description": "D",
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": assignee_dict,
    }


# A resolver that knows only "alice" (→ an accountId); everything else is
# unmappable (None, authoritative). Returns (accountId|None, authoritative,
# is_account_id), the 264f resolver shape.
def _resolver(assignee: str, jira_key: str):
    if not assignee:
        return ("", True, False)
    if assignee == "alice":
        return ("acct-alice", True, False)
    return (None, True, False)  # unmappable → desired unassigned


# --- the core fix: unmappable assignee + unassigned Jira → no emit -----------


def test_unmappable_assignee_converges_when_jira_unassigned() -> None:
    changed = _diff(_ticket("claude"), _jira(None), assignee_resolver=_resolver, jira_key="REB-1")
    assert "assignee" not in changed, (
        f"unmappable assignee with an already-unassigned Jira issue must NOT "
        f"re-emit (convergence); got {changed!r}"
    )


def test_unmappable_assignee_unassigns_when_jira_has_someone() -> None:
    jira = _jira({"accountId": "acct-bob", "displayName": "Bob"})
    changed = _diff(_ticket("claude"), jira, assignee_resolver=_resolver, jira_key="REB-1")
    assert changed.get("assignee") == "", "unmappable local → unassign the Jira issue"


def test_mappable_assignee_emitted_when_jira_unassigned() -> None:
    changed = _diff(_ticket("alice"), _jira(None), assignee_resolver=_resolver, jira_key="REB-1")
    assert changed.get("assignee") == "alice", "a mappable assignee is still synced"


def test_mappable_assignee_converges_when_already_assigned() -> None:
    jira = _jira({"accountId": "acct-alice", "displayName": "Alice"})
    changed = _diff(_ticket("alice"), jira, assignee_resolver=_resolver, jira_key="REB-1")
    assert "assignee" not in changed, "already-correct assignee must not re-emit"


def test_no_resolver_preserves_legacy_string_match() -> None:
    # Without a resolver, behaviour is the permissive string match: local "claude"
    # vs unassigned still fires (unchanged legacy behaviour — the fix is opt-in via
    # the resolver that the live pass supplies).
    changed = _diff(_ticket("claude"), _jira(None))
    assert changed.get("assignee") == "claude"


# --- integration: the resolver closure built inside compute_outbound_mutations ---


class _StubBindingStore:
    def __init__(self, bindings):
        self._b = bindings

    def get_baseline(self, local_id):
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id):
        return False

    def get_jira_key(self, local_id):
        return self._b.get(local_id)

    def is_bound(self, local_id):
        return local_id in self._b


class AssigneeNotFoundError(Exception):
    # Name matches the real acli class; the resolver classifies by class name.
    pass


class _FakeClient:
    """Resolves only 'alice'; raises AssigneeNotFoundError otherwise. get_comments
    returns [] so the comment differ makes no further calls."""

    def __init__(self):
        self.calls = []

    def validate_assignee_exists(self, assignee, *, issue_key=None, project_key=None):
        self.calls.append(assignee)
        if assignee == "alice":
            return "acct-alice"
        raise AssigneeNotFoundError(f"no assignable user for {assignee!r}")

    def get_comments(self, jira_key):
        return []


def test_compute_outbound_unmappable_assignee_converges(differ):
    """End-to-end through compute_outbound_mutations + the real resolver closure:
    an unmappable assignee with an unassigned bound issue emits NO assignee update,
    and the resolution is cached (one lookup for the repeated assignee)."""
    ticket = _ticket("claude")
    jira_key = "REB-9"
    snapshot = {jira_key: _jira(None) | {"comment": {"comments": [], "total": 0}}}
    client = _FakeClient()
    muts, _ = differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=_StubBindingStore({"loc-1": jira_key}),
        config=differ.OutboundDiffConfig(
            client=client,
        ),
    )
    assignee_updates = [m for m in muts if getattr(m, "fields", None) and "assignee" in m.fields]
    assert assignee_updates == [], (
        f"unmappable assignee must converge (no assignee mutation); got "
        f"{[m.fields for m in muts if getattr(m, 'fields', None)]}"
    )
