"""Honest hermetic BIDIRECTIONAL round-trip — comments, links, parent.

Epic f89d, story F (`822a-2bad-fe48-43e0`). The live e2e probe encoded the
inbound-comment bug as EXPECTED (`NOT-SYNCED`) and no probe round-tripped
relationships at all. This is the hermetic replacement: it round-trips comments,
issue-links (both directions) and parent through the PRODUCTION code path using
story A's REAL captured fixtures (not hand-built dicts like
test_reconcile_roundtrip.py), runnable in CI without live Jira.

The two production legs (the read-only fake can't write, so the outbound leg uses a
recording write-client through the production applier):

  * INBOUND  (Jira -> local): FakeAcliClient -> ``fetcher.compute_snapshot`` ->
    ``inbound_differ.compute_inbound_mutations``.
  * OUTBOUND (local -> Jira): ``outbound_differ.compute_outbound_mutations`` ->
    ``batch_dispatch.update_one`` against a recording client.

The round-trip property asserted is **convergence**: derive the local steady state
from what inbound actually emits, then neither differ re-emits — a value present on
BOTH sides yields ZERO spurious mutation in EITHER direction (no oscillation). A
key/shape divergence (bug 0ee6 / 3f04) breaks this. See
docs/adr/0004-reconciler-snapshot-contract.md.
"""

from __future__ import annotations

import copy

import pytest
from _fakes import install

pytestmark = pytest.mark.integration

_COMMENT_KEY = "comment"


def _adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class _StubBindingStore:
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


def _make_local(ticket_id: str, **over) -> dict:
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


class _RecordingClient:
    """Write-capable client for the OUTBOUND leg (the fake is read-only)."""

    def __init__(self) -> None:
        self.add_comment_calls: list[tuple] = []
        self.set_relationship_calls: list[tuple] = []
        self.set_parent_calls: list[tuple] = []

    def update_issue(self, key, **fields):
        return {"key": key}

    def set_parent(self, key, parent_key):
        self.set_parent_calls.append((key, parent_key))

    def add_comment(self, key, body):
        self.add_comment_calls.append((key, body))

    def get_issue_links(self, key):
        return []

    def set_relationship(self, from_key, to_key, link_type="Blocks"):
        self.set_relationship_calls.append((from_key, to_key, link_type))


# REB-431 (a Story): parent REB-430, three outward Blocks links to REB-426/427/428.
_BINDINGS = {
    "loc-431": "REB-431",
    "loc-430": "REB-430",
    "loc-426": "REB-426",
    "loc-427": "REB-427",
    "loc-428": "REB-428",
}


@pytest.fixture
def snapshot(monkeypatch) -> dict:
    from rebar_reconciler import fetcher

    install(monkeypatch, fetcher)
    monkeypatch.setenv("JIRA_PROJECT", "REB")
    return fetcher.compute_snapshot("roundtrip-pass")


def _inbound(snapshot, bind, locals_by_id):
    from rebar_reconciler import inbound_differ

    muts, _ = inbound_differ.compute_inbound_mutations(snapshot, bind, locals_by_id)
    return muts


# ---------------------------------------------------------------------------
# INBOUND leg — Jira state reflected into a local that lacks it.
# ---------------------------------------------------------------------------


def test_inbound_reflects_links_and_parent(snapshot) -> None:
    bind = _StubBindingStore(_BINDINGS)
    locals_by_id = {lid: _make_local(lid) for lid in _BINDINGS}
    a = next(m for m in _inbound(snapshot, bind, locals_by_id) if m.local_id == "loc-431")
    targets = {lk["target_id"] for lk in (getattr(a, "links", []) or [])}
    assert {"loc-426", "loc-427", "loc-428"} <= targets, "inbound did not reflect issuelinks"
    assert a.fields.get("parent_id") == "loc-430", "inbound did not reflect parent"


# ---------------------------------------------------------------------------
# OUTBOUND leg — a local that has state Jira lacks pushes it via the applier.
# ---------------------------------------------------------------------------


def test_outbound_pushes_comment_link_parent_through_applier() -> None:
    from rebar_reconciler import batch_dispatch, outbound_differ

    bind = _StubBindingStore({"loc-a": "DIG-1", "loc-b": "DIG-2", "loc-p": "DIG-9"})
    a = _make_local(
        "loc-a",
        parent_id="loc-p",
        deps=[{"target_id": "loc-b", "relation": "blocks", "link_uuid": "u1"}],
        comments=[{"body": "local-only comment"}],
    )
    # Jira hierarchy only permits Epic parents — a non-epic parent diff is
    # suppressed by the outbound differ (ticket 8b25), so loc-p is an epic
    # (REB-431's real parent REB-430 is itself an epic).
    locals_list = [a, _make_local("loc-b"), _make_local("loc-p", ticket_type="epic")]
    # Jira side has NONE of them (empty comment field + empty issuelinks, no parent),
    # so the outbound differ sees a divergence to push.
    jira_snapshot = {
        "DIG-1": {
            "summary": "local title",
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "assignee": None,
            "labels": [],
            _COMMENT_KEY: {"comments": []},
            "issuelinks": [],
        },
        "DIG-2": {"summary": "b", "issuelinks": [], _COMMENT_KEY: {"comments": []}},
        "DIG-9": {"summary": "p", "issuelinks": [], _COMMENT_KEY: {"comments": []}},
    }
    muts, _ = outbound_differ.compute_outbound_mutations(locals_list, jira_snapshot, bind)
    a_mut = next((m for m in muts if m.local_id == "loc-a"), None)
    assert a_mut is not None, f"outbound emitted no mutation for loc-a: {muts}"

    # Drive the emitted mutation through the PRODUCTION applier with a write client.
    # Build the batch dict exactly as production does (reconcile.py converts an
    # OutboundMutation update to a typed Mutation payload {changed_fields, comments,
    # labels, links} which _mutation_to_batch_dict flattens to this shape).
    client = _RecordingClient()
    batch = {
        "action": "update",
        "direction": "outbound",
        "key": a_mut.jira_key,
        "fields": dict(a_mut.fields),  # parent rides here as a changed field
        "local_id": a_mut.local_id,
        "follow_on": None,
        "comments": list(a_mut.comments or []),
        "labels": list(a_mut.labels or []),
        "links": list(getattr(a_mut, "links", []) or []),
    }
    batch_dispatch.update_one(batch, client)

    assert client.set_parent_calls == [("DIG-1", "DIG-9")], "parent not pushed outbound"
    assert ("DIG-1", "DIG-2", "Blocks") in client.set_relationship_calls, "link not pushed"
    assert any("local-only comment" in c[1] for c in client.add_comment_calls), (
        f"comment not pushed outbound: {client.add_comment_calls}"
    )


# ---------------------------------------------------------------------------
# ROUND-TRIP convergence — derive steady state from inbound, then neither
# direction re-emits (fixed point; the no-oscillation property).
# ---------------------------------------------------------------------------


def test_roundtrip_converges_links_and_parent(snapshot) -> None:
    from rebar_reconciler import outbound_differ

    bind = _StubBindingStore(_BINDINGS)

    # 1. Inbound on a local that lacks the state — capture what it would apply.
    empty_locals = {lid: _make_local(lid) for lid in _BINDINGS}
    a = next(m for m in _inbound(snapshot, bind, empty_locals) if m.local_id == "loc-431")
    applied_deps = [
        {"target_id": lk["target_id"], "relation": lk["relation"], "link_uuid": "u"}
        for lk in (getattr(a, "links", []) or [])
    ]
    applied_parent = a.fields.get("parent_id")
    assert applied_deps and applied_parent, "inbound produced nothing to converge on"

    # 2. Build the local STEADY STATE = exactly what inbound said to apply.
    mirrored = {lid: _make_local(lid) for lid in _BINDINGS}
    mirrored["loc-431"] = _make_local("loc-431", deps=applied_deps, parent_id=applied_parent)

    # 3a. Inbound must now emit NOTHING for loc-431 (no re-pull / oscillation).
    re_inbound = next(
        (m for m in _inbound(snapshot, bind, mirrored) if m.local_id == "loc-431"), None
    )
    re_links = list(getattr(re_inbound, "links", []) or []) if re_inbound else []
    re_parent = re_inbound.fields.get("parent_id") if re_inbound else None
    assert not re_links, f"inbound re-emitted links after convergence: {re_links}"
    assert re_parent is None, f"inbound re-emitted a parent change after convergence: {re_parent}"

    # 3b. Outbound must also emit NOTHING for loc-431 (no spurious re-push).
    out, _ = outbound_differ.compute_outbound_mutations(list(mirrored.values()), snapshot, bind)
    out_a = next((m for m in out if m.local_id == "loc-431"), None)
    out_links = list(getattr(out_a, "links", []) or []) if out_a else []
    assert not out_links, f"outbound re-pushed links after convergence: {out_links}"
    out_parent = (out_a.fields or {}).get("parent") if out_a else None
    assert not out_parent, f"outbound re-pushed a parent change after convergence: {out_parent}"


def test_roundtrip_comments_echo_suppressed_native_synced(snapshot) -> None:
    """Comment round-trip both ways through the production inbound path.

    The fixed point for comments is ECHO SUPPRESSION: a comment the reconciler
    pushed outbound (carrying RECONCILER_MARKER) must NOT round-trip back inbound
    (no oscillation). Conversely a genuinely Jira-side (native) comment MUST flow to
    local. Inject one native comment alongside the real (all-echo) fixture comments
    and assert inbound emits an add for exactly the native one.
    """
    from rebar_reconciler import inbound_differ

    bind = _StubBindingStore(_BINDINGS)
    locals_by_id = {lid: _make_local(lid) for lid in _BINDINGS}

    # Real REB-431 fixture comments are all reconciler echoes (RECONCILER_MARKER).
    snap = copy.deepcopy(snapshot)
    fixture_comments = snap["REB-431"][_COMMENT_KEY]["comments"]
    assert fixture_comments, "fixture must carry comments to exercise echo suppression"
    # A genuinely Jira-side comment (no marker) the reconciler never wrote.
    native = {"id": "native-1", "body": _adf("a genuinely native jira comment")}
    snap["REB-431"][_COMMENT_KEY]["comments"] = [*fixture_comments, native]

    a = next(
        m
        for m in inbound_differ.compute_inbound_mutations(snap, bind, locals_by_id)[0]
        if m.local_id == "loc-431"
    )
    adds = list(getattr(a, "comments", []) or [])
    # Exactly the native comment is pulled in; every echo is suppressed (fixed point).
    assert [c.get("jira_comment_id") for c in adds] == ["native-1"], (
        f"inbound must sync ONLY the native comment and suppress all echoes; got {adds}"
    )
