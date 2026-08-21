"""Characterization of the Jira DATA CENTER backend — pins VALUES, not shapes
(rebar-ticket cedc-58d1-f6d1-428e, epic 3e73-72b5-cff2-40f0).

The Cloud sibling ``test_backend_characterization.py`` is a byte-for-byte safety
net, but it hard-binds ``_backend()`` to ``JiraBackend``, so none of it runs
against Data Center. DC's only unit-level backend coverage
(``test_backend_contract.py``) asserts presence and SHAPE — "the result is a
dict", "the expected keys are present" — and would pass with EVERY VALUE wrong.
This module closes that gap: every assertion here is one a wrong-but-well-formed
value fails.

WHY A SIBLING FILE AND NOT A PARAMETRIZATION OF THE CLOUD MODULE
----------------------------------------------------------------
The plan said to try the parametrization first and treat a split as the fallback.
It was tried, and rejected on two concrete grounds:

1. *It hides the Cloud pins it is supposed to preserve.* Parametrizing the Cloud
   module's ``_backend()`` seam over both vendors turns each golden literal into a
   per-vendor lookup — ``"assignee": "me@example.com"`` becomes
   ``{"jira": ..., "jira-datacenter": ...}[vendor]``. Cloud's pin is no longer a
   value a reviewer can read off the assertion, which is precisely the property
   that made it a safety net.
2. *Where the adapters legitimately diverge, a parameter is not enough.* The
   create-path description is the clearest case: Cloud's ``_map_local_to_jira_fields``
   does NOT fit it at all, while DC's ``_map_local_to_dc_fields`` fits it through
   ``WikiTextCodec``. That needs a per-vendor BRANCH, not a per-vendor value — at
   which point the "one parametrized test" is two tests wearing one hat.

A third, mechanical reason, and the decisive one in hindsight: the Cloud module is
373 LOC and the CI module-size gate is a hard 800. This module is ~715 LOC on its
own, so folding the two together would blow the cap outright — the split is not a
stylistic preference, it is the only shape that fits.

Splitting also makes AC9 ("Cloud's existing assertions still pass UNEDITED")
provable by inspection: ``test_backend_characterization.py`` is not touched by this
change at all.

WHERE CLOUD AND DC LEGITIMATELY DIVERGE, THIS MODULE ASSERTS THE DIVERGENCE.
Nothing here claims parity the adapters do not have; the tests named
``..._diverges_from_cloud`` pin the difference itself, so a future change that
accidentally makes DC behave like Cloud goes red.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.adapters.jira_datacenter.backend import (
    JiraDataCenterBackend,
    _map_local_to_dc_fields,
)
from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec

from .backend_support import FakeTransport

# ===========================================================================
# MUTATION LEDGER — every pin in this module was mutation-checked
# (rebar-ticket cedc-58d1-f6d1-428e, AC8).
#
# A characterization suite's own credibility is the risk it carries: DC's
# pre-existing coverage asserted shape and would have passed with every value
# wrong. So each pin here was verified by PERTURBING the value it pins — one map
# entry, or one boundary by one character — in a working tree, running the pin,
# and confirming it went red for the RIGHT reason (an ``AssertionError``, never an
# ``AttributeError``/``ImportError``, which would prove nothing). The mutation is
# then reverted and leaves no artifact of its own, which is why this block exists:
# it is the committed, diff-reviewable record. The same ledger is recorded as a
# comment on the ticket.
#
# MUTATED: LOCAL_PRIORITY_TO_JIRA[1] "High" -> "Higher"; FAILED:
#   test_dc_create_path_maps_every_key_by_value,
#   test_dc_create_path_priority_map_pins_every_level,
#   test_dc_map_fields_to_remote_maps_every_priority_plus_unmapped_default
# MUTATED: LOCAL_STATUS_TO_JIRA["in_progress"] "In Progress" -> "In progress";
#   FAILED: test_dc_create_path_maps_every_key_by_value,
#   test_dc_create_path_status_map_pins_every_state,
#   test_dc_map_fields_to_remote_maps_every_status_plus_unmapped_default
# MUTATED: JIRA_SUMMARY_MAX_CHARS 254 -> 253; FAILED:
#   test_dc_sanitize_summary_at_inclusive_limit_is_untruncated,
#   test_dc_sanitize_summary_one_over_limit_truncates_with_marker
# MUTATED: JIRA_LABEL_MAX_CHARS 255 -> 254; FAILED:
#   test_dc_sanitize_label_pins_the_shared_token_rules
# MUTATED: _LOCAL_TO_JIRA_TYPE["story"] "Story" -> "Storey"; FAILED:
#   test_dc_create_path_maps_every_key_by_value,
#   test_dc_create_path_issuetype_map_pins_every_type
# MUTATED: _map_local_to_dc_fields assignee `ticket.get("assignee") or ""` ->
#   `ticket.get("assignee", "")`; FAILED:
#   test_dc_create_path_coerces_explicit_none_to_empty_string
# MUTATED: _map_local_to_dc_fields description `codec.fit_outbound(...)` ->
#   unfitted passthrough; FAILED:
#   test_dc_create_path_fits_description_through_the_wiki_codec,
#   test_dc_create_path_description_fit_diverges_from_cloud
# MUTATED: WIKI_DESCRIPTION_LIMIT 32767 -> 32766; FAILED:
#   test_dc_rich_text_constants_pinned,
#   test_dc_sanitize_description_at_inclusive_limit_is_untruncated,
#   test_dc_sanitize_comment_at_inclusive_limit_is_untruncated,
#   test_dc_map_fields_to_remote_leaves_an_at_limit_description_untouched,
#   test_dc_create_path_leaves_an_at_limit_description_untouched
# MUTATED: _WIKI_TRUNCATION_SUFFIX " … [truncated by reconciler]" ->
#   " … [truncated by reconcilers]"; FAILED:
#   test_dc_sanitize_description_one_over_limit_truncates_with_marker,
#   test_dc_sanitize_comment_one_over_limit_truncates_with_marker,
#   test_dc_create_path_fits_description_through_the_wiki_codec
# MUTATED: OutboundFieldMapper description guard `isinstance(value, str)` -> `True`
#   AND WikiTextCodec.fit_outbound non-str `return text` -> `return str(text)`;
#   FAILED: test_dc_map_fields_to_remote_passes_non_string_description_untouched
# MUTATED: OutboundFieldMapper status default "To Do" -> "Todo"; FAILED:
#   test_dc_map_fields_to_remote_maps_every_status_plus_unmapped_default
# MUTATED: JiraIdentityConvention _CANONICAL_PREFIX "rebar-id:" -> "rebar-id=";
#   FAILED: test_dc_identity_format_label_uses_canonical_colon_form
# MUTATED: DEFAULT_RESOLVED_STATUSES {"Resolved","Done","Cancelled"} ->
#   {"Resolved","Done","Canceled"}; FAILED:
#   test_dc_default_resolved_statuses_pinned
#   (constant and test both RETIRED by task 549c; entry kept as the record)
#
# TWO MUTATIONS INITIALLY SURVIVED, and fixing them changed the suite. Recorded
# because a mutation ledger that only lists successes is not evidence:
#
#   1. WIKI_DESCRIPTION_LIMIT 32767 -> 32766 survived, because the at-limit pins
#      built their inputs from the IMPORTED constant, so the mutation moved the
#      test's own boundary along with the code's. Fixed by pinning against the
#      module-local LITERAL ``WIKI_LIMIT`` (see its comment) — after which the same
#      mutation kills five pins.
#   2. The mapper's `isinstance(value, str)` guard could not be killed on its own,
#      nor could WikiTextCodec.fit_outbound's non-str early return: the two guards
#      are REDUNDANT on the DC path. Only removing BOTH is observable, which is how
#      that mutation is recorded above and explained on the pin itself.
# ===========================================================================

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
    # ``.get(key, default)`` fallback of each value map rather than the ticket's.
    # Map-or-drift (S2): an unmapped status is OMITTED entirely, never coerced.
    assert _map_local_to_dc_fields(
        {"title": "t", "ticket_type": "no_such_type", "priority": 99, "status": "no_such_status"}
    ) == {
        "summary": "t",
        "description": "",
        "issuetype": "Task",
        "priority": "Medium",
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


def test_dc_fit_comment_is_the_wiki_codec_fit():
    # ``_DCSanitizer.fit_comment`` is the differ-side comparison transform; it must
    # be the IDENTICAL fit the send path applies, or the diff never converges.
    body = "c" * (WIKI_LIMIT + 1)
    assert _backend().sanitizer.fit_comment(body) == WikiTextCodec().fit_outbound(body)


# ---------------------------------------------------------------------------
# AC3 — the SHARED ``OutboundFieldMapper.map_fields_to_remote`` driven through
# ``WikiTextCodec``. That shared body is currently exercised only through
# ``AdfCodec`` (test_rich_text_seam_heldout.py), so a regression in the DC
# composition would reach DC users with nothing to catch it.
#
# Deliberately NOT an ordering assertion. ``WikiTextCodec.normalize_outbound`` is
# the IDENTITY, so ``normalize_outbound(fit_outbound(v)) == fit_outbound(v)`` for
# every ``v`` and swapping the two calls is unobservable on the DC path — an
# ordering pin written here could not fail. Ordering stays ``AdfCodec``'s to pin.
# What the DC path CAN pin is that the fit is applied at all, and its exact value.
# ---------------------------------------------------------------------------


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
    assert outbound.map_fields_to_remote({"priority": 99}) == {"priority": "Medium"}


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
