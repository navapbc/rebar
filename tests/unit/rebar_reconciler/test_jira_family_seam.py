"""The ``adapters/jira_family/`` shared-layer public surface (story J2, epic e369).

J2 extracts the Jira-family-general units out of the Cloud adapter into
``adapters/jira_family/`` with PUBLIC names, so a second Jira-family backend
(Data Center, J6/J7) consumes one implementation instead of forking Cloud's.

These tests pin the shared layer's public contract: the value maps, the
sanitizers (with their vendor dependency INJECTED rather than imported), the
link vocabulary, and the identity convention.

Behaviour is unchanged from the pre-move Cloud implementations — the expected
values here are the same ones ``test_backend_characterization.py`` pins on the
Cloud side, so the two must agree.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.adapters.jira_family import (
    JIRA_LABEL_MAX_CHARS,
    JIRA_SUMMARY_MAX_CHARS,
    LOCAL_PRIORITY_TO_JIRA,
    LOCAL_STATUS_TO_JIRA,
    RELATION_TO_JIRA_LINK,
    InvalidLabelError,
    JiraIdentityConvention,
    sanitize_comment,
    sanitize_description,
    sanitize_label,
    sanitize_summary,
)

# ---------------------------------------------------------------------------
# Value maps — the single definition site
# ---------------------------------------------------------------------------


def test_local_priority_map_pins_every_level() -> None:
    assert LOCAL_PRIORITY_TO_JIRA == {
        0: "Highest",
        1: "High",
        2: "Medium",
        3: "Low",
        4: "Lowest",
    }


def test_local_status_map_pins_every_state_including_unified_deleted() -> None:
    """The unified map carries ``deleted`` -> ``Done``.

    This is J2's ONE declared observable change: the pre-move ACLI-side copy
    omitted ``deleted`` and fell through to ``.title()`` -> ``"Deleted"``, which
    is not a state in the live DIG workflow, so Jira rejected that transition.
    Two of the three pre-existing copies already mapped it to ``Done``.
    """
    assert LOCAL_STATUS_TO_JIRA == {
        "idea": "IDEA",
        "open": "To Do",
        "in_progress": "In Progress",
        "blocked": "In Progress",
        "closed": "Done",
        "cancelled": "Done",
        "deleted": "Done",
    }


def test_relation_to_jira_link_vocabulary_pinned() -> None:
    """Relations with no reliable Jira link type stay ABSENT (the differ skips them)."""
    assert RELATION_TO_JIRA_LINK == {
        "blocks": ("Blocks", False),
        "depends_on": ("Blocks", True),
        "relates_to": ("Relates", False),
    }
    for absent in ("duplicates", "supersedes", "discovered_from"):
        assert absent not in RELATION_TO_JIRA_LINK


# ---------------------------------------------------------------------------
# Sanitizers — pure ones move outright
# ---------------------------------------------------------------------------


def test_sanitize_label_strips_and_returns_clean_token() -> None:
    assert sanitize_label("  tidy-label  ") == "tidy-label"


def test_sanitize_label_at_inclusive_limit_passes() -> None:
    """The label limit is 'not more than 255', so 255 is accepted and 256 raises."""
    assert sanitize_label("x" * JIRA_LABEL_MAX_CHARS) == "x" * JIRA_LABEL_MAX_CHARS
    with pytest.raises(InvalidLabelError):
        sanitize_label("x" * (JIRA_LABEL_MAX_CHARS + 1))


@pytest.mark.parametrize(
    "bad",
    ["with space", "a,b", "   ", "x" * 300],
    ids=["internal-whitespace", "comma", "empty-after-strip", "oversize"],
)
def test_sanitize_label_raises_on_every_rejection_reason(bad: str) -> None:
    with pytest.raises(InvalidLabelError):
        sanitize_label(bad)


def test_sanitize_summary_at_inclusive_limit_is_untruncated() -> None:
    """Jira's summary error is 'less than 255', so the INCLUSIVE max is 254."""
    at_limit = "s" * JIRA_SUMMARY_MAX_CHARS
    assert sanitize_summary(at_limit) == at_limit


def test_sanitize_summary_one_over_limit_truncates_with_suffix() -> None:
    result = sanitize_summary("s" * (JIRA_SUMMARY_MAX_CHARS + 1))
    assert result.endswith(" [truncated]")
    assert len(result) == JIRA_SUMMARY_MAX_CHARS


# ---------------------------------------------------------------------------
# Sanitizers — the two whose vendor dependency is INJECTED, not imported
# ---------------------------------------------------------------------------


def test_sanitize_description_applies_the_injected_fit_function() -> None:
    """The rich-text dependency arrives as a contract, so the shared layer never
    imports the Cloud-pinned ``adf`` module. J3 widens ``fit`` to the full
    ``RichTextCodec``; here it is the minimal callable form."""
    assert sanitize_description("hello", fit=lambda text: text) == "hello"
    assert sanitize_description("hello world", fit=lambda text: text[:5]) == "hello"


def test_sanitize_comment_applies_the_injected_truncate_function() -> None:
    """Comment truncation arrives as a contract, so the shared layer never imports
    the Cloud-pinned ``comment_limits`` module."""
    assert sanitize_comment("body", truncate=lambda b: b, max_chars=32767) == "body"
    assert sanitize_comment("abcdef", truncate=lambda b: b[:3] + "…", max_chars=32767) == "abc…"


# ---------------------------------------------------------------------------
# Identity convention
# ---------------------------------------------------------------------------


def test_identity_convention_round_trips_the_canonical_form() -> None:
    convention = JiraIdentityConvention()
    assert convention.format_label("abc1-2345") == "rebar-id:abc1-2345"
    assert convention.parse_label("rebar-id:abc1-2345") == "abc1-2345"
    # the legacy hyphen read-form is still accepted
    assert convention.parse_label("rebar-id-abc1-2345") == "abc1-2345"
    assert convention.parse_label("unrelated-label") is None
    assert convention.is_identity_label("rebar-id:x") is True
    assert convention.is_identity_label("nope") is False
