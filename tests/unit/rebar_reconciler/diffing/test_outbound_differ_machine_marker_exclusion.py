"""Axis 1 — machine-metadata comment exclusion in outbound_differ._diff_comments.

Bug 6afc-20ee-84e5-4dd5: the outbound comment-sync loop. Skill-to-skill ticket
comments (e.g. the ``PREPLANNING_CONTEXT:`` payload defined by
``src/rebar/_engine/docs/contracts/pil-handoff.md``) are machine-to-machine payloads
that must NEVER be mirrored outbound to Jira. They are large, internal, and
exist only to hand context between DSO skills. The differ must skip any local
comment whose normalised body starts with a bridge-internal machine-marker
prefix — mirroring the label ``_EXCLUDED_PREFIXES`` exclusion, but for comments.

Behavioural assertion: a local comment beginning with ``PREPLANNING_CONTEXT:``
produces NO outbound comment mutation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load_module("outbound_differ_machine_marker", OUTBOUND_DIFFER_PATH)


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


def _make_jira_snapshot_with_comments(jira_key: str, comment_bodies: list[str]) -> dict:
    jira_comments = [
        {"id": str(100 + i), "body": body} for i, body in enumerate(comment_bodies)
    ]
    return {
        jira_key: {
            "summary": "Some issue",
            "description": "desc",
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
            "comment": {"comments": jira_comments, "total": len(jira_comments)},
        }
    }


def _make_ticket_with_comments(ticket_id: str, comment_bodies: list[str]) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": "Some issue",
        "description": "desc",
        "status": "open",
        "priority": 2,
        "ticket_type": "bug",
        "assignee": "alice",
        "tags": [],
        "comments": [{"body": body} for body in comment_bodies],
        "deps": [],
    }


def test_preplanning_context_comment_now_emitted(
    outbound_differ: ModuleType,
) -> None:
    """Post-decouple: ``PREPLANNING_CONTEXT:`` was a DSO skill-to-skill marker.
    rebar no longer recognizes it, so such a comment is treated as ordinary and
    IS mirrored outbound (the exclusion list is trimmed to reconciler-internal
    markers only)."""
    jira_key = "DIG-6000"
    body = 'PREPLANNING_CONTEXT:{"epic":"e1","stories":["s1","s2"]}'
    ticket = _make_ticket_with_comments("local-mm-1", [body])
    store = StubBindingStore({"local-mm-1": jira_key})
    snapshot = _make_jira_snapshot_with_comments(jira_key, [])

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    comment_mutations = [m for m in result if m.comments]
    assert len(comment_mutations) == 1, (
        "A PREPLANNING_CONTEXT: comment is now an ordinary comment and must be "
        f"mirrored outbound. Got comment mutations: {comment_mutations}"
    )


def test_human_comment_still_emitted_alongside_excluded_marker(
    outbound_differ: ModuleType,
) -> None:
    """Exclusion is surgical: a genuine human comment in the same ticket is still
    synced, while a reconciler-internal machine-marker comment is skipped."""
    jira_key = "DIG-6001"
    human_body = "Investigating the root cause; will update shortly."
    marker_body = "BRIDGE_CANARY_ALERT: Still stale as of 2026-06-04T20:42:59Z: 3h ago."
    ticket = _make_ticket_with_comments("local-mm-2", [marker_body, human_body])
    store = StubBindingStore({"local-mm-2": jira_key})
    snapshot = _make_jira_snapshot_with_comments(jira_key, [])

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    comment_mutations = [m for m in result if m.comments]
    assert len(comment_mutations) == 1
    emitted = comment_mutations[0].comments
    assert len(emitted) == 1, (
        f"Exactly one (human) comment should be emitted; got {emitted}"
    )
    assert human_body in emitted[0]["body"]
    assert "BRIDGE_CANARY_ALERT" not in emitted[0]["body"]


def test_bridge_canary_alert_comments_not_emitted(
    outbound_differ: ModuleType,
) -> None:
    """Bug 57d1: the heartbeat canary appends a fresh-TIMESTAMPED
    ``BRIDGE_CANARY_ALERT: Still stale as of <ts>: ...`` comment every run. The
    volatile timestamp means the body never matches a prior Jira comment, so
    pre-fix it re-added every pass and accumulated duplicate Jira comments
    (20+ observed on DIG-5383). Two such comments with DIFFERENT timestamps must
    BOTH be excluded from outbound sync — proving the re-emitter is killed."""
    jira_key = "DIG-5383"
    c1 = "BRIDGE_CANARY_ALERT: Still stale as of 2026-06-04T20:42:59Z: Last successful run was 3h ago."
    c2 = "BRIDGE_CANARY_ALERT: Still stale as of 2026-06-04T23:29:33Z: Last successful run was 6h ago."
    ticket = _make_ticket_with_comments("local-canary", [c1, c2])
    store = StubBindingStore({"local-canary": jira_key})
    snapshot = _make_jira_snapshot_with_comments(jira_key, [])

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    comment_mutations = [m for m in result if m.comments]
    assert comment_mutations == [], (
        "BRIDGE_CANARY_ALERT: heartbeat comments (volatile timestamp) must be "
        "excluded from outbound sync so they stop re-emitting/accumulating in "
        f"Jira. Got comment mutations: {[m.comments for m in comment_mutations]}"
    )


def test_legacy_unmarked_canary_comment_not_emitted(
    outbound_differ: ModuleType,
) -> None:
    """57d1 follow-up: the BRIDGE_CANARY_ALERT: marker only tags FUTURE canary
    comments. The existing unmarked backlog ("Still stale as of <ts>: ...")
    already on the alert ticket must ALSO be excluded by its legacy content
    prefix, or it keeps re-emitting (DIG-5383 observed still re-emitting after
    the marker-only fix landed)."""
    jira_key = "DIG-5383"
    legacy = "Still stale as of 2026-06-04T23:29:33Z: Last successful run was 6h 12m ago (threshold: 2h). Run: https://example/actions/runs/1"
    ticket = _make_ticket_with_comments("local-legacy-canary", [legacy])
    store = StubBindingStore({"local-legacy-canary": jira_key})
    snapshot = _make_jira_snapshot_with_comments(jira_key, [])

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    comment_mutations = [m for m in result if m.comments]
    assert comment_mutations == [], (
        "Legacy unmarked 'Still stale as of' canary comments must also be "
        "excluded so the existing backlog stops re-emitting. Got: "
        f"{[m.comments for m in comment_mutations]}"
    )
