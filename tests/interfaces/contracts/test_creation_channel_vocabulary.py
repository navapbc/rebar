"""The runtime creation-channel vocabulary must stay in sync with the schema enum.

``creation_channel`` has two sources of truth that MUST agree: the wire/output schema
enum (``common.schema.json#/$defs/creation_channel``, consumed by validators and the
generated ``rebar.types``) and the runtime constant + validator in
``rebar.reducer._version`` (consumed by ``create_core`` on the write path). This
contract pins them together so the vocabulary can never drift between the schema and
the code that enforces it at write time (story 6fe2).
"""

from __future__ import annotations

import pytest

from rebar import schemas
from rebar.reducer._version import CREATION_CHANNELS, validate_creation_channel

_EXPECTED = {"cli", "mcp", "python", "jira", "import", "unknown"}


def _schema_enum() -> set[str]:
    return set(schemas.load(schemas.COMMON)["$defs"]["creation_channel"]["enum"])


def test_runtime_constant_matches_schema_enum() -> None:
    """The runtime constant and the schema enum are the SAME closed set."""
    assert set(CREATION_CHANNELS) == _schema_enum()


def test_vocabulary_is_exactly_the_six_documented_values() -> None:
    """Both sources equal the documented six-value enum (a change to either must be
    a deliberate edit to this list)."""
    assert set(CREATION_CHANNELS) == _EXPECTED
    assert _schema_enum() == _EXPECTED


def test_validate_accepts_every_live_write_channel() -> None:
    """Every channel except the projection-only ``unknown`` is a valid live write and
    is returned unchanged."""
    for ch in _EXPECTED - {"unknown"}:
        assert validate_creation_channel(ch) == ch


def test_validate_rejects_unknown_as_projection_only() -> None:
    """``unknown`` is in the vocabulary but is a projection-only fallback — never a
    valid value to write at genesis."""
    with pytest.raises(ValueError):
        validate_creation_channel("unknown")


def test_validate_rejects_out_of_vocabulary_value() -> None:
    with pytest.raises(ValueError):
        validate_creation_channel("slack")
