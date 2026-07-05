"""The public ``rebar.types`` TypedDicts are faithfully derived from the schemas.

Story 3a10: ``src/rebar/types.py`` is GENERATED from the canonical JSON Schemas by
``rebar.schemas.gen_types``. These tests pin the two invariants the CI drift-gate
and the generator promise:

  * the committed ``types.py`` is not stale (regenerating reproduces it exactly),
  * each generated TypedDict's required/optional key split matches its schema
    (schema-``required`` → required field; the rest → ``NotRequired``).
"""

from __future__ import annotations

import rebar.types as public_types
from rebar import schemas
from rebar.schemas import gen_types


def test_types_module_is_not_stale() -> None:
    """Regenerating from the schemas reproduces the committed file byte-for-byte.

    This is the drift-gate as a unit test — a schema edit without a regenerate
    (or a hand-edit of the generated file) fails here as well as in CI.
    """
    committed = (gen_types._target_path()).read_text(encoding="utf-8")
    assert gen_types.render() == committed, (
        "src/rebar/types.py is stale — run `python -m rebar.schemas.gen_types`"
    )


def test_required_keys_match_schema() -> None:
    """Each top-level object TypedDict's required/optional split mirrors its schema."""
    for name in gen_types.TOP_LEVEL_OBJECTS:
        schema = schemas.load(name)
        cls = getattr(public_types, schema["title"])
        props = set(schema.get("properties", {}))
        required = set(schema.get("required", []))
        assert cls.__required_keys__ == required, name
        assert cls.__optional_keys__ == (props - required), name


def test_key_public_types_are_exported() -> None:
    """The headline return types resolve as TypedDicts."""
    for tname in ("TicketState", "TransitionResult", "ClaimResult", "CreateResult"):
        cls = getattr(public_types, tname)
        # TypedDicts carry the required/optional key metadata.
        assert hasattr(cls, "__required_keys__"), tname
