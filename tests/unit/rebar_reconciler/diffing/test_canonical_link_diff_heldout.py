"""Ticket eefd (HELD-OUT edge oracle): canonical link diff edges.

Withheld from the implementer: dedup against an existing remote link (either direction),
the unmapped-relation skip, the unknown-remote-type ignore, and the depends_on direction
swap.
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


class _BS:
    def __init__(self, l2j):
        self.l2j = l2j
        self.j2l = {v: k for k, v in l2j.items()}

    def get_jira_key(self, l):  # noqa: E741
        return self.l2j.get(l)

    def get_local_id(self, j):
        return self.j2l.get(j)


def _dep(target, relation, uuid="u1"):
    return {"target_id": target, "relation": relation, "link_uuid": uuid}


def _link(type_name, inward=None, outward=None):
    d = {"type": {"name": type_name}}
    if inward:
        d["inwardIssue"] = {"key": inward}
    if outward:
        d["outwardIssue"] = {"key": outward}
    return d


@pytest.fixture(scope="module")
def backend():
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(transport=object())


@pytest.fixture(scope="module")
def ol() -> ModuleType:
    return _load("outbound_links_canonical_heldout", "outbound_links.py")


def _adds(ol, backend, ticket, jira_fields, bs):
    return ol._diff_links(ticket, jira_fields, bs, backend)


def test_existing_link_dedups_no_emit(ol, backend) -> None:
    """A dep already present remotely (Blocks -> DIG-9) emits nothing (golden: [])."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = {"ticket_id": "loc-1", "deps": [_dep("tgt", "blocks")]}
    out = _adds(ol, backend, ticket, {"issuelinks": [_link("Blocks", outward="DIG-9")]}, bs)
    assert out == []


def test_unmapped_relation_is_skipped(ol, backend) -> None:
    """A relation with no vendor link type (duplicates) emits nothing (golden: [])."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = {"ticket_id": "loc-1", "deps": [_dep("tgt", "duplicates")]}
    assert _adds(ol, backend, ticket, {"issuelinks": []}, bs) == []


def test_unknown_remote_link_type_ignored_for_matching(ol, backend) -> None:
    """An unknown remote link type (no canonical relation) is ignored for matching: it does
    not dedup a local dep, so the dep still emits its ADD."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = {"ticket_id": "loc-1", "deps": [_dep("tgt", "blocks")]}
    out = _adds(ol, backend, ticket, {"issuelinks": [_link("Mentions", outward="DIG-9")]}, bs)
    assert out == [
        {
            "action": "add",
            "type": "Blocks",
            "to_key": "DIG-9",
            "relation": "blocks",
            "swap": False,
            "link_uuid": "u1",
        }
    ]


def test_depends_on_emits_swap_true(ol, backend) -> None:
    """depends_on maps to Jira 'Blocks' with swap=True (direction preserved)."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = {"ticket_id": "loc-1", "deps": [_dep("tgt", "depends_on")]}
    out = _adds(ol, backend, ticket, {"issuelinks": []}, bs)
    assert out == [
        {
            "action": "add",
            "type": "Blocks",
            "to_key": "DIG-9",
            "relation": "depends_on",
            "swap": True,
            "link_uuid": "u1",
        }
    ]


def test_dedup_is_direction_agnostic(ol, backend) -> None:
    """A local blocks->K dep dedups against an INWARD Blocks link from K (not just outward):
    the ADD dedup matches on (vendor_type, remote_key) regardless of direction, preserving
    today's _existing_jira_links semantics (golden: [])."""
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = {"ticket_id": "loc-1", "deps": [_dep("tgt", "blocks")]}
    out = _adds(ol, backend, ticket, {"issuelinks": [_link("Blocks", inward="DIG-9")]}, bs)
    assert out == []
