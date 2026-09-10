"""Characterize Jira Data Center with literal values, not structural shapes.

The Cloud suite binds a different backend, while the shared contract tests would
accept wrong-but-well-formed values. These tests therefore pin DC mappings, limits,
identity formats, and rich-text results directly. Vendor differences are explicit:
for example, DC fits descriptions through ``WikiTextCodec`` where Cloud does not.
Keeping a sibling module preserves readable Cloud literals, leaves that suite
untouched, and avoids combining two large modules beyond the size gate. Tests named
``..._diverges_from_cloud`` intentionally reject accidental parity.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.adapters.jira_datacenter.backend import (
    JiraDataCenterBackend,
    _map_local_to_dc_fields,
)
from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec
from rebar_reconciler.outbound_comments import _decorate_outbound_comment

from .backend_support import FakeTransport

# Mutation ledger (cedc-58d1-f6d1-428e, AC8). Each perturbation failed by
# assertion in the named value/boundary pins:
# - priority ``High`` -> ``Higher`` and status ``In Progress`` -> ``In progress``:
#   create-path, exhaustive-map, and reverse-map tests;
# - summary 254 -> 253 and label 255 -> 254: inclusive-limit/token tests;
# - story ``Story`` -> ``Storey``: create and exhaustive issue-type tests;
# - assignee ``get(...) or ""`` -> ``get(..., "")``: explicit-None coercion;
# - description fit -> passthrough: WikiText fitting and Cloud-divergence tests;
# - wiki limit 32767 -> 32766: literal constants plus all inclusive-limit pins;
# - suffix ``… [truncated by reconciler]`` -> plural: exact truncation pins;
# - both non-string guards removed: non-string description preservation;
# - status default ``To Do`` -> ``Todo``: status/default mapping;
# - identity prefix ``rebar-id:`` -> ``rebar-id=``: canonical label format;
# - resolved ``Cancelled`` -> ``Canceled``: the now-retired default-status pin.
# The wiki-limit mutation initially survived because tests imported the production
# boundary; module-local ``WIKI_LIMIT`` now kills it. Either non-string guard alone is
# redundant, so their observable mutation deliberately removes both.

#: DC's plain-character rich-text cap and the exact marker ``WikiTextCodec``
#: appends to a truncated value, both spelled out as LITERALS rather than
#: imported from the code under test.
#:
#: This is load-bearing, and the first mutation check proved it: an earlier draft
#: built its at-limit inputs from the imported ``WIKI_LIMIT``, so
#: moving that constant by one character moved the test's own boundary with it and
#: every at-limit pin SURVIVED the mutation. A pin that reads its expectation out
#: of the value it is pinning cannot fail.
WIKI_LIMIT = 32767
WIKI_SUFFIX = " … [truncated by reconciler]"


def _backend() -> JiraDataCenterBackend:
    """DC's ``_backend()`` seam, mirroring the Cloud module's."""
    return JiraDataCenterBackend(transport=FakeTransport())


# ---------------------------------------------------------------------------
# AC1 — the create path (``_map_local_to_dc_fields``), pinned by VALUE for every
# key it emits, including each lookup's DEFAULT-fallback branch.
# ---------------------------------------------------------------------------


def test_dc_create_path_maps_every_key_by_value():
    ticket = {
        "ticket_id": "abc1-2345-6789-0abc",
        "title": "Add widget",
        "description": "Body text",
        "ticket_type": "story",
        "priority": 1,
        "status": "in_progress",
        # DC's user identity is the ``name`` username, never Cloud's accountId.
        "assignee": "jsmith",
    }
    assert _backend().outbound.map_local_to_remote(ticket, None) == {
        "summary": "Add widget",
        "description": "Body text",
        "issuetype": "Story",
        "priority": "High",
        "status": "In Progress",
        "assignee": "jsmith",
    }


def test_dc_create_path_emits_no_project_key():
    # The project is threaded separately (``JiraDataCenterBackend.project`` ->
    # ``JiraDataCenterTransport.project``, which ``create_issue`` setdefaults into
    # the payload). A mapper that started emitting one would double-write it.
    emitted = _map_local_to_dc_fields({"title": "t"})
    assert "project" not in emitted
    assert set(emitted) == {
        "summary",
        "description",
        "issuetype",
        "priority",
        "status",
        "assignee",
    }


def test_dc_create_path_defaults_on_a_bare_ticket():
    # Every lookup's DEFAULT branch at once, plus both empty-string fallbacks.
    assert _map_local_to_dc_fields({}) == {
        "summary": "",
        "description": "",
        "issuetype": "Task",
        "priority": "Medium",
        "status": "To Do",
        "assignee": "",
    }


def test_dc_create_path_defaults_on_unmapped_values():
    # Present-but-unmapped is a DIFFERENT branch from missing: it exercises the
    # map-or-drift fallback of each value axis rather than the ticket's.
    # Map-or-drift (S2/S5): an unmapped status AND an unmapped priority are OMITTED
    # entirely, never coerced. (issuetype is mandatory on a create, so it still
    # defaults to "Task".)
    assert _map_local_to_dc_fields(
        {"title": "t", "ticket_type": "no_such_type", "priority": 99, "status": "no_such_status"}
    ) == {
        "summary": "t",
        "description": "",
        "issuetype": "Task",
        "assignee": "",
    }


def test_dc_create_path_coerces_explicit_none_to_empty_string():
    # ``.get(key) or ""`` (not ``.get(key, "")``): an explicit ``None`` — which the
    # ticket reducer writes for an unassigned ticket — must normalise to "" rather
    # than propagate and become the literal string "None" at the wire boundary.
    assert _map_local_to_dc_fields({"title": None, "description": None, "assignee": None}) == {
        "summary": "",
        "description": "",
        "issuetype": "Task",
        "priority": "Medium",
        "status": "To Do",
        "assignee": "",
    }


def test_dc_create_path_issuetype_map_pins_every_type():
    for local_type, dc_name in {
        "task": "Task",
        "story": "Story",
        "bug": "Bug",
        "epic": "Epic",
    }.items():
        assert _map_local_to_dc_fields({"ticket_type": local_type})["issuetype"] == dc_name


def test_dc_create_path_priority_map_pins_every_level():
    for local_priority, dc_name in {
        0: "Highest",
        1: "High",
        2: "Medium",
        3: "Low",
        4: "Lowest",
    }.items():
        assert _map_local_to_dc_fields({"priority": local_priority})["priority"] == dc_name


def test_dc_create_path_status_map_pins_every_state():
    for local_status, dc_state in {
        "idea": "IDEA",
        "open": "To Do",
        "in_progress": "In Progress",
        "closed": "Done",
        "blocked": "In Progress",
        "cancelled": "Done",
    }.items():
        assert _map_local_to_dc_fields({"status": local_status})["status"] == dc_state


def test_dc_create_path_fits_description_through_the_wiki_codec():
    text = "d" * (WIKI_LIMIT + 1)
    fitted = _map_local_to_dc_fields({"description": text})["description"]
    assert fitted == WikiTextCodec().fit_outbound(text)
    assert len(fitted) == WIKI_LIMIT
    assert fitted.endswith(WIKI_SUFFIX)


def test_dc_create_path_description_fit_diverges_from_cloud():
    # DIVERGENCE, asserted rather than assumed: DC's create path fits the
    # description through ``WikiTextCodec``; Cloud's ``_map_local_to_jira_fields``
    # passes it through untouched (Cloud fits on the send path, not here). A
    # change that made either side match the other must go red.
    from rebar_reconciler.adapters.jira.outbound_fields import _map_local_to_jira_fields

    text = "d" * (WIKI_LIMIT + 1)
    dc_value = _map_local_to_dc_fields({"description": text})["description"]
    cloud_value = _map_local_to_jira_fields({"description": text})["description"]
    assert cloud_value == text
    assert dc_value != cloud_value
    assert len(dc_value) == WIKI_LIMIT


def test_dc_create_path_leaves_an_at_limit_description_untouched():
    text = "d" * WIKI_LIMIT
    assert _map_local_to_dc_fields({"description": text})["description"] == text


def test_dc_rich_text_constants_pinned():
    """The two values every boundary pin below is written against.

    Asserted here so a change to either constant fails ONE obvious test as well
    as the boundary pins, instead of quietly relocating their expectations.
    """
    from rebar_reconciler.adapters.jira_family import rich_text

    assert rich_text.WIKI_DESCRIPTION_LIMIT == WIKI_LIMIT
    assert rich_text._WIKI_TRUNCATION_SUFFIX == WIKI_SUFFIX


# ---------------------------------------------------------------------------
# AC2 — both DC sanitizer boundaries, AT the limit and ONE OVER, including the
# truncation suffix marker, for summary / description / comment.
# ---------------------------------------------------------------------------


def test_dc_sanitize_summary_at_inclusive_limit_is_untruncated():
    out = _backend().sanitizer.sanitize_summary("x" * 254)
    assert out == "x" * 254
    assert not out.endswith(" [truncated]")


def test_dc_sanitize_summary_one_over_limit_truncates_with_marker():
    out = _backend().sanitizer.sanitize_summary("x" * 255)
    assert len(out) == 254
    assert out.endswith(" [truncated]")
    assert out == "x" * (254 - len(" [truncated]")) + " [truncated]"


def test_dc_sanitize_description_at_inclusive_limit_is_untruncated():
    body = "d" * WIKI_LIMIT
    assert _backend().sanitizer.sanitize_description(body) == body


def test_dc_sanitize_description_one_over_limit_truncates_with_marker():
    out = _backend().sanitizer.sanitize_description("d" * (WIKI_LIMIT + 1))
    assert len(out) == WIKI_LIMIT
    assert out.endswith(WIKI_SUFFIX)
    assert out == "d" * (WIKI_LIMIT - len(WIKI_SUFFIX)) + WIKI_SUFFIX


def test_dc_sanitize_comment_at_inclusive_limit_is_untruncated():
    body = "c" * WIKI_LIMIT
    assert _backend().sanitizer.sanitize_comment(body) == body


def test_dc_sanitize_comment_one_over_limit_truncates_with_marker():
    out = _backend().sanitizer.sanitize_comment("c" * (WIKI_LIMIT + 1))
    assert len(out) == WIKI_LIMIT
    assert out.endswith(WIKI_SUFFIX)
    assert out == "c" * (WIKI_LIMIT - len(WIKI_SUFFIX)) + WIKI_SUFFIX


def test_dc_description_boundary_diverges_from_cloud():
    # DIVERGENCE, asserted rather than assumed. DC's description limit is
    # WIKI_LIMIT PLAIN characters; Cloud's is measured on the
    # serialized ADF document (a smaller effective plain-text budget). At exactly
    # DC's limit, DC passes the value through and Cloud truncates it — so a change
    # that accidentally bound Cloud's fit to the DC sanitizer goes red here.
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    body = "d" * WIKI_LIMIT
    dc_out = _backend().sanitizer.sanitize_description(body)
    cloud_out = JiraBackend(transport=FakeTransport()).sanitizer.sanitize_description(body)
    assert dc_out == body
    assert len(cloud_out) < WIKI_LIMIT
    assert cloud_out.endswith(WIKI_SUFFIX)


def test_dc_sanitize_label_pins_the_shared_token_rules():
    # Labels are Jira-family-general; DC binds the SHARED sanitizer, so parity
    # with Cloud here is CORRECT and pinning the values keeps that true.
    sanitizer = _backend().sanitizer
    assert sanitizer.sanitize_label("  rebar-id:foo  ") == "rebar-id:foo"
    assert sanitizer.sanitize_label("y" * 255) == "y" * 255
    for bad in ("with space", "has,comma", "   ", "z" * 256):
        with pytest.raises(ValueError):
            sanitizer.sanitize_label(bad)


def test_dc_fit_comment_is_the_send_path_fit():
    # ``_DCSanitizer.fit_comment`` is the differ-side comparison transform; it must
    # be exactly the marker-stripped body the SEND path lands (decorate →
    # ``sanitize_comment`` fitting through ``fit_preserving_marker`` → strip the
    # decoration; bug b9b4-f460-2d54-4872), or the diff never converges. The
    # expectation is DERIVED from the send composition, not from any bare fitter.
    body = "c" * (WIKI_LIMIT + 1)
    sanitizer = _backend().sanitizer
    decoration = _decorate_outbound_comment("")
    landed = sanitizer.sanitize_comment(_decorate_outbound_comment(body))
    assert landed.endswith(decoration)
    assert sanitizer.fit_comment(body) == landed[: len(landed) - len(decoration)]


# AC3 drives the shared mapper through ``WikiTextCodec`` and asserts exact fitting.
# It deliberately does not assert call order: normalization is identity on DC, so
# swapping normalization and fitting is unobservable; ``AdfCodec`` owns that pin.


def test_dc_map_fields_to_remote_fits_description_to_the_wiki_value():
    value = "d" * (WIKI_LIMIT + 1)
    out = _backend().outbound.map_fields_to_remote({"description": value})
    assert out == {"description": WikiTextCodec().fit_outbound(value)}
    assert len(out["description"]) == WIKI_LIMIT
    assert out["description"].endswith(WIKI_SUFFIX)


def test_dc_map_fields_to_remote_leaves_an_at_limit_description_untouched():
    value = "d" * WIKI_LIMIT
    assert _backend().outbound.map_fields_to_remote({"description": value}) == {
        "description": value
    }


def test_dc_map_fields_to_remote_description_diverges_from_cloud():
    # DIVERGENCE: the SAME shared mapper body, the SAME input, two codecs. At
    # exactly DC's limit the wiki fit is a no-op while the ADF fit truncates —
    # proof the codec really is the injected parameter and not a shared constant.
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    value = "d" * WIKI_LIMIT
    dc_out = _backend().outbound.map_fields_to_remote({"description": value})["description"]
    cloud_out = JiraBackend(transport=FakeTransport()).outbound.map_fields_to_remote(
        {"description": value}
    )["description"]
    assert dc_out == value
    assert cloud_out != value
    assert len(cloud_out) < WIKI_LIMIT


def test_dc_map_fields_to_remote_passes_non_string_description_untouched():
    """A NON-``str`` description comes back UNTOUCHED — the same object, not an
    equal one — so a "helpful" coercion cannot slip through.

    TWO guards produce this outcome on the DC path and they are REDUNDANT: the
    mapper's own ``isinstance(value, str)`` branch, and
    ``WikiTextCodec.fit_outbound``'s non-``str`` early return. The mutation check
    proved the redundancy is real — neither guard can be killed alone. Removing the
    mapper's branch leaves the behaviour intact (the codec still guards, and
    ``normalize_outbound`` is the identity); making the codec coerce leaves it
    intact too (the mapper's branch short-circuits before the codec is ever
    called). The valid mutation therefore removes BOTH, and it is recorded that way
    in the ledger. This is a fact about the code worth writing down: on the DC path
    the mapper's guard is defence in depth, not the sole protection.

    The ``is`` assertions are what make the mutation observable at all; an ``==``
    comparison against ``None`` would still pass against the string ``"None"``.
    """
    outbound = _backend().outbound
    sentinel_dict = {"type": "doc"}
    assert outbound.map_fields_to_remote({"description": None})["description"] is None
    assert outbound.map_fields_to_remote({"description": 42})["description"] == 42
    assert not isinstance(outbound.map_fields_to_remote({"description": 42})["description"], str)
    assert (
        outbound.map_fields_to_remote({"description": sentinel_dict})["description"]
        is sentinel_dict
    )


def test_dc_map_fields_to_remote_renames_title_to_summary():
    assert _backend().outbound.map_fields_to_remote({"title": "New title"}) == {
        "summary": "New title"
    }


def test_dc_map_fields_to_remote_maps_every_status_plus_unmapped_default():
    outbound = _backend().outbound
    for local_status, dc_state in {
        "idea": "IDEA",
        "open": "To Do",
        "in_progress": "In Progress",
        "closed": "Done",
        "blocked": "In Progress",
        "cancelled": "Done",
    }.items():
        assert outbound.map_fields_to_remote({"status": local_status}) == {"status": dc_state}
    # Map-or-drift (S2): an unmapped status is OMITTED, never coerced to "To Do".
    assert outbound.map_fields_to_remote({"status": "no_such_status"}) == {}


def test_dc_map_fields_to_remote_maps_every_priority_plus_unmapped_default():
    outbound = _backend().outbound
    for local_priority, dc_name in {
        0: "Highest",
        1: "High",
        2: "Medium",
        3: "Low",
        4: "Lowest",
    }.items():
        assert outbound.map_fields_to_remote({"priority": local_priority}) == {"priority": dc_name}
    # Map-or-drift (S5): an unmapped priority is OMITTED, never coerced to "Medium".
    assert outbound.map_fields_to_remote({"priority": 99}) == {}


def test_dc_map_fields_to_remote_passes_resolved_fields_through_by_own_name():
    assert _backend().outbound.map_fields_to_remote(
        {"assignee": "jsmith", "parent": "REB-1", "reporter": "someone"}
    ) == {"assignee": "jsmith", "parent": "REB-1", "reporter": "someone"}


def test_dc_map_fields_to_remote_on_empty_changed_dict_is_empty():
    assert _backend().outbound.map_fields_to_remote({}) == {}


# ---------------------------------------------------------------------------
# AC4 — DC's identity label form, pinned by value. DC binds the SHARED
# ``JiraIdentityConvention``, so parity with Cloud here is CORRECT: the
# ``rebar-id:`` back-pointer is one convention across the Jira family, and a DC
# deployment that minted a different prefix would orphan every binding.
# ---------------------------------------------------------------------------


def test_dc_identity_format_label_uses_canonical_colon_form():
    assert _backend().identity.format_label("abc1-2345-6789-0abc") == "rebar-id:abc1-2345-6789-0abc"


def test_dc_identity_parse_label_accepts_colon_and_legacy_hyphen_forms():
    ident = _backend().identity
    assert ident.parse_label("rebar-id:abc1-2345") == "abc1-2345"
    assert ident.parse_label("rebar-id-abc1-2345") == "abc1-2345"


def test_dc_identity_parse_label_rejects_non_identity_and_empty_remainder():
    ident = _backend().identity
    assert ident.parse_label("sprint-42") is None
    assert ident.parse_label("rebar-id:") is None
    assert ident.parse_label("rebar-id:   ") is None


def test_dc_identity_is_identity_label_tracks_parse():
    ident = _backend().identity
    assert ident.is_identity_label("rebar-id:abc1") is True
    assert ident.is_identity_label("rebar-id-abc1") is True
    assert ident.is_identity_label("rebar-id:") is False
    assert ident.is_identity_label("other") is False


# ---------------------------------------------------------------------------
# The DC resolved-status default was RETIRED by task 549c along with the
# write-only transport plumbing it fed (see test_jira_dc_config_settings.py).
# The mutation-ledger entry below is kept as the historical record.
# ---------------------------------------------------------------------------
