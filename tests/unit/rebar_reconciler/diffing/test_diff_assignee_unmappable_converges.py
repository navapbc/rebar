"""Bug 9b94: an unmappable local assignee converges (no per-pass churn).

Local tickets store assignees that may be agent identities (`claude`,
`config-loop`) which are not Jira users. Previously the outbound differ compared
the raw local string to Jira's (unassigned) state — a permanent mismatch — and
re-emitted an assignee update EVERY pass; the applier then soft-failed the assign,
Jira stayed unassigned, and the loop never converged.

The fix resolves the local assignee to a Jira account. A local assignee that maps
to NO assignable user means "desired = unassigned", so an already-unassigned Jira
issue is a MATCH and the differ stops re-emitting. A resolvable assignee is still
synced. With no resolver (no client / fixture path) the legacy permissive string
match is preserved.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"


def _load_differ() -> ModuleType:
    spec = importlib.util.spec_from_file_location("outbound_differ_unmappable_test", DIFFER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["outbound_differ_unmappable_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def differ() -> ModuleType:
    return _load_differ()


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
# unmappable (None, authoritative).
def _resolver(assignee: str, jira_key: str):
    if not assignee:
        return ("", True)
    if assignee == "alice":
        return ("acct-alice", True)
    return (None, True)  # unmappable → desired unassigned


# --- the core fix: unmappable assignee + unassigned Jira → no emit -----------


def test_unmappable_assignee_converges_when_jira_unassigned(differ):
    changed = differ._diff_fields(
        _ticket("claude"), _jira(None), assignee_resolver=_resolver, jira_key="REB-1"
    )
    assert "assignee" not in changed, (
        f"unmappable assignee with an already-unassigned Jira issue must NOT "
        f"re-emit (convergence); got {changed!r}"
    )


def test_unmappable_assignee_unassigns_when_jira_has_someone(differ):
    jira = _jira({"accountId": "acct-bob", "displayName": "Bob"})
    changed = differ._diff_fields(
        _ticket("claude"), jira, assignee_resolver=_resolver, jira_key="REB-1"
    )
    assert changed.get("assignee") == "", "unmappable local → unassign the Jira issue"


def test_mappable_assignee_emitted_when_jira_unassigned(differ):
    changed = differ._diff_fields(
        _ticket("alice"), _jira(None), assignee_resolver=_resolver, jira_key="REB-1"
    )
    assert changed.get("assignee") == "alice", "a mappable assignee is still synced"


def test_mappable_assignee_converges_when_already_assigned(differ):
    jira = _jira({"accountId": "acct-alice", "displayName": "Alice"})
    changed = differ._diff_fields(
        _ticket("alice"), jira, assignee_resolver=_resolver, jira_key="REB-1"
    )
    assert "assignee" not in changed, "already-correct assignee must not re-emit"


def test_no_resolver_preserves_legacy_string_match(differ):
    # Without a resolver, behaviour is the permissive string match: local "claude"
    # vs unassigned still fires (unchanged legacy behaviour — the fix is opt-in via
    # the resolver that the live pass supplies).
    changed = differ._diff_fields(_ticket("claude"), _jira(None))
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
