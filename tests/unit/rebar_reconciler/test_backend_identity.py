"""HELD-OUT edge tests for JiraIdentityConvention (S2, epic bbf1).

The identity convention has no pre-existing single delegate — S2 introduces it as a
pure object that must reproduce the inlined ``rebar-id:`` convention EXACTLY (the
write form ``rebar-id:<local_id>`` and the read scan at ``binding_walk`` that accepts
both the canonical ``rebar-id:`` colon form and the legacy ``rebar-id-`` hyphen form,
treating an empty-after-prefix marker as NOT a binding).

Held out from the implementation subagent.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.adapters.jira.identity import JiraIdentityConvention


@pytest.fixture
def identity() -> JiraIdentityConvention:
    return JiraIdentityConvention()


def test_format_uses_the_canonical_colon_form(identity):
    assert identity.format_label("abc1-2345") == "rebar-id:abc1-2345"


def test_parse_recovers_id_from_colon_form(identity):
    assert identity.parse_label("rebar-id:abc1-2345") == "abc1-2345"


def test_parse_recovers_id_from_legacy_hyphen_form(identity):
    # binding_walk accepts the legacy ``rebar-id-`` prefix on read.
    assert identity.parse_label("rebar-id-abc1-2345") == "abc1-2345"


def test_format_parse_round_trip(identity):
    for local_id in ("a", "abc1-2345-6789-0abc", "jira-dig-5029"):
        assert identity.parse_label(identity.format_label(local_id)) == local_id


def test_empty_marker_is_not_a_binding(identity):
    # An empty/whitespace-only id after the prefix is NOT an identity binding
    # (matches binding_walk._has_rebar_id_label — else the key strands forever).
    assert identity.parse_label("rebar-id:") is None
    assert identity.parse_label("rebar-id:   ") is None
    assert identity.is_identity_label("rebar-id:") is False


def test_non_identity_labels_are_rejected(identity):
    for label in ("imported:foo", "rebar-status:blocked", "random", "", "id:foo"):
        assert identity.parse_label(label) is None
        assert identity.is_identity_label(label) is False


def test_is_identity_label_true_for_well_formed_markers(identity):
    assert identity.is_identity_label("rebar-id:abc1") is True
    assert identity.is_identity_label("rebar-id-abc1") is True
