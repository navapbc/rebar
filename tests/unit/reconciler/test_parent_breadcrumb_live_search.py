"""Emit parent-drift breadcrumbs even with zero local comments (9ebb).

When Jira search supplies comments, a bound under-defined child gets one
append-once breadcrumb. Unknown state from no client or failed fetch suppresses
it. No drift makes no needless fetch. Dedup against the fetched set."""

from __future__ import annotations

from typing import Any

from rebar.reconciler import outbound_comments as oc

PARENT_BREADCRUMB_TAG = "<!-- rebar:parent-breadcrumb -->"


class _FakeBinding:
    """Minimal binding-store stand-in: local_id -> bound Jira key (or None)."""

    def __init__(self, keys: dict[str, str | None]) -> None:
        self._keys = keys

    def get_jira_key(self, local_id: str) -> str | None:
        return self._keys.get(local_id)

    def is_comment_mapped(self, local_comment_key: str) -> bool:
        return False


class _FakeClient:
    """Transport stand-in recording ``get_comments`` calls.

    ``fail=True`` raises on fetch (the bug-4292 unknown-state path).
    """

    def __init__(self, comments: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self._comments = comments or []
        self._fail = fail
        self.calls: list[str] = []

    def get_comments(self, jira_key: str) -> list[dict[str, Any]]:
        self.calls.append(jira_key)
        if self._fail:
            raise RuntimeError("boom: live fetch failed")
        return self._comments


def _live_snapshot(jira_key: str) -> dict[str, Any]:
    """Live search-result shape: fields WITHOUT the ``comment`` key."""
    return {jira_key: {"summary": "Some issue", "labels": []}}


def _breadcrumbs(mutations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in mutations if PARENT_BREADCRUMB_TAG in (m.get("body") or "")]


def _drift_kwargs(bs: _FakeBinding) -> dict[str, Any]:
    """The S7 ancestor maps for a child C with a type-collapsed bound parent P1."""
    return {
        "binding_store": bs,
        "local_parents": {"C": "P1"},
        "local_ticket_types": {"P1": "task"},  # non-epic => parent field suppressed
    }


def test_breadcrumb_emitted_live_path_zero_local_comments() -> None:
    """RED for bug 9ebb-3114: a drift-affected child with NO local comments and
    an available client gets exactly one breadcrumb in the live-search path.
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}
    client = _FakeClient([])

    out = oc._diff_comments(ticket, "PROJ-1", _live_snapshot("PROJ-1"), client, **_drift_kwargs(bs))

    bc = _breadcrumbs(out)
    assert len(bc) == 1, f"expected exactly one breadcrumb, got {len(bc)}: {out!r}"
    body = bc[0]["body"]
    assert bc[0]["action"] == "add"
    assert "PROJ-42" in body, "breadcrumb must name the nearest represented ancestor's key"
    assert oc.RECONCILER_MARKER in body, "breadcrumb must carry the echo marker"
    assert client.calls == ["PROJ-1"], "the dedup set must come from ONE live fetch"


def test_no_breadcrumb_live_path_when_client_none() -> None:
    """Bug-4292 safety: no client ⇒ Jira comment state unknown ⇒ no breadcrumb."""
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}

    out = oc._diff_comments(ticket, "PROJ-1", _live_snapshot("PROJ-1"), None, **_drift_kwargs(bs))

    assert out == [], "unknown comment state (no client) must emit nothing"


def test_no_breadcrumb_live_path_when_fetch_fails() -> None:
    """Bug-4292 safety: a failing live fetch ⇒ state unknown ⇒ no breadcrumb."""
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}
    client = _FakeClient(fail=True)

    out = oc._diff_comments(ticket, "PROJ-1", _live_snapshot("PROJ-1"), client, **_drift_kwargs(bs))

    assert _breadcrumbs(out) == [], "a failed live fetch must emit no breadcrumb"
    assert out == [], "a failed live fetch must emit no comment mutations at all"


def test_append_once_against_live_fetched_set() -> None:
    """An already-landed tagged breadcrumb in the LIVE-FETCHED comment set
    suppresses a second one (append-once keys on the stable tag).
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}
    existing = (
        "This ticket's parent hierarchy could not be fully represented in Jira. "
        "Nearest tracked ancestor: PROJ-OLD. Full parent context is maintained in rebar.\n"
        + PARENT_BREADCRUMB_TAG
        + "\n\n"
        + oc.RECONCILER_MARKER
    )
    client = _FakeClient([{"id": "500", "body": existing}])

    out = oc._diff_comments(ticket, "PROJ-1", _live_snapshot("PROJ-1"), client, **_drift_kwargs(bs))

    assert _breadcrumbs(out) == [], "an existing tagged breadcrumb must suppress a second one"


def test_no_fetch_when_zero_comments_and_no_drift() -> None:
    """Counter-regression: zero local comments and NO drift condition ⇒ the
    client is never consulted (the existing skip-the-unnecessary-API-call
    behaviour of the live-search path is preserved).
    """
    bs = _FakeBinding({"C": "PROJ-1", "P1": "PROJ-42"})
    ticket = {"ticket_id": "C", "parent_id": "P1", "comments": []}
    client = _FakeClient([])

    out = oc._diff_comments(
        ticket,
        "PROJ-1",
        _live_snapshot("PROJ-1"),
        client,
        binding_store=bs,
        local_parents={"C": "P1"},
        local_ticket_types={"P1": "epic"},  # epic parent => representable => no drift
    )

    assert out == []
    assert client.calls == [], "no drift and nothing to compare ⇒ no live fetch"
