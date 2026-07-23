"""Ticket eefd: canonical link diff behind SupportsLinks (happy path).

The outbound link differ stops comparing in Jira shape. Two translations move onto the
``SupportsLinks`` capability (adapter-side, translation only):

* ``map_remote_links(remote_fields)`` -> the canonical link set: entries
  ``(relation, remote_key, opaque_vendor_type)`` (absorbs today's ``_existing_jira_links``
  + its direction-agnostic dedup; unknown vendor types -> ``relation=None``).
* ``link_payload_for_relation(relation)`` -> ``(vendor_type, swap)`` (or None if unmapped).

Core ``_diff_links`` then compares local ``ticket["deps"]`` against the canonical set in
RELATION vocabulary, taking the SupportsLinks capability as its 4th argument. Emitted
ADD/REMOVE payloads are unchanged.

Happy-path oracle: the port members, and an ADD emitted with today's exact payload.
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
    return _load("outbound_links_canonical", "outbound_links.py")


# ── link_payload_for_relation (emission translation) ─────────────────────────
def test_link_payload_for_relation_blocks(backend) -> None:
    assert backend.link_payload_for_relation("blocks") == ("Blocks", False)


def test_link_payload_for_relation_depends_on_swaps(backend) -> None:
    assert backend.link_payload_for_relation("depends_on") == ("Blocks", True)


def test_link_payload_for_relation_unmapped_is_none(backend) -> None:
    assert backend.link_payload_for_relation("duplicates") is None


# ── map_remote_links (canonical set) ─────────────────────────────────────────
def test_map_remote_links_canonicalizes_a_blocks_link(backend) -> None:
    canonical = set(backend.map_remote_links({"issuelinks": [_link("Blocks", outward="DIG-9")]}))
    assert ("blocks", "DIG-9", "Blocks") in canonical


# ── _diff_links ADD golden (canonical path, capability injected) ─────────────
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
