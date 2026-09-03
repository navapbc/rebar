"""The cross-session warn set is derived from ``ROUTES``, not a hardcoded spelling list.

``_execute.py`` carried 14 hardcoded command SPELLINGS and checked them with
``if name not in _CROSS_SESSION_WARN_COMMANDS``. A route rename therefore dropped the warning
silently: the stale spelling simply never matched again and nothing failed (mirror F11).

WHICH verbs warn is a policy judgement and is NOT what this change touches. The mirror is only
that the spellings must BE route names, which nothing enforced — so the fix flags the routes and
derives the set, and these tests pin that the derivation did not move the policy.
"""

from __future__ import annotations

import pytest

from rebar._cli import _execute, _registry

pytestmark = pytest.mark.unit

#: The exact 14 spellings the hardcoded literal carried, kept as the HISTORICAL RECORD so the
#: refactor is provably policy-preserving. This is not a mirror of the route table: it is the
#: "before" side of a one-time comparison, and it is expected to need a deliberate edit if the
#: policy is ever intentionally changed.
_LITERAL_BEFORE = frozenset(
    {
        "show",
        "comment",
        "edit",
        "transition",
        "reopen",
        "tag",
        "untag",
        "set-file-impact",
        "deps",
        "archive",
        "check-ac",
        "clarity-check",
        "link",
        "unlink",
    }
)


def _derived() -> frozenset[str]:
    return _registry.derive_policy_sets()["_WARN_CROSS_SESSION"]


def test_warn_set_is_derived_from_the_route_table() -> None:
    """AC1: the set comes from ``derive_policy_sets``, beside its sibling policy sets."""
    assert "_WARN_CROSS_SESSION" in _registry.derive_policy_sets()


def test_execute_no_longer_carries_a_spelling_list() -> None:
    """AC1: the literal is gone from ``_execute`` — it consumes the derived set."""
    source = _execute.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "_CROSS_SESSION_WARN_COMMANDS = frozenset(" not in text, (
        "_execute.py still defines its own spelling list"
    )


def test_membership_is_unchanged_by_the_refactor() -> None:
    """AC2: exactly the same 14 commands warn — the refactor moved no policy."""
    assert _derived() == _LITERAL_BEFORE


def test_every_warn_name_is_a_live_route() -> None:
    """AC3: the property the hardcoded list could violate and nothing checked."""
    live = {route.name for route in _registry.ROUTES if not route.retired}
    assert _derived() <= live, f"warn names with no live route: {sorted(_derived() - live)}"


def test_a_renamed_route_carries_its_warning() -> None:
    """AC4: the flag travels with the route, which is the whole point of the change."""
    original = next(r for r in _registry.ROUTES if r.name == "comment")
    assert original.warn_cross_session is True

    import dataclasses

    renamed = dataclasses.replace(original, name="annotate")
    table = (*(r for r in _registry.ROUTES if r.name != "comment"), renamed)
    derived = _registry.derive_policy_sets(table)["_WARN_CROSS_SESSION"]

    assert "annotate" in derived, "the warning did not follow the rename"
    assert "comment" not in derived


def test_an_unflagged_route_does_not_warn() -> None:
    """The negative control: the flag is a partition, not a blanket."""
    assert "list" not in _derived()
    assert "search" not in _derived()


def test_a_retired_flagged_route_contributes_nothing() -> None:
    """Retired spellings are unrouted, so they must not leak into the overlay."""
    import dataclasses

    retired = dataclasses.replace(
        next(r for r in _registry.ROUTES if r.name == "comment"),
        name="obsolete-verb",
        retired=True,
        handler=None,
    )
    derived = _registry.derive_policy_sets((*_registry.ROUTES, retired))["_WARN_CROSS_SESSION"]
    assert "obsolete-verb" not in derived
