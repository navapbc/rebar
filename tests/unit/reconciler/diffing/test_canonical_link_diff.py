"""The SupportsLinks port maps vendor links into canonical relations and back to wire
payloads. These tests preserve the outbound ADD payload and relation-based
comparison.

Held-out oracles merged from test_canonical_link_diff_heldout: Held-out link-edge oracle:
deduplicate either direction, skip unmapped relations, ignore unknown remote types, and swap
depends_on direction.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit


_REC = Path(__file__).resolve().parents[4] / "src" / "rebar" / "reconciler"


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
    from rebar.reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(transport=object())


@pytest.fixture(scope="module")
def ol() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_links")


def test_link_payload_for_relation_blocks(backend) -> None:
    assert backend.link_payload_for_relation("blocks") == ("Blocks", False)


def test_link_payload_for_relation_depends_on_swaps(backend) -> None:
    assert backend.link_payload_for_relation("depends_on") == ("Blocks", True)


def test_link_payload_for_relation_unmapped_is_none(backend) -> None:
    assert backend.link_payload_for_relation("duplicates") is None


def test_map_remote_links_canonicalizes_a_blocks_link(backend) -> None:
    canonical = set(backend.map_remote_links({"issuelinks": [_link("Blocks", outward="DIG-9")]}))
    assert ("blocks", "DIG-9", "Blocks") in canonical


def test_diff_links_emits_add_with_todays_payload(ol, backend) -> None:
    bs = _BS({"loc-1": "DIG-1", "tgt": "DIG-9"})
    ticket = {"ticket_id": "loc-1", "deps": [_dep("tgt", "blocks")]}
    out = ol._diff_links(ticket, {"issuelinks": []}, bs, backend)
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


# ── held-out oracles (merged from test_canonical_link_diff_heldout) ──


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
