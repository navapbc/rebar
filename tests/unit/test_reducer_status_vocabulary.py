"""The reducer's status vocabulary is derived from TicketStatus (mirror F6).

Ticket a516-e61b-ede9-465c (disparaged-censorable-lobo).

`_KNOWN_TICKET_STATUSES` is not a description of the statuses — it is an ASSERTION about
them. `_processors` raises `ValueError("unknown ticket status in snapshot")` on anything
outside it, so unlike the event-type path (which preserves-and-ignores an unknown kind), a
status this set lags on makes a newer clone's snapshot UNREADABLE.
"""

from __future__ import annotations

from typing import get_args

import pytest

from rebar.reducer._processors import _KNOWN_TICKET_STATUSES
from rebar.types import TicketStatus

pytestmark = pytest.mark.unit


def test_the_reducer_vocabulary_equals_ticket_status() -> None:
    """AC1. Imports both real objects; re-listing either would defeat the check."""
    assert _KNOWN_TICKET_STATUSES == frozenset(get_args(TicketStatus))


def test_a_status_added_to_ticket_status_needs_no_second_edit() -> None:
    """AC2. The derivation is what makes this true; a copy would need remembering."""
    extended = frozenset(get_args(TicketStatus)) | {"parked"}
    assert extended - _KNOWN_TICKET_STATUSES == {"parked"}, (
        "only the hypothetical new member should be outside the derived set"
    )


def test_the_module_no_longer_lists_the_statuses() -> None:
    """AC1, the negative half: a literal copy left behind would silently win."""
    import inspect

    from rebar.reducer import _processors

    source = inspect.getsource(_processors)
    assert '{"idea", "open", "in_progress"' not in source
    assert "frozenset(get_args(TicketStatus))" in source


def test_every_current_status_is_accepted() -> None:
    """AC3. Behavior unchanged for the seven that exist."""
    for status in get_args(TicketStatus):
        assert status in _KNOWN_TICKET_STATUSES


def test_an_unknown_status_is_still_outside_the_set() -> None:
    """AC3. The raise at the snapshot reader must still have something to fire on."""
    assert "not-a-status" not in _KNOWN_TICKET_STATUSES
