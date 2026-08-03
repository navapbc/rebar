r"""Canonical snapshot-entry schema — the single source of truth for the shape
the fetcher (PRODUCER) writes and the differs (CONSUMERS) read.

Epic f89d, story B (`8937-eed3-c881-4ea1`). The Jira-sync bug class (0ee6, 3f04)
was a *seam* defect: the fetcher and the differs share an implicit contract — the
per-issue snapshot-entry dict shape — that nothing pinned or jointly exercised, so
a key/shape divergence (differ reading flat ``comments`` while the fetcher wrote
nested ``comment``; the snapshot never carrying ``issuelinks``) silently produced
no mutation and passed green.

This module pins that contract ONCE, as both a ``TypedDict`` (for readers/IDEs) and
a JSON Schema (for runtime validation). ``test_snapshot_contract.py`` validates the
real ``fetcher.fetch_snapshot`` output against it and drives the producer→consumer
round-trip through the production path, so a future divergence fails a test
immediately instead of escaping to production.

Validation asserts **structure/type, not exact values** (a Dredd/Gavel posture):
the schema fixes which KEYS carry which SHAPES, never specific field values — so it
is not a change-detector. The schema is a *present-key shape floor* plus a direct
guard against the BUG-0ee6 flat-``comments`` shape; the BUG-3f04 *absent-issuelinks*
regression is caught by the round-trip contract test, not the schema (absence is not
a per-entry shape violation). See docs/adr/0004-reconciler-snapshot-contract.md.

## The producer→consumer contract (who reads what)

A snapshot entry is ``snapshot[jira_key]`` after ``fetcher._build_snapshot``: the
base ``fields`` of the issue (from ``search_issues``) merged with three enrichment
keys (``parent``, ``comment``, ``issuelinks``). Each field below names the
consumer that reads it, so a change to either side has an obvious contract anchor:

| Key          | Shape                                   | Consumer (read site) |
|--------------|-----------------------------------------|----------------------|
| ``summary``  | str                                     | both differs |
| ``description`` | ADF ``doc`` dict **or** plain str    | both differs (ADF-decoded) |
| ``status``   | ``{"name": str, ...}`` (or str)         | both differs |
| ``priority`` | ``{"name": str, ...}`` (or str/null)    | both differs |
| ``assignee`` | ``{"displayName"/"accountId", ...}`` or null | both differs |
| ``labels``   | list[str]                               | the label differs |
| ``issuetype``| ``{"name": str, ...}`` (or str)         | both differs |
| ``comment``  | ``{"comments": [{author, body, ...}]}`` | comment differs (bug 0ee6) |
| ``issuelinks``| ``[{type:{name,inward,outward}, in/outwardIssue?}]`` | link differs (3f04) |
| ``parent``   | ``{"key": str}``                        | the field + parent differ |

The two seam keys: ``comment`` is **nested** (NOT a flat top-level ``comments`` —
bug 0ee6), and ``issuelinks`` must be **present** (bug 3f04 carried none).

(The exact read-site functions per key are named in this module's tests and in the
inbound/outbound differ docstrings.)
"""

from __future__ import annotations

from typing import Any, TypedDict

import jsonschema

# ---------------------------------------------------------------------------
# TypedDicts — the canonical shape for readers (total=False: the enrichment
# keys are conditionally present; a top-level / link-less / unassigned issue
# omits parent / issuelinks-as-[] / a null assignee).
# ---------------------------------------------------------------------------


class JiraComment(TypedDict, total=False):
    author: dict[str, Any] | None
    body: dict[str, Any] | str  # ADF doc dict (REST v3) or plain text
    created: str
    updated: str
    id: str


class JiraCommentField(TypedDict, total=False):
    """The nested ``comment`` FIELD — bug 0ee6's seam (NOT a flat ``comments``)."""

    comments: list[JiraComment]
    total: int
    startAt: int
    maxResults: int


class JiraIssueLinkType(TypedDict, total=False):
    name: str
    inward: str
    outward: str


class JiraIssueLink(TypedDict, total=False):
    id: str
    type: JiraIssueLinkType
    inwardIssue: dict[str, Any]
    outwardIssue: dict[str, Any]


class JiraParent(TypedDict, total=False):
    key: str


class SnapshotEntry(TypedDict, total=False):
    """One ``snapshot[jira_key]`` entry — the producer↔consumer contract shape."""

    summary: str
    description: dict[str, Any] | str | None
    status: dict[str, Any] | str
    priority: dict[str, Any] | str | None
    assignee: dict[str, Any] | None
    labels: list[str]
    issuetype: dict[str, Any] | str
    comment: JiraCommentField
    issuelinks: list[JiraIssueLink]
    parent: JiraParent


# ---------------------------------------------------------------------------
# JSON Schema — the runtime-validated single source of truth. It constrains the
# SHAPE of each contract key (types + nesting), never specific values, and
# leaves ``additionalProperties`` open because a real Jira issue carries many
# more fields the reconciler does not read. The load-bearing constraints are:
#   * ``comment`` is an OBJECT with a ``comments`` ARRAY (the nested shape) —
#     a flat top-level ``comments`` array does NOT satisfy it (bug 0ee6);
#   * ``issuelinks`` is an ARRAY of link objects each with a ``type.name``
#     (bug 3f04);
#   * ``parent`` is ``{"key": str}``.
# ---------------------------------------------------------------------------

SNAPSHOT_ENTRY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "JiraSnapshotEntry",
    "type": "object",
    # A flat top-level ``comments`` key is the BUG-0ee6 shape (the differ read it;
    # the fetcher never writes it). Forbid it outright so validation catches a
    # producer that regresses to the flat shape — comments must live nested under
    # ``comment`` (constrained below).
    "not": {"required": ["comments"]},
    "properties": {
        "summary": {"type": "string"},
        "description": {"type": ["object", "string", "null"]},
        "status": {"type": ["object", "string"]},
        "priority": {"type": ["object", "string", "null"]},
        "assignee": {"type": ["object", "null"]},
        "labels": {"type": "array", "items": {"type": "string"}},
        "issuetype": {"type": ["object", "string"]},
        "comment": {
            "type": "object",
            "required": ["comments"],
            "properties": {
                "comments": {"type": "array", "items": {"$ref": "#/$defs/comment"}},
            },
        },
        "issuelinks": {"type": "array", "items": {"$ref": "#/$defs/issuelink"}},
        "parent": {
            "type": "object",
            "required": ["key"],
            "properties": {"key": {"type": "string"}},
        },
    },
    "$defs": {
        "comment": {
            "type": "object",
            "required": ["body"],
            "properties": {
                "author": {"type": ["object", "null"]},
                "body": {"type": ["object", "string"]},
            },
        },
        "issuelink": {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "inward": {"type": "string"},
                        "outward": {"type": "string"},
                    },
                },
                "inwardIssue": {"type": "object"},
                "outwardIssue": {"type": "object"},
            },
        },
    },
}

# The nine contract keys the differs read — the exact set the round-trip test
# must jointly exercise (story B AC: "scalar fields + comment/issuelinks/parent").
CONTRACT_KEYS: tuple[str, ...] = (
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
)

# The three enrichment keys merged by the fetcher (the historical seam bugs):
# parent (8b25), comment (0ee6/87e4), issuelinks (3f04).
ENRICHMENT_KEYS: tuple[str, ...] = ("parent", "comment", "issuelinks")


def validate_snapshot_entry(entry: dict[str, Any]) -> None:
    """Validate one snapshot entry against the canonical schema.

    Raises ``jsonschema.ValidationError`` on a shape/type violation. It catches the
    BUG-0ee6 shape directly — a flat top-level ``comments`` key is forbidden — and
    rejects a malformed ``comment``/``issuelinks``/``parent`` shape WHEN PRESENT.
    It does NOT (and cannot) catch BUG-3f04 (``issuelinks`` simply *absent*): a
    top-level / link-less issue legitimately omits it, so absence is not a schema
    violation — that producer regression is caught by the round-trip contract test
    (``test_producer_carries_enrichment_keys``), not here. Value content is never
    checked (not a change-detector).
    """
    jsonschema.validate(instance=entry, schema=SNAPSHOT_ENTRY_SCHEMA)
