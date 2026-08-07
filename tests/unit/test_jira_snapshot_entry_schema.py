"""Happy path for the packaged Jira snapshot-entry contract (ticket 8537).

ADR 0004 always claimed the snapshot-entry shape had "a single source of truth …
both the fetcher's output and the differs' expectations target it", but the
hand-rolled Python module holding it was imported by no production code and sat
outside the machinery that keeps the other ~90 schemas honest. The contract now
lives in ``src/rebar/schemas`` as ``jira_snapshot_entry.schema.json``, so it is
loadable via ``schemas.load()`` and its generated ``JiraSnapshotEntry`` TypedDict
rides the existing CI drift gate (``python -m rebar.schemas.gen_types --check``).

These tests assert OBSERVABLE behaviour only — what the loader returns and what
the validator accepts — never private names or source text. Edge/error coverage
(the flat-``comments`` regression, malformed sub-shapes) is owned by
``tests/integration/rebar_reconciler/jira_contract/test_snapshot_contract.py``,
which validates real ``fetch_snapshot`` output against this same schema.

The fixture ``tests/fixtures/jira/snapshot_entry_shapes.json`` is DERIVED and
REDACTED from real fetcher output under ``bridge_state/snapshots/`` (gitignored,
PII-bearing): every ``emailAddress`` / ``displayName`` / ``accountId`` is replaced
with a placeholder and every tenant URL dropped. The repo-wide scrub guard in
``tests/integration/rebar_reconciler/jira_contract/test_jira_fixtures.py`` globs
``tests/fixtures/jira/*.json``, so it covers this file automatically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar import schemas

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "jira" / "snapshot_entry_shapes.json"

# The three shapes observed across real fetcher output, and what each one pins.
POPULATED = "REB-901"  # every contract key present; parent is {"key": ...}
PARENT_ABSENT = "REB-902"  # no `parent` key at all — "never queried"
EMPTY = "REB-903"  # horizon-trimmed: comment.comments == []; parent is null


@pytest.fixture(scope="module")
def entries() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_contract_is_loadable_from_the_schemas_package() -> None:
    """The contract is a packaged schema like its siblings, not a Python literal."""
    assert schemas.JIRA_SNAPSHOT_ENTRY in schemas.names()

    schema = schemas.load(schemas.JIRA_SNAPSHOT_ENTRY)
    assert schema["title"] == "JiraSnapshotEntry"
    # Every consumer-read key the differs depend on is described by the contract.
    assert {
        "summary",
        "description",
        "status",
        "priority",
        "assignee",
        "labels",
        "issuetype",
        "comment",
        "issuelinks",
        "parent",
    } <= set(schema["properties"])


def test_generated_typeddict_tracks_the_schema() -> None:
    """The generated layer names the same keys — the drift gate's subject.

    ``python -m rebar.schemas.gen_types --check`` fails in CI when these two fall
    out of step, which is what forces a fetcher field change through the schema.
    """
    from rebar.types import JiraSnapshotEntry

    schema_keys = set(schemas.load(schemas.JIRA_SNAPSHOT_ENTRY)["properties"])
    typed_keys = set(JiraSnapshotEntry.__required_keys__) | set(JiraSnapshotEntry.__optional_keys__)
    assert typed_keys == schema_keys


@pytest.mark.parametrize("key", [POPULATED, PARENT_ABSENT, EMPTY])
def test_real_shaped_entries_validate_cleanly(entries, key) -> None:
    """Each of the three observed producer shapes is accepted by the contract."""
    schemas.validator(schemas.JIRA_SNAPSHOT_ENTRY).validate(entries[key])


def test_fixture_covers_the_parent_present_vs_absent_distinction(entries) -> None:
    """The distinction that gates a data-destroying inbound CLEAR stays intact.

    ``parent`` ABSENT means "never queried" (a truncated walk, a cross-project
    issue, or ``get_parent_map``'s ``{}`` degradation); ``parent`` PRESENT-and-null
    means "queried, and Jira genuinely has no parent". Only the latter may
    authorise clearing a local parent, so the contract must accept both and keep
    them distinguishable.
    """
    validate = schemas.validator(schemas.JIRA_SNAPSHOT_ENTRY).validate

    assert entries[POPULATED]["parent"] == {"key": "REB-903"}
    assert "parent" not in entries[PARENT_ABSENT]
    assert entries[EMPTY]["parent"] is None

    # All three arms are valid: the schema records the distinction, it does not
    # collapse it by demanding the key or forbidding the null.
    for key in (POPULATED, PARENT_ABSENT, EMPTY):
        validate(entries[key])
