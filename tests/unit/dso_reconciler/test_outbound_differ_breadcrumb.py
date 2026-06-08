"""Behavioral tests for the outbound-update CHANGED-FIELD breadcrumb.

Bug 57d1-7d11-4f62-4a58 / sync-hardening proposal P5: when
``compute_outbound_mutations`` emits an outbound UPDATE for a bound key whose
fields diverge from Jira, it must log a one-line breadcrumb to stderr naming
the CHANGED FIELDS (names only, never values) so a residual non-converging
field is visible in CI logs without live Jira credentials.

Reuses the StubBindingStore / _make_ticket fixtures from the sibling
test_outbound_differ module to avoid duplicating the loader boilerplate.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "outbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load_module("outbound_differ", OUTBOUND_DIFFER_PATH)


class StubBindingStore:
    """In-memory binding store (mirrors test_outbound_differ.StubBindingStore)."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


def _make_ticket(
    ticket_id: str = "abc-1234",
    title: str = "Fix the widget",
    description: str = "It is broken",
    status: str = "open",
    priority: int = 2,
    ticket_type: str = "bug",
    assignee: str = "alice",
    tags: list[str] | None = None,
    comments: list[dict] | None = None,
    deps: list[str] | None = None,
) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "ticket_type": ticket_type,
        "assignee": assignee,
        "tags": tags or [],
        "comments": comments or [],
        "deps": deps or [],
    }


def _jira_fields(
    summary: str = "Fix the widget",
    description: str = "It is broken",
    status: str = "To Do",
    priority: str = "Medium",
    assignee: str = "alice",
) -> dict:
    return {
        "summary": summary,
        "description": description,
        "status": {"name": status},
        "priority": {"name": priority},
        "assignee": {"displayName": assignee},
        "labels": [],
    }


def test_outbound_update_logs_changed_field_names(
    outbound_differ: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A field diff on a bound key logs `RECON: outbound_update key=... changed=[...]`.

    DIG-5347 in the snapshot, local description differs from Jira -> the
    differ emits one outbound update and one breadcrumb naming `description`.
    """
    ticket = _make_ticket(
        ticket_id="local-1",
        description="A new and different description",
    )
    store = StubBindingStore({"local-1": "DIG-5347"})
    snapshot = {"DIG-5347": _jira_fields(description="The stale Jira description")}

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    assert len(result) == 1
    assert result[0].action == "update"

    captured = capsys.readouterr()
    assert "RECON: outbound_update key=DIG-5347" in captured.err
    assert "changed=[" in captured.err
    # Field NAME present...
    assert "description" in captured.err
    # ...and the VALUE absent (names only — values may be large/sensitive).
    assert "A new and different description" not in captured.err
    assert "The stale Jira description" not in captured.err


def test_breadcrumb_lists_multiple_changed_fields_sorted(
    outbound_differ: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Multiple diverging fields appear in one sorted, comma-joined breadcrumb."""
    ticket = _make_ticket(
        ticket_id="local-2",
        title="Updated title",
        description="Updated body",
        priority=0,  # -> "Highest", diverges from Jira "Medium"
    )
    store = StubBindingStore({"local-2": "DIG-5382"})
    snapshot = {
        "DIG-5382": _jira_fields(
            summary="Old title",
            description="Old body",
            priority="Medium",
        )
    }

    outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    err = capsys.readouterr().err
    assert "RECON: outbound_update key=DIG-5382" in err
    # sorted: description, priority, summary
    assert "changed=[description,priority,summary]" in err


def test_no_breadcrumb_when_fields_converge(
    outbound_differ: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bound key whose fields all match Jira emits no update and no breadcrumb."""
    ticket = _make_ticket(ticket_id="local-3")
    store = StubBindingStore({"local-3": "DIG-5383"})
    snapshot = {"DIG-5383": _jira_fields()}

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    assert result == []
    assert "RECON: outbound_update" not in capsys.readouterr().err


def test_breadcrumb_reports_comment_and_label_counts(
    outbound_differ: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A label-only update (no scalar field diff) still logs a breadcrumb with
    `changed=[]` plus the comment/label counts — so a comment/label re-emitter
    (bug 57d1: in-window keys re-emitting with an empty field diff) is visible,
    not silently suppressed."""
    ticket = _make_ticket(ticket_id="local-4", tags=["urgent"])
    store = StubBindingStore({"local-4": "DIG-5529"})
    # Fields all match; only the label set diverges (local 'urgent' not in Jira).
    snapshot = {"DIG-5529": _jira_fields()}

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    assert len(result) == 1
    assert result[0].action == "update"
    err = capsys.readouterr().err
    assert "RECON: outbound_update key=DIG-5529" in err
    assert "changed=[]" in err  # no scalar field diff
    assert "labels=1" in err  # the label re-emit IS visible
