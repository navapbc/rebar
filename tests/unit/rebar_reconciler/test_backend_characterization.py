"""HELD-OUT characterization of JiraBackend delegation (S2, epic bbf1).

Byte-for-byte pins on the CURRENT outputs of the pure Jira mappers/sanitizers,
asserting that ``JiraBackend``'s role Protocols delegate to them with ZERO behaviour
change. The expected values were captured from the pre-story ``main`` functions
(``outbound_fields._map_local_to_jira_fields``, ``inbound_fields._map_jira_to_local_fields``,
``adapters/jira/jira_fields`` sanitizers). A delegation that drops or rewires a map
fails here.

This file is HELD OUT from the implementation subagent — it is the oracle that proves
the delegation is faithful, not a spec the implementer codes against.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.adapters.jira.backend import JiraBackend

from .backend_support import FakeTransport


def _backend() -> JiraBackend:
    return JiraBackend(transport=FakeTransport())


def test_outbound_map_is_byte_for_byte_jira_fields():
    ticket = {
        "ticket_id": "abc1-2345-6789-0abc",
        "title": "Add widget",
        "description": "Body text",
        "ticket_type": "story",
        "priority": 1,
        "status": "in_progress",
        "assignee": "me@example.com",
    }
    assert _backend().outbound.map_local_to_remote(ticket, None) == {
        "summary": "Add widget",
        "description": "Body text",
        "issuetype": "Story",
        "priority": "High",
        "status": "In Progress",
        "assignee": "me@example.com",
    }


def test_inbound_map_is_byte_for_byte_local_fields():
    jira_fields = {
        "summary": "Add widget",
        "description": "Body text",
        "issuetype": {"name": "Story"},
        "priority": {"name": "High"},
        "status": {"name": "In Progress"},
        "assignee": {"displayName": "Me", "emailAddress": "me@example.com"},
    }
    assert _backend().inbound.map_remote_to_local(jira_fields) == {
        "title": "Add widget",
        "description": "Body text",
        "ticket_type": "story",
        "priority": 1,
        "assignee": "Me",
        # ticket 625b: additive canonical identity key emitted when ``assignee`` is
        # present, so the core never reads the raw Jira assignee shape.
        "assignee_identity": {"display": "Me", "email": "me@example.com", "account_id": None},
        "status": "in_progress",
    }


def test_outbound_priority_value_map_pins_all_levels():
    b = _backend()
    expected = {0: "Highest", 1: "High", 2: "Medium", 3: "Low", 4: "Lowest"}
    for local_pri, jira_name in expected.items():
        remote = b.outbound.map_local_to_remote({"title": "t", "priority": local_pri}, None)
        assert remote["priority"] == jira_name


def test_outbound_status_value_map_pins_all_states():
    b = _backend()
    expected = {
        "idea": "IDEA",
        "open": "To Do",
        "in_progress": "In Progress",
        "closed": "Done",
        "blocked": "In Progress",
        "cancelled": "Done",
    }
    for local_status, jira_state in expected.items():
        remote = b.outbound.map_local_to_remote({"title": "t", "status": local_status}, None)
        assert remote["status"] == jira_state


def test_sanitize_summary_truncates_to_254_with_suffix():
    out = _backend().sanitizer.sanitize_summary("x" * 300)
    assert len(out) == 254
    assert out.endswith(" [truncated]")
    assert out.startswith("x" * 100)


def test_sanitize_label_strips_and_returns_clean_token():
    assert _backend().sanitizer.sanitize_label("  rebar-id:foo  ") == "rebar-id:foo"


def test_sanitize_short_values_pass_through_unchanged():
    b = _backend()
    assert b.sanitizer.sanitize_summary("Fine") == "Fine"


# ---------------------------------------------------------------------------
# J1 (rebar-ticket acb2-1823-2f0d-415b) — extraction oracles.
#
# The pins below freeze the CURRENT output of every unit that the Jira-family
# extraction (J2) either relocates to ``adapters/jira_family/`` or re-points at a
# ``jira_family`` contract. They are golden-value assertions on purpose: their whole
# job is to fail if a byte of observable output changes during a move that is
# supposed to change nothing. Values were captured from this checkout's
# ``adapters/jira/`` before any code moved.
# ---------------------------------------------------------------------------


# --- map_fields_to_remote: every branch, including both value-map defaults --------


def test_map_fields_to_remote_renames_title_to_summary():
    assert _backend().outbound.map_fields_to_remote({"title": "New title"}) == {
        "summary": "New title"
    }


def test_map_fields_to_remote_maps_every_status_plus_unmapped_default():
    b = _backend()
    for local_status, jira_state in {
        "idea": "IDEA",
        "open": "To Do",
        "in_progress": "In Progress",
        "closed": "Done",
        "blocked": "In Progress",
        "cancelled": "Done",
    }.items():
        assert b.outbound.map_fields_to_remote({"status": local_status}) == {"status": jira_state}
    # Map-or-drift (S2): an unmapped local status is OMITTED entirely (Jira left
    # unchanged), never coerced to "To Do".
    assert b.outbound.map_fields_to_remote({"status": "no_such_status"}) == {}


def test_map_fields_to_remote_maps_every_priority_plus_unmapped_default():
    b = _backend()
    for local_priority, jira_name in {
        0: "Highest",
        1: "High",
        2: "Medium",
        3: "Low",
        4: "Lowest",
    }.items():
        assert b.outbound.map_fields_to_remote({"priority": local_priority}) == {
            "priority": jira_name
        }
    assert b.outbound.map_fields_to_remote({"priority": 99}) == {"priority": "Medium"}


def test_map_fields_to_remote_normalizes_and_fits_description():
    # A short description round-trips through fit_text_to_adf_limit + normalize_description
    # unchanged — pinning that the ADF composition stays a fixed point for simple prose.
    assert _backend().outbound.map_fields_to_remote({"description": "Body text"}) == {
        "description": "Body text"
    }


def test_map_fields_to_remote_passes_non_string_description_through_untouched():
    # The ``isinstance(value, str)`` guard: a None description must NOT reach the ADF
    # encoder. Pinned because an extraction that drops the guard raises instead.
    assert _backend().outbound.map_fields_to_remote({"description": None}) == {"description": None}


def test_map_fields_to_remote_passes_resolved_fields_through_by_own_name():
    assert _backend().outbound.map_fields_to_remote(
        {
            "assignee": "6270abc",
            "parent": "REB-1",
            "reporter": "someone",
            "_assignee_is_account_id": True,
        }
    ) == {
        "assignee": "6270abc",
        "parent": "REB-1",
        "reporter": "someone",
        "_assignee_is_account_id": True,
    }


def test_map_fields_to_remote_on_empty_changed_dict_is_empty():
    assert _backend().outbound.map_fields_to_remote({}) == {}


# --- sanitizers: at, below, and above the limits, plus the invalid-label raises ----


def test_sanitize_summary_at_inclusive_limit_is_untruncated():
    # 254 is the INCLUSIVE max (Jira rejects 255) — the off-by-one the source
    # documents, and the exact boundary an extraction could shift.
    out = _backend().sanitizer.sanitize_summary("x" * 254)
    assert out == "x" * 254
    assert not out.endswith(" [truncated]")


def test_sanitize_summary_one_over_limit_truncates():
    out = _backend().sanitizer.sanitize_summary("x" * 255)
    assert len(out) == 254
    assert out.endswith(" [truncated]")


def test_sanitize_label_at_inclusive_limit_passes():
    # Label's inclusive max is 255 (not-more-than), deliberately one different
    # from summary's 254.
    label = "y" * 255
    assert _backend().sanitizer.sanitize_label(label) == label


def test_sanitize_label_raises_on_every_rejection_reason():
    b = _backend()
    for bad in (
        "with space",  # internal whitespace
        "has,comma",  # comma
        "   ",  # empty after strip
        "z" * 256,  # one over the 255 inclusive limit
    ):
        with pytest.raises(ValueError):
            b.sanitizer.sanitize_label(bad)


# (The ``sanitize_comment`` golden pins that sat here were removed with the dead
# code they characterized: Cloud's caller-less ``_JiraSanitizer.sanitize_comment``
# was deleted by bug b9b4-f460-2d54-4872 — the REAL Cloud send fit lives in
# ``acli_cli_ops.add_comment`` and is pinned by
# ``diffing/test_comment_dedup_key_matches_send_fit.py``.)


def test_sanitize_description_short_value_passes_through():
    assert _backend().sanitizer.sanitize_description("Body text") == "Body text"


# --- identity convention: format / parse / predicate, incl. the legacy form -------


def test_identity_format_label_uses_canonical_colon_form():
    assert _backend().identity.format_label("abc1-2345-6789-0abc") == "rebar-id:abc1-2345-6789-0abc"


def test_identity_parse_label_accepts_colon_and_legacy_hyphen_forms():
    ident = _backend().identity
    assert ident.parse_label("rebar-id:abc1-2345") == "abc1-2345"
    # The legacy hyphen read form must keep working — dropping it silently orphans
    # every binding written before the canonical form.
    assert ident.parse_label("rebar-id-abc1-2345") == "abc1-2345"


def test_identity_parse_label_rejects_non_identity_and_empty_remainder():
    ident = _backend().identity
    assert ident.parse_label("sprint-42") is None
    assert ident.parse_label("rebar-id:") is None
    assert ident.parse_label("rebar-id:   ") is None


def test_identity_is_identity_label_tracks_parse():
    ident = _backend().identity
    assert ident.is_identity_label("rebar-id:abc1") is True
    assert ident.is_identity_label("rebar-id-abc1") is True
    assert ident.is_identity_label("rebar-id:") is False
    assert ident.is_identity_label("other") is False


# --- link relation vocabulary + inbound direction, for every relation -------------


def test_relation_to_jira_link_vocabulary_pinned():
    from rebar_reconciler.adapters.jira_family import (
        RELATION_TO_JIRA_LINK as _RELATION_TO_JIRA_LINK,
    )

    assert _RELATION_TO_JIRA_LINK == {
        "blocks": ("Blocks", False),
        "depends_on": ("Blocks", True),
        "relates_to": ("Relates", False),
    }
    # Relations with no reliable Jira link type stay ABSENT (the differ skips them).
    for absent in ("duplicates", "supersedes", "discovered_from"):
        assert absent not in _RELATION_TO_JIRA_LINK


def test_resolve_inbound_link_pins_direction_for_every_relation():
    from rebar_reconciler.link_direction import resolve_inbound_link

    # outward Blocks == X blocks Y
    assert resolve_inbound_link({"type": {"name": "Blocks"}, "outwardIssue": {"key": "REB-2"}}) == (
        "REB-2",
        "blocks",
    )
    # inward Blocks == X is blocked by Y -> the INVERSE relation
    assert resolve_inbound_link({"type": {"name": "Blocks"}, "inwardIssue": {"key": "REB-3"}}) == (
        "REB-3",
        "depends_on",
    )
    # Relates is symmetric: same relation from either side
    assert resolve_inbound_link(
        {"type": {"name": "Relates"}, "outwardIssue": {"key": "REB-4"}}
    ) == ("REB-4", "relates_to")
    assert resolve_inbound_link({"type": {"name": "Relates"}, "inwardIssue": {"key": "REB-5"}}) == (
        "REB-5",
        "relates_to",
    )
    # unmapped link type and malformed entries both yield (None, None)
    assert resolve_inbound_link(
        {"type": {"name": "Duplicate"}, "outwardIssue": {"key": "REB-6"}}
    ) == (None, None)
    assert resolve_inbound_link({"type": {"name": "Blocks"}}) == (None, None)
    assert resolve_inbound_link({}) == (None, None)
