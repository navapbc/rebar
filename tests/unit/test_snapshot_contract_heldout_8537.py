"""Held-out validation for bug 8537 — authored independently of the implementation.

ADR 0004 asserted the snapshot-entry shape "has a single source of truth... Both the
fetcher's output and the differs' expectations target it." Nothing enforced that: the
old `_snapshot_schema.py` was imported by NO production module, and the intended
producer `fetcher.py` never imported it.

The contract is not decorative. It encodes a `parent` present-vs-absent distinction
that gates an inbound CLEAR — a data-destroying path whose own history contains a revert
titled "Revert the inbound parent clear: it destroys data." Collapsing "absent" (never
queried) into "null" (known-empty) is therefore the single most costly way this schema
could be wrong, so it is tested hardest here.

The other thing worth proving is that the enforcement is REAL rather than declarative:
a schema that accepts everything would pass a happy-path test and catch nothing. Most of
these cases are therefore REJECTIONS.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rebar import schemas

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "jira" / "snapshot_entry_shapes.json"


def _validator():
    return schemas.validator(schemas.JIRA_SNAPSHOT_ENTRY)


def _entries() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _valid_entry() -> dict[str, Any]:
    """The populated shape, as a fresh mutable copy."""
    return json.loads(json.dumps(_entries()["REB-901"]))


# ── the contract is reachable through the shared loader ─────────────────────


def test_contract_is_loadable_like_its_ninety_siblings() -> None:
    assert schemas.JIRA_SNAPSHOT_ENTRY in schemas.names()
    doc = schemas.load(schemas.JIRA_SNAPSHOT_ENTRY)
    assert doc.get("properties"), "the contract must actually describe the entry shape"


# ── the parent distinction: absent != null != present ───────────────────────


def test_all_three_parent_arms_are_accepted() -> None:
    """Absent (never queried), null (known-empty) and present must ALL validate — the
    schema must not force the producer to invent a value it does not have."""
    v = _validator()
    entries = _entries()
    assert "parent" not in entries["REB-902"], "fixture must cover the parent-ABSENT shape"
    assert entries["REB-903"]["parent"] is None, "fixture must cover the parent-NULL shape"
    assert entries["REB-901"]["parent"]["key"], "fixture must cover the parent-PRESENT shape"
    for entry in entries.values():
        v.validate(entry)  # must not raise for any of the three


def test_a_present_parent_must_carry_a_key() -> None:
    """`{}` is the dangerous middle ground: present, so a consumer reads it, but with no
    key to read. That must be refused rather than silently treated as a clear."""
    import jsonschema

    entry = _valid_entry()
    entry["parent"] = {}
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(entry)


def test_parent_as_a_bare_string_is_refused() -> None:
    """The shape a naive fetcher change would produce (`parent: "REB-1"`)."""
    import jsonschema

    entry = _valid_entry()
    entry["parent"] = "REB-1"
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(entry)


# ── the enforcement is real, not declarative ────────────────────────────────


def test_the_flat_comments_regression_is_refused() -> None:
    """BUG-0ee6's shape: `comments` at the TOP level instead of nested under `comment`.
    This is the regression the original hand-rolled guard existed to catch; it must
    survive the move into schemas/."""
    import jsonschema

    entry = _valid_entry()
    entry["comments"] = []
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(entry)


def test_labels_as_a_comma_string_is_refused() -> None:
    import jsonschema

    entry = _valid_entry()
    entry["labels"] = "a,b,c"
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(entry)


def test_an_issuelink_without_a_type_name_is_refused() -> None:
    """The differs key relation mapping off `type.name`; a link missing it would be
    silently skipped rather than mapped."""
    import jsonschema

    entry = _valid_entry()
    entry["issuelinks"] = [{"type": {}}]
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(entry)


# ── the old duplicate is gone ───────────────────────────────────────────────


def test_the_hand_rolled_module_no_longer_exists() -> None:
    """Two copies of one contract is the defect; the point of the move is that there is
    now exactly one."""
    src = Path(schemas.__file__).resolve().parents[1]
    assert not (src / "_engine" / "rebar_reconciler" / "_snapshot_schema.py").exists()


def test_generated_typeddict_tracks_the_schema() -> None:
    """The drift gate's subject: the generated TypedDict's keys must equal the schema's
    properties, so a schema edit without a regeneration is caught by `gen_types --check`."""
    from rebar import types

    doc = schemas.load(schemas.JIRA_SNAPSHOT_ENTRY)
    td = types.JiraSnapshotEntry
    generated = set(td.__required_keys__) | set(td.__optional_keys__)
    assert generated == set(doc["properties"]), (
        "TypedDict and schema disagree; the drift gate would not be enforcing this contract"
    )
