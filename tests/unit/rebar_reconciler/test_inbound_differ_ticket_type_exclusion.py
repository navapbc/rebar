"""Bug 36af: inbound differ must exclude ticket_type from field diffs.

ticket_type sync is an approved exception. Outbound already honors it
(outbound_differ does not emit ticket_type). The inbound direction was
silently still diffing it — so a bound ticket with local 'epic' and Jira
'bug' produced an inbound update that would corrupt the local epic.

This RED test asserts inbound_differ excludes ticket_type from the
field map, mirroring the outbound exception.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "inbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)


@pytest.fixture(scope="module")
def inbound_differ() -> ModuleType:
    return _load_module("inbound_differ_36af", INBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load_module("outbound_differ_36af", OUTBOUND_DIFFER_PATH)


class StubOutboundBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_local_id(self, jira_key: str) -> str | None:
        return self._bindings.get(jira_key)


def test_ticket_type_mismatch_does_not_emit_inbound_mutation(
    inbound_differ: ModuleType,
) -> None:
    """Bug 36af: local 'epic' vs Jira 'Bug' -> ZERO inbound mutations.

    Pre-fix: the field_map in _diff_jira_vs_local included
    'ticket_type', so this scenario emitted an inbound update setting
    local.ticket_type = 'bug' — corrupting the epic.
    """
    jira_snapshot = {
        "PROJ-1": {
            "summary": "Same title",
            "description": "Same desc",
            "issuetype": "Bug",  # would map to local 'bug'
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",
            "labels": [],
        }
    }
    store = StubBindingStore({"PROJ-1": "local-1"})
    local_tickets = {
        "local-1": {
            "title": "Same title",
            "description": "Same desc",
            "ticket_type": "epic",  # the type that must not be overwritten
            "priority": 2,
            "status": "open",
            "assignee": "",
            "tags": [],
        }
    }

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    assert result == [], (
        f"inbound differ leaked ticket_type mutation: {result}"
    )
    assert suppressed == 0


def test_ticket_type_excluded_even_when_other_fields_changed(
    inbound_differ: ModuleType,
) -> None:
    """When OTHER fields legitimately changed Jira-side, the inbound
    mutation must NOT carry ticket_type in its fields dict. Otherwise
    the applier would write ticket_type into the EDIT event alongside
    the legitimate field, corrupting the type as a side effect.
    """
    jira_snapshot = {
        "PROJ-2": {
            "summary": "Jira updated title",  # legitimate Jira-side change
            "description": "Same desc",
            "issuetype": "Bug",  # would map to local 'bug' — must be excluded
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",
            "labels": [],
        }
    }
    store = StubBindingStore({"PROJ-2": "local-2"})
    local_tickets = {
        "local-2": {
            "title": "Original title",
            "description": "Same desc",
            "ticket_type": "epic",
            "priority": 2,
            "status": "open",
            "assignee": "",
            "tags": [],
        }
    }

    result, _ = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    assert len(result) == 1
    m = result[0]
    # Title legitimately changes; ticket_type must NOT appear in fields.
    assert "title" in m.fields
    assert "ticket_type" not in m.fields, (
        f"ticket_type leaked into inbound mutation fields: {m.fields}"
    )


def test_outbound_excludes_issuetype_from_update_mutations(
    outbound_differ: ModuleType,
) -> None:
    """Mirror of bug 36af on the outbound direction.

    Pre-fix: outbound _diff_fields included issuetype in the field map,
    so a bound ticket with local 'epic' vs Jira 'Bug' produced an
    outbound update setting issuetype='Epic'. The approved sync
    exception says ticket_type updates do NOT propagate in either
    direction once bound.

    issuetype IS still emitted at CREATE (it's a Jira-required field
    for issue creation) — see test_outbound_create_includes_issuetype.
    """
    ticket = {
        "ticket_id": "local-3",
        "title": "Same",
        "description": "Same",
        "status": "open",
        "priority": 2,
        "ticket_type": "epic",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
    }
    store = StubOutboundBindingStore({"local-3": "PROJ-3"})
    jira_snapshot = {
        "PROJ-3": {
            "summary": "Same",
            "description": "Same",
            "issuetype": "Bug",  # mismatched type
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",
            "labels": [],
        }
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    # Either no mutation at all (correct), or a mutation with no
    # issuetype in the fields. Both are acceptable; what's NOT
    # acceptable is an issuetype emission.
    for m in result:
        assert "issuetype" not in m.fields, (
            f"outbound update leaked issuetype: {m.fields}"
        )


def test_outbound_create_still_includes_issuetype(
    outbound_differ: ModuleType,
) -> None:
    """The exclusion only applies to UPDATE mutations. CREATE mutations
    must still carry issuetype because Jira requires it when opening
    a new issue.
    """
    ticket = {
        "ticket_id": "local-4",
        "title": "New",
        "description": "",
        "status": "open",
        "priority": 2,
        "ticket_type": "epic",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
    }
    store = StubOutboundBindingStore({})  # unbound -> create
    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot={},
        binding_store=store,
    )

    assert len(result) == 1
    assert result[0].action == "create"
    assert result[0].fields.get("issuetype") == "Epic", (
        "create mutations must include issuetype (Jira-required for new issues)"
    )
