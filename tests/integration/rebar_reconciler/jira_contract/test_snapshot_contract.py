"""Producer↔consumer snapshot contract — the keystone that closes the bug class.

Epic f89d, story B (`8937-eed3-c881-4ea1`). The fetcher (PRODUCER) and the differs
(CONSUMERS) share one implicit contract — the per-issue snapshot-entry shape — that
bugs 0ee6 (nested ``comment`` read as flat ``comments``) and 3f04 (``issuelinks``
never carried) silently violated. This module pins it two ways, BOTH through the
PRODUCTION code path (the FakeAcliClient's real fixtures → ``fetcher.fetch_snapshot``
→ the differs), so a key/shape divergence fails a test immediately:

  1. **Schema conformance** — every ``fetch_snapshot`` entry validates against the
     canonical ``_snapshot_schema`` (all nine consumer-read keys, shape/type only).
  2. **Semantic round-trip** — the inbound differ, run on the produced snapshot,
     actually READS ``comment`` / ``issuelinks`` / ``parent`` (+ a scalar exemplar):
     its emitted mutations change with the producer's content, and would be EMPTY
     under the pre-fix shapes (proving the test would have caught 0ee6 / 3f04).

Assertions are semantic (keys/types/round-trip), never whole-blob golden equality
or mock-call-count. See docs/adr/0004-reconciler-snapshot-contract.md.
"""

from __future__ import annotations

import copy

import jsonschema
import pytest
from _fakes import install

pytestmark = pytest.mark.integration

# Local<->Jira bindings for the REB fixture neighbourhood. REB-431 (a Story) has
# parent REB-430 and three outward Blocks links to REB-426/427/428; binding those
# targets lets the inbound link reverse-map and parent-map resolve to local ids.
_BINDINGS = {
    "loc-431": "REB-431",
    "loc-430": "REB-430",
    "loc-426": "REB-426",
    "loc-427": "REB-427",
    "loc-428": "REB-428",
}


class _StubBindingStore:
    """Both-direction binding over one local_id<->jira_key map (per the harness)."""

    def __init__(self, bindings: dict[str, str]) -> None:
        self._l2j = dict(bindings)
        self._j2l = {v: k for k, v in bindings.items()}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._l2j.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._l2j

    def get_local_id(self, jira_key: str) -> str | None:
        return self._j2l.get(jira_key)

    # baseline arbitration surface (always-on since story d6bd)
    def is_pending(self, local_id: str) -> bool:
        return False

    def get_baseline(self, local_id: str) -> dict | None:
        return None


def _make_local(ticket_id: str, **over: object) -> dict:
    base = {
        "ticket_id": ticket_id,
        "title": "local title",
        "description": "A local description long enough to be realistic.",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": None,
        "tags": [],
        "comments": [],
        "deps": [],
        "parent_id": None,
    }
    base.update(over)
    return base


@pytest.fixture
def produced_snapshot(monkeypatch) -> dict:
    """The real production snapshot built from fixtures through fetch_snapshot."""
    from rebar_reconciler import fetcher

    install(monkeypatch, fetcher)
    monkeypatch.setenv("JIRA_PROJECT", "REB")
    return fetcher.compute_snapshot("contract-pass")


# ---------------------------------------------------------------------------
# 1. Schema conformance — the producer output matches the canonical shape.
# ---------------------------------------------------------------------------


def test_fetch_snapshot_conforms_to_schema(produced_snapshot) -> None:
    from rebar_reconciler import _snapshot_schema

    assert produced_snapshot, "producer built an empty snapshot"
    for key, entry in produced_snapshot.items():
        try:
            _snapshot_schema.validate_snapshot_entry(entry)
        except jsonschema.ValidationError as exc:  # noqa: PERF203 — per-entry context
            pytest.fail(f"{key} violates the snapshot-entry schema: {exc.message}")


def test_schema_rejects_flat_comments_prefix_shape(produced_snapshot) -> None:
    """The schema catches the BUG-0ee6 shape: a flat top-level ``comments`` key.

    Take a real producer entry, move its nested ``comment`` to a flat ``comments``
    key (the exact pre-fix divergence), and assert the schema now REJECTS it.
    """
    from rebar_reconciler import _snapshot_schema

    entry = copy.deepcopy(produced_snapshot["REB-431"])
    _snapshot_schema.validate_snapshot_entry(entry)  # the real shape is valid
    entry["comments"] = entry.pop("comment")["comments"]  # regress to flat shape
    with pytest.raises(jsonschema.ValidationError):
        _snapshot_schema.validate_snapshot_entry(entry)


def test_producer_carries_enrichment_keys(produced_snapshot) -> None:
    """The producer emits the post-fix nested-comment / issuelinks / parent shapes.

    This is the producer half of "would fail on the pre-fix conditions": pre-3f04
    the snapshot carried NO ``issuelinks``; pre-0ee6 comments were keyed flat. The
    assertions below regress to red if the fetcher reverts to either shape. They
    assert SHAPE/presence, not specific values (the fixtures will drift on
    re-capture; only the contract shape is frozen).
    """
    entry = produced_snapshot["REB-431"]
    # comment: nested object with a comments list (NOT a flat top-level "comments").
    assert "comments" not in entry, "comments must be nested under 'comment', not flat"
    assert isinstance(entry.get("comment"), dict) and "comments" in entry["comment"]
    # issuelinks: present (bug 3f04 carried none), each a REST-nested type.name shape.
    assert isinstance(entry.get("issuelinks"), list) and entry["issuelinks"]
    assert all(lk.get("type", {}).get("name") for lk in entry["issuelinks"])
    # parent: {"key": <non-empty str>}.
    assert isinstance(entry.get("parent"), dict)
    assert isinstance(entry["parent"].get("key"), str) and entry["parent"]["key"]


# ---------------------------------------------------------------------------
# 2. Semantic round-trip — the consumer READS each producer key (production path).
# ---------------------------------------------------------------------------


def test_inbound_reads_issuelinks(produced_snapshot) -> None:
    """Inbound reverse-maps the producer's ``issuelinks`` into local link adds."""
    from rebar_reconciler import inbound_differ

    bind = _StubBindingStore(_BINDINGS)
    locals_by_id = {lid: _make_local(lid) for lid in _BINDINGS}
    muts, _ = inbound_differ.compute_inbound_mutations(produced_snapshot, bind, locals_by_id)

    a = next((m for m in muts if m.local_id == "loc-431"), None)
    links = list(getattr(a, "links", []) or []) if a else []
    # The inbound link contract key is ``target_id`` (inbound_differ._diff_links_inbound).
    targets = {lk["target_id"] for lk in links}
    assert {"loc-426", "loc-427", "loc-428"} <= targets, (
        f"inbound did not reflect the producer's issuelinks; loc-431 links={links}"
    )


def test_inbound_reads_parent(produced_snapshot) -> None:
    """Inbound maps the producer's ``parent.key`` to the bound local parent."""
    from rebar_reconciler import inbound_differ

    bind = _StubBindingStore(_BINDINGS)
    locals_by_id = {lid: _make_local(lid) for lid in _BINDINGS}
    muts, _ = inbound_differ.compute_inbound_mutations(produced_snapshot, bind, locals_by_id)

    a = next((m for m in muts if m.local_id == "loc-431"), None)
    assert a is not None, "inbound emitted no mutation for loc-431"
    # parent is emitted under the local ``parent_id`` key, resolved via the binding.
    assert a.fields.get("parent_id") == "loc-430", (
        f"inbound did not read parent into loc-431: {a.fields}"
    )


def test_inbound_reads_comment_field_and_would_miss_prefix(produced_snapshot) -> None:
    """Inbound reads the nested ``comment.comments`` list.

    REB-431's fixture comments are all reconciler ECHOES (carry RECONCILER_MARKER),
    so a faithful inbound pass suppresses them — emitting zero comment adds. We
    prove the field is actually READ (not merely key-absent) by stripping the
    marker from the bodies: the emitted-add count jumps to the real comment count.
    Under the PRE-FIX flat-``comments`` shape the count would be zero in BOTH runs
    — which is exactly the divergence this contract catches.
    """
    from rebar_reconciler import inbound_differ

    bind = _StubBindingStore(_BINDINGS)
    locals_by_id = {lid: _make_local(lid) for lid in _BINDINGS}

    def _comment_adds(snap: dict) -> int:
        muts, _ = inbound_differ.compute_inbound_mutations(snap, bind, locals_by_id)
        a = next((m for m in muts if m.local_id == "loc-431"), None)
        return len(getattr(a, "comments", []) or []) if a else 0

    n_comments = len(produced_snapshot["REB-431"]["comment"]["comments"])
    baseline = _comment_adds(produced_snapshot)
    # At least one echo is suppressed (REB-431's comments are reconciler echoes),
    # so the read count is below the raw comment count — proving the field is read
    # AND that echo suppression runs. (Direction-only; not pinned to exactly 0, so
    # a future human comment on REB-431 doesn't spuriously red this test.)
    assert baseline < n_comments

    # Strip the echo marker (value-level only; nested shape untouched) → the real
    # comments now read as Jira-native and inbound emits one add each. The count
    # STRICTLY INCREASES, which is only possible if comment.comments was read.
    deecho = copy.deepcopy(produced_snapshot)
    for c in deecho["REB-431"]["comment"]["comments"]:
        c["body"] = {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}],
        }
    deecho_adds = _comment_adds(deecho)
    assert deecho_adds == n_comments and deecho_adds > baseline, (
        "inbound did not read comment.comments (stripping the echo marker should "
        f"raise the add count: baseline={baseline} deecho={deecho_adds} n={n_comments})"
    )

    # PRE-FIX simulation: move comments to a flat top-level "comments" key (drop the
    # nested "comment"). The consumer must now see NOTHING — the bug 0ee6 failure.
    prefix = copy.deepcopy(deecho)
    prefix["REB-431"]["comments"] = prefix["REB-431"].pop("comment")["comments"]
    assert _comment_adds(prefix) == 0, "flat 'comments' must be invisible to the consumer"


def test_inbound_reads_scalar_summary(produced_snapshot) -> None:
    """Scalar exemplar: inbound reads ``summary`` (all scalars read uniformly)."""
    from rebar_reconciler import inbound_differ

    bind = _StubBindingStore(_BINDINGS)
    jira_summary = produced_snapshot["REB-431"]["summary"]
    locals_by_id = {lid: _make_local(lid) for lid in _BINDINGS}
    locals_by_id["loc-431"] = _make_local("loc-431", title="a stale local summary")
    muts, _ = inbound_differ.compute_inbound_mutations(produced_snapshot, bind, locals_by_id)

    a = next((m for m in muts if m.local_id == "loc-431"), None)
    # The Jira ``summary`` is read into the local ``title`` field.
    assert a is not None and a.fields.get("title") == jira_summary, (
        f"inbound did not read the producer's summary into loc-431: {a.fields if a else None}"
    )
