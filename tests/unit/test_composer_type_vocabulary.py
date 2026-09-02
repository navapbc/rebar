"""`create`'s type vocabulary is derived from TicketType (mirror F7).

Ticket ce8f-4da9-2836-4115 (boiling-gold-pterosaurs).

composer was the outlier: the reconciler's inbound-field vocabulary already derived the same
set, while composer re-listed it. A type added to `TicketType` but missed here is REJECTED at
`create`, so the new type cannot be filed at all.

Distinct from F3, which consolidated the plan-review EXEMPTION subset. This is the full create
vocabulary — a different concept living next door, which is why F3 deliberately left it alone.
"""

from __future__ import annotations

import inspect
from typing import get_args

import pytest

from rebar._commands import composer
from rebar.types import TICKET_TYPES, TicketType

pytestmark = pytest.mark.unit


def test_composer_types_are_the_canonical_vocabulary() -> None:
    """AC1."""
    assert composer._TYPES == TICKET_TYPES == get_args(TicketType)


def test_the_module_no_longer_lists_the_types() -> None:
    """AC1, negative half."""
    source = inspect.getsource(composer)
    assert '"bug", "epic", "story", "task"' not in source


def test_the_usage_text_is_generated_from_the_same_tuple() -> None:
    """AC2. Help and validation cannot disagree if one is rendered from the other."""
    line = next(x for x in composer._USAGE.split("\n") if "ticket_type:" in x)
    for member in TICKET_TYPES:
        assert member in line, f"{member} missing from the usage line"
    listed = [p.strip() for p in line.split(":", 1)[1].split("|")]
    assert listed == list(TICKET_TYPES), "usage order must track declaration order"


def test_declaration_order_is_preserved() -> None:
    """The curated help order (work types first) is the Literal's order; sorting would have
    silently reworded user-facing help."""
    assert TICKET_TYPES[:4] == ("bug", "epic", "story", "task")


def test_a_type_added_to_ticket_type_becomes_creatable() -> None:
    """AC3. The derivation is what makes this true."""
    extended = set(get_args(TicketType)) | {"proposal"}
    assert extended - set(composer._TYPES) == {"proposal"}


def test_an_unknown_type_is_still_rejected() -> None:
    """AC4."""
    assert "not-a-type" not in composer._TYPES
