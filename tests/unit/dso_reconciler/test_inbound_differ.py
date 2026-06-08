"""Unit tests for dso_reconciler/inbound_differ.py.

Tests the inbound differ that detects Jira-side changes for bound tickets
and emits InboundMutation objects for changes to apply locally.

Uses the importlib spec_from_file_location pattern established in the
reconciler test tree (see conftest.py docstring for rationale).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
INBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "inbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def inbound_differ() -> ModuleType:
    return _load_module("inbound_differ", INBOUND_DIFFER_PATH)


# ---------------------------------------------------------------------------
# Stub BindingStore
# ---------------------------------------------------------------------------


class StubBindingStore:
    """In-memory binding store for tests (inbound direction)."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        # bindings: {jira_key: local_id}
        self._bindings: dict[str, str] = bindings or {}

    def get_local_id(self, jira_key: str) -> str | None:
        return self._bindings.get(jira_key)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bound_ticket_jira_changed_emits_inbound_update(
    inbound_differ: ModuleType,
) -> None:
    """Bound ticket where Jira title differs from local -> inbound update."""
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Updated from Jira",
            "description": "Same desc",
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
        }
    }
    store = StubBindingStore({"PROJ-100": "local-1"})
    local_tickets = {
        "local-1": {
            "title": "Original title",
            "description": "Same desc",
            "ticket_type": "bug",
            "priority": 2,
            "status": "open",
            "assignee": "alice",
            "tags": [],
        }
    }

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    assert len(result) == 1
    assert suppressed == 0
    m = result[0]
    assert m.jira_key == "PROJ-100"
    assert m.local_id == "local-1"
    assert m.action == "update"
    assert m.fields == {"title": "Updated from Jira"}


def test_unbound_jira_issue_ignored(inbound_differ: ModuleType) -> None:
    """Unbound Jira issue -> no inbound mutation (local is source of truth)."""
    jira_snapshot = {
        "PROJ-200": {
            "summary": "Some Jira issue",
            "description": "",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",
            "labels": [],
        }
    }
    store = StubBindingStore()  # no bindings
    local_tickets: dict = {}

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    assert result == []
    assert suppressed == 0


def test_bound_both_changed_skipped(inbound_differ: ModuleType) -> None:
    """When both local and Jira changed, the inbound differ still emits.

    Local-wins conflict resolution is enforced at the orchestrator level:
    the outbound differ's mutation takes precedence. The inbound differ
    does not have access to a baseline snapshot to detect local changes,
    so it emits the diff and lets the orchestrator resolve conflicts.

    In practice, when both sides changed, the outbound differ will push
    the local value (local wins), and the inbound mutation will be
    superseded by the outbound mutation during apply ordering.
    """
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Jira changed title",
            "description": "Same",
            "issuetype": "Bug",
            "priority": "High",  # Jira changed priority too
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
        }
    }
    store = StubBindingStore({"PROJ-100": "local-1"})
    local_tickets = {
        "local-1": {
            "title": "Local changed title",  # local also changed
            "description": "Same",
            "ticket_type": "bug",
            "priority": 1,  # local also changed priority
            "status": "open",
            "assignee": "alice",
            "tags": [],
        }
    }

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    # The inbound differ emits the diff. The orchestrator resolves conflicts
    # by giving outbound mutations precedence (local wins).
    assert len(result) == 1
    assert suppressed == 0
    m = result[0]
    # The title differs (Jira says "Jira changed title", local says "Local changed title")
    assert "title" in m.fields
    assert m.fields["title"] == "Jira changed title"


def test_assignee_dict_shape_matches_local_email_no_phantom(
    inbound_differ: ModuleType,
) -> None:
    """Jira assignee dict matching local email form -> no inbound mutation.

    Convergence-churn regression (bug 85a1 family): a real Jira fetch returns
    ``assignee`` as ``{accountId, displayName, emailAddress}``. Local tickets
    store assignee as a bare string that may be the email form (the
    ticket-create default). The outbound differ already tolerates all three
    identity forms via ``_assignee_matches``; the inbound differ did NOT, so it
    extracted only ``displayName`` and compared it against the local email,
    reporting a phantom ``assignee`` change on EVERY pass — the field never
    converges. This asserts shape-tolerant inbound equality: a Jira dict whose
    emailAddress equals the local string emits nothing.
    """
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Same title",
            "description": "Same desc",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "In Progress",
            "assignee": {
                "accountId": "abc123",
                "displayName": "Joe Oakhart",
                "emailAddress": "joeoakhart@navapbc.com",
            },
            "labels": [],
        }
    }
    store = StubBindingStore({"PROJ-100": "local-1"})
    local_tickets = {
        "local-1": {
            "title": "Same title",
            "description": "Same desc",
            "ticket_type": "task",
            "priority": 2,
            "status": "in_progress",
            "assignee": "joeoakhart@navapbc.com",  # email form, not displayName
            "tags": [],
        }
    }

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    assert result == [], (
        "phantom inbound assignee mutation: Jira dict shape did not match "
        "local email form (inbound differ lacks _assignee_matches tolerance)"
    )
    assert suppressed == 0


def test_assignee_dict_genuine_change_still_emitted(
    inbound_differ: ModuleType,
) -> None:
    """A genuine Jira-side assignee change (no identity form matches local)
    still emits an inbound update — shape tolerance must not swallow real
    reassignments. Carries the displayName (local-side canonical form)."""
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Same title",
            "description": "Same desc",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "In Progress",
            "assignee": {
                "accountId": "newperson",
                "displayName": "New Person",
                "emailAddress": "newperson@navapbc.com",
            },
            "labels": [],
        }
    }
    store = StubBindingStore({"PROJ-100": "local-1"})
    local_tickets = {
        "local-1": {
            "title": "Same title",
            "description": "Same desc",
            "ticket_type": "task",
            "priority": 2,
            "status": "in_progress",
            "assignee": "joeoakhart@navapbc.com",
            "tags": [],
        }
    }

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    assert len(result) == 1
    assert result[0].fields.get("assignee") == "New Person"


def test_bound_no_changes_emits_nothing(inbound_differ: ModuleType) -> None:
    """Bound ticket where Jira fields match local -> no mutation."""
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Same title",
            "description": "Same desc",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "In Progress",
            "assignee": "bob",
            "labels": ["shared-label"],
        }
    }
    store = StubBindingStore({"PROJ-100": "local-1"})
    local_tickets = {
        "local-1": {
            "title": "Same title",
            "description": "Same desc",
            "ticket_type": "task",
            "priority": 2,
            "status": "in_progress",
            "assignee": "bob",
            "tags": ["shared-label"],
        }
    }

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    assert result == []
    assert suppressed == 0


def test_inbound_label_diff(inbound_differ: ModuleType) -> None:
    """Jira has a label that local doesn't -> inbound label add."""
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Same",
            "description": "Same",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",
            "labels": ["jira-label", "shared"],
        }
    }
    store = StubBindingStore({"PROJ-100": "local-1"})
    local_tickets = {
        "local-1": {
            "title": "Same",
            "description": "Same",
            "ticket_type": "task",
            "priority": 2,
            "status": "open",
            "assignee": "",
            "tags": ["shared"],
        }
    }

    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    assert len(result) == 1
    assert suppressed == 0
    m = result[0]
    label_adds = [lb for lb in m.labels if lb["action"] == "add"]
    assert any(lb["label"] == "jira-label" for lb in label_adds)


# ---------------------------------------------------------------------------
# Bug eadb (Issue A): colon-form ``dso-id:<local_id>`` must be excluded from
# inbound label diff (same root cause as outbound PR #454, this is the
# inbound mirror). The probe (T3 IB-ADD) saw local pick up
# ``dso-id:jira-dig-5024`` after the reconciler ran because the colon form
# was not in ``_EXCLUDED_PREFIXES``.
# ---------------------------------------------------------------------------


def test_inbound_label_diff_excludes_colon_form_dso_id(
    inbound_differ: ModuleType,
) -> None:
    """``dso-id:<local_id>`` (colon form) must NOT appear as an inbound ADD.

    Pre-fix: ``_EXCLUDED_PREFIXES`` contained only ``("dso-id-", "imported:")``,
    so the colon-form canonical dso-id label written by
    ``_apply_outbound_create`` / ``_apply_inbound_create`` was treated as a
    Jira-only user label and emitted as an ADD on every pass — leaking the
    bridge identifier into local tags.
    """
    jira_snapshot = {
        "PROJ-200": {
            "summary": "S",
            "description": "D",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",
            "labels": [
                "labelprobe",
                "dso-id:local-200",
                "dso-id:jira-dig-200",
                "dso-id-legacy-hyphen",
                "imported:legacy",
            ],
        }
    }
    store = StubBindingStore({"PROJ-200": "local-200"})
    local_tickets = {
        "local-200": {
            "title": "S",
            "description": "D",
            "ticket_type": "task",
            "priority": 2,
            "status": "open",
            "assignee": "",
            "tags": ["labelprobe"],
        }
    }

    result, _suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    # No inbound mutation should emit a ``dso-id*`` add — both colon AND
    # hyphen forms are bridge-internal. The ``imported:`` prefix is also
    # excluded.
    for m in result:
        for lm in m.labels:
            label = lm.get("label", "")
            assert not label.startswith("dso-id:"), (
                f"Inbound differ leaked colon-form dso-id label: {label!r}"
            )
            assert not label.startswith("dso-id-"), (
                f"Inbound differ leaked hyphen-form dso-id label: {label!r}"
            )
            assert not label.startswith("imported:"), (
                f"Inbound differ leaked imported: label: {label!r}"
            )


def test_inbound_label_diff_does_not_remove_local_colon_form_dso_id(
    inbound_differ: ModuleType,
) -> None:
    """When local has a stale ``dso-id:<id>`` tag but Jira lacks it, the
    inbound differ must NOT emit a REMOVE — bridge-internal labels are
    governed by the dso-id label authorization contract, not by inbound
    user-label coordination.
    """
    jira_snapshot = {
        "PROJ-201": {
            "summary": "S",
            "description": "D",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",
            "labels": ["labelprobe"],
        }
    }
    store = StubBindingStore({"PROJ-201": "local-201"})
    local_tickets = {
        "local-201": {
            "title": "S",
            "description": "D",
            "ticket_type": "task",
            "priority": 2,
            "status": "open",
            "assignee": "",
            "tags": ["labelprobe", "dso-id:local-201", "dso-id:jira-dig-201"],
        }
    }

    result, _suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    for m in result:
        for lm in m.labels:
            assert not (
                lm.get("action") == "remove"
                and str(lm.get("label", "")).startswith("dso-id:")
            ), f"Inbound differ emitted spurious REMOVE for dso-id label: {lm}"
