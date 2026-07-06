"""Story 7c58 (epic sure-foyer-aroma): two Pass-4 code-review coach moves —
``file-follow-on-ticket`` and ``delete-bad-test``.

Pins the data-flow that resolved the plan-review G6 block: a move is OFFERED by Pass-4 only when
its ``applies_when`` overlaps ``active_triggers`` (the union of the surviving findings' overlay
``criteria``). Both new moves therefore key on EXISTING ``{OVERLAY_IDS ∪ "always"}`` tags — never a
novel value — so they actually enter the applicable set. ``validate_move_registry`` does NOT check
``applies_when`` against the overlay vocabulary, so the real proof is ``applicable_moves``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_new_moves_registered_with_valid_templates():
    # Built-ins load STRICTLY — a bad template/name would raise here. Both carry a {subject}.
    from rebar.llm.code_review.moves import load_move_registry

    mr = load_move_registry()
    assert "file-follow-on-ticket" in mr
    assert "delete-bad-test" in mr
    for mid in ("file-follow-on-ticket", "delete-bad-test"):
        assert "{subject}" in mr[mid]["template"]
        assert mr[mid]["name"]
    # delete-bad-test names DELETION (distinct from the additive add-regression-test move).
    assert "delete" in mr["delete-bad-test"]["name"].lower()
    assert "re-pin" in mr["delete-bad-test"]["template"].lower()
    # file-follow-on-ticket pairs with rebar's discovered_from provenance.
    assert "discovered_from" in mr["file-follow-on-ticket"]["template"]


def test_applies_when_uses_only_existing_vocabulary():
    # The closed vocabulary is {OVERLAY_IDS ∪ "always"}. A novel tag would never enter
    # active_triggers, so assert both moves key on values already in that set.
    from rebar.llm.code_review.moves import load_move_registry
    from rebar.llm.code_review.registry import OVERLAY_IDS

    allowed = set(OVERLAY_IDS) | {"always"}
    mr = load_move_registry()
    assert set(mr["delete-bad-test"]["applies_when"]) <= allowed
    assert set(mr["file-follow-on-ticket"]["applies_when"]) <= allowed
    assert mr["delete-bad-test"]["applies_when"] == ["tests"]
    assert mr["file-follow-on-ticket"]["applies_when"] == ["always"]


def test_delete_bad_test_offered_for_a_tests_finding():
    # The REAL proof it fires: a surviving finding tagged criteria:["tests"] builds
    # active_triggers={"tests"}, and applicable_moves surfaces delete-bad-test for it.
    from rebar.llm.code_review.moves import load_move_registry
    from rebar.llm.review_kernel import applicable_moves

    mr = load_move_registry()
    active_triggers = {"tests"}  # what code_review_coach_inputs derives from a tests finding
    applicable = applicable_moves(mr, active_triggers)
    assert "delete-bad-test" in applicable
    # It sits alongside the additive test move — the coach chooses delete vs add by their names.
    assert "add-regression-test" in applicable
    # A non-overlapping specialist move (security) is NOT offered.
    assert "threat-model" not in applicable


def test_file_follow_on_ticket_is_always_offered():
    # `always` means offered regardless of which overlay the finding came from — even with no
    # trigger overlap. delete-bad-test (tests-only) is NOT offered when tests is inactive.
    from rebar.llm.code_review.moves import load_move_registry
    from rebar.llm.review_kernel import applicable_moves

    mr = load_move_registry()
    applicable = applicable_moves(mr, set())  # no active triggers
    assert "file-follow-on-ticket" in applicable
    assert "delete-bad-test" not in applicable
