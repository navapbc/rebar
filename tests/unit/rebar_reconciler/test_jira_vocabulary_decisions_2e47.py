"""The Jira-family vocabulary decisions bug 2e47 turned from prose into data.

Three of 2e47's four findings are DISPOSITIONS rather than code changes, and a disposition that
lives only in a comment drifts. These tests pin the ones that can be pinned in-repo:

* the relation vocabulary is exhaustively partitioned into synced and deliberately-unsynced, so a
  new relation added without a decision fails here rather than silently never syncing;
* the label ceiling stays 255 and the reason is pinned, because the obvious "correct it to the
  measured 254" is a Cloud regression (see the test below).
"""

from __future__ import annotations

import typing

import pytest

from rebar.types import Relation


def _relation_vocabulary() -> set[str]:
    return set(typing.get_args(Relation))


def _value_maps():
    """The single definition site, reached the way the seam tests reach it.

    ``rebar_reconciler`` is a top-level package on the test path, not
    ``rebar._engine.rebar_reconciler`` — the engine is loaded dynamically.
    """
    from rebar_reconciler.adapters.jira_family import value_maps

    return value_maps


def test_every_relation_is_either_synced_or_deliberately_unsynced():
    """THE GUARD: the two sets must exhaustively partition the relation vocabulary.

    2e47's finding was not that four relations go unsynced — that is a defensible choice — but
    that nothing recorded whether it was a choice. The failure mode this prevents is a NEW
    relation being added to `rebar.types.Relation` and silently not syncing, which looks
    identical to a decision nobody made.
    """
    value_maps = _value_maps()
    synced = set(value_maps.RELATION_TO_JIRA_LINK)
    unsynced = set(value_maps.UNSYNCED_RELATIONS)

    assert not (synced & unsynced), (
        f"relation(s) {sorted(synced & unsynced)} are declared both synced and unsynced"
    )
    assert synced | unsynced == _relation_vocabulary(), (
        f"undecided relation(s) {sorted(_relation_vocabulary() - (synced | unsynced))}: every "
        f"relation needs an explicit Jira sync decision — map it in RELATION_TO_JIRA_LINK or "
        f"record why it is skipped in UNSYNCED_RELATIONS. Silence is not a decision."
    )


def test_the_unsynced_set_is_exactly_the_four_measured_relations():
    """Names the four explicitly, because the prose named the wrong three — twice.

    The pre-fix code comment listed duplicates/supersedes/discovered_from (omitting `caused_by`);
    the bug report listed supersedes/discovered_from/caused_by (omitting `duplicates`). Both were
    wrong in different directions, which is why the set is now data.
    """
    assert set(_value_maps().UNSYNCED_RELATIONS) == {
        "duplicates",
        "supersedes",
        "discovered_from",
        "caused_by",
    }


def test_every_unsynced_relation_carries_a_reason():
    """A name with no reason is the same undocumented decision in a new shape."""
    for relation, reason in _value_maps().UNSYNCED_RELATIONS.items():
        assert reason.strip(), f"{relation} is declared unsynced with no reason"
        assert len(reason) > 20, f"{relation}'s reason is too terse to be a decision: {reason!r}"


def test_the_synced_relations_are_unchanged_by_this_ticket():
    """Behaviour-preserving guard: 2e47 documents the unsynced set, it re-maps nothing."""
    assert _value_maps().RELATION_TO_JIRA_LINK == {
        "blocks": ("Blocks", False),
        "depends_on": ("Blocks", True),
        "relates_to": ("Relates", False),
    }


def test_the_shared_label_ceiling_is_not_tightened_in_place():
    """THE TRAP THIS TICKET WALKED INTO. Do not "correct" 255 to the measured DC value here.

    A real DC 8.17.1 instance rejects a 255-char label and accepts 254, so tightening this shared
    constant looks like the fix. It is not: `sanitize_label` RAISES above the ceiling instead of
    truncating, so a shared 254 makes the live-validated CLOUD path reject a label Cloud accepts.
    That trades a silent DC defect for a loud Cloud regression — the precise failure mode the
    epic's characterization oracles exist to catch, and they DID catch it (four oracles went red).

    The label ceiling is an axis of variation and needs a per-deployment value, following the
    `comment_max_chars` precedent from bug 049e. Until that lands, this pins the shared constant
    so nobody repeats the shortcut.
    """
    assert _value_maps().JIRA_LABEL_MAX_CHARS == 255


def test_the_summary_ceiling_was_already_correct_and_is_unchanged():
    """Checked in the same measurement pass and found to MATCH, so it must not be touched."""
    assert _value_maps().JIRA_SUMMARY_MAX_CHARS == 254


def test_a_literal_255_character_label_passes_the_sanitizer():
    """A 255-char label, written as a LITERAL 255 rather than via the constant.

    The pre-existing seam test spells this length as `JIRA_LABEL_MAX_CHARS`, so it keeps passing
    whatever that constant becomes — it pins the sanitizer against itself and would go on being
    green if the ceiling moved to 254, 200, or 10, silently no longer testing 255 at all.

    255 is the interesting length precisely because the two deployments disagree about it: Jira
    Cloud's documented rule admits it, a real DC 8.17.1 instance rejects it. This pins what rebar
    does TODAY — accepts it — so the per-deployment fix (see the ceiling test above) has to change
    this test deliberately rather than drift past it.
    """
    from rebar_reconciler.adapters.jira_family import sanitize_label

    label = "x" * 255
    assert len(label) == 255

    assert sanitize_label(label) == label


def test_a_256_character_label_is_rejected():
    """The other side of the literal boundary, so the 255 case is a boundary and not a floor."""
    from rebar_reconciler.adapters.jira_family import InvalidLabelError, sanitize_label

    with pytest.raises(InvalidLabelError):
        sanitize_label("x" * 256)


def test_the_idea_status_mapping_is_left_intact_as_an_operator_prerequisite():
    """2e47's `IDEA` finding is a DISPOSITION, and the disposition is "change nothing".

    `IDEA` is absent from the harness instance's workflows, but `idea <-> IDEA` is a deliberate
    INJECTIVE mapping that requires the operator to add an `IDEA` status with transitions
    (config.py's own note; docs/jira-sync-setup.md "The `idea` status <-> Jira `IDEA`"). Its
    injectivity is what lets it round-trip WITHOUT a `rebar-status:` annotation label, unlike
    blocked/cancelled.

    So remapping `idea` to "To Do" — the tempting fix — would break the documented round-trip on
    every instance that HAS provisioned `IDEA`, to accommodate one that has not. This test exists
    to make that trade explicit and refuse it.
    """
    assert _value_maps().LOCAL_STATUS_TO_JIRA["idea"] == "IDEA"
    # The injectivity that the annotation-free round-trip depends on.
    mapped = _value_maps().LOCAL_STATUS_TO_JIRA
    assert sum(1 for value in mapped.values() if value == "IDEA") == 1, (
        "`IDEA` must remain the image of exactly one local status, or the annotation-free "
        "inbound reconstruction documented in config.py is no longer sound"
    )
