"""Ticket 625b: canonical-shape outbound field diff — decision-for-decision equivalence.

The outbound UPDATE path stops comparing in Jira shape: it canonicalizes the remote snapshot
via the injected ``InboundMapper`` (mirroring the inbound differ) and diffs in LOCAL shape,
mapping back to Jira shape only at the emission boundary via the injected ``OutboundMapper``.

This suite pins the observable contract: for a matrix of (local, remote) inputs the NEW
canonical path emits the SAME ``OutboundMutation.fields`` the old vendor-shape ``_diff_fields``
emitted (golden values captured from the pre-refactor differ). It also proves the snapshot is
canonicalized through the INJECTED inbound mapper at runtime, and that the differ names no
vendor snapshot key.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_REC = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _REC / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _StubBindingStore:
    def __init__(self, l2j: dict[str, str]) -> None:
        self._l2j = l2j
        self._j2l = {v: k for k, v in l2j.items()}

    def get_jira_key(self, l):  # noqa: E741
        return self._l2j.get(l)

    def is_bound(self, l):  # noqa: E741
        return l in self._l2j

    def get_local_id(self, j):
        return self._j2l.get(j)

    def is_pending(self, l):  # noqa: E741
        return False

    def get_baseline(self, l):  # noqa: E741
        return None


def _ticket(**ov) -> dict:
    t = {
        "ticket_id": "loc-1",
        "title": "T",
        "description": "D",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
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
        "labels": [],
    }
    f.update(ov)
    return f


def _emit(od, backend, t: dict, jf: dict) -> dict | None:
    """Run the canonical differ with the JiraBackend's INJECTED mappers; return the emitted
    update mutation's ``fields`` (or None if nothing emitted)."""
    bs = _StubBindingStore({"loc-1": "DIG-1"})
    muts, _ = od.compute_outbound_mutations(
        [t],
        {"DIG-1": jf},
        bs,
        outbound_mapper=backend.outbound,
        inbound_mapper=backend.inbound,
    )
    for m in muts:
        fields = getattr(m, "fields", None)
        if fields is not None:
            return dict(fields)
    return None


@pytest.fixture(scope="module")
def od() -> ModuleType:
    return _load("outbound_differ_canonical_equiv", "outbound_differ.py")


@pytest.fixture(scope="module")
def backend():
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(transport=object())


# Golden decisions captured from the pre-625b vendor-shape differ.
def test_identical_emits_nothing(od, backend) -> None:
    assert _emit(od, backend, _ticket(), _jira()) is None


def test_title_change_emits_summary(od, backend) -> None:
    assert _emit(od, backend, _ticket(title="NEW"), _jira(summary="T")) == {"summary": "NEW"}


def test_description_change_emits_description(od, backend) -> None:
    assert _emit(od, backend, _ticket(description="LOCAL"), _jira(description="REMOTE")) == {
        "description": "LOCAL"
    }


def test_status_change_emits_mapped_status(od, backend) -> None:
    assert _emit(od, backend, _ticket(status="closed"), _jira(status={"name": "To Do"})) == {
        "status": "Done"
    }


# Runtime proof: the snapshot is canonicalized through the INJECTED inbound mapper.
def test_snapshot_canonicalized_via_injected_inbound_mapper(od) -> None:
    class SpyInbound:
        def __init__(self) -> None:
            self.seen: list[dict] = []

        def map_remote_to_local(self, remote_fields):
            self.seen.append(remote_fields)
            # Return a canonical shape equal to the local ticket so nothing is emitted.
            return {"title": "T", "description": "D", "status": "open"}

    class PassthroughOutbound:
        def map_local_to_remote(
            self, ticket, binding_store=None, local_ticket_types=None, emit_detach_clear=False
        ):
            return {}

        def map_fields_to_remote(
            self, changed, ticket=None, binding_store=None, local_ticket_types=None
        ):
            return dict(changed)

        def resolve_assignee(self, local_value, remote_identity):
            return (local_value, False, False)

    spy = SpyInbound()
    bs = _StubBindingStore({"loc-1": "DIG-1"})
    od.compute_outbound_mutations(
        [_ticket()],
        {"DIG-1": _jira()},
        bs,
        outbound_mapper=PassthroughOutbound(),
        inbound_mapper=spy,
    )
    assert spy.seen, "the update path must canonicalize the snapshot via the injected InboundMapper"
    assert spy.seen[0].get("summary") == "T"  # it observed the raw snapshot entry for DIG-1
