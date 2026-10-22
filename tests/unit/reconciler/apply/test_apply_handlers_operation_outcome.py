"""Verify ``handle_update`` keeps summary-only updates on the legacy path.

`handle_update` sends every outbound update, a summary-only one included,
through `dispatch_one.update_one`, which makes one transport attempt (ADR 0103
S3 retired the generic handler-level retry). A confirmed write advances
`ctx.synced_fields`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rebar.reconciler.apply_handlers import BatchApplyContext, handle_update


class _StubClient:
    """A transport recording every legacy ``update_issue`` call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.comments: list[str] = []

    def update_issue(self, jira_key: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(dict(fields))
        return {"key": jira_key}

    def add_comment(self, jira_key: str, body: str) -> None:
        self.comments.append(body)


def _summary_mutation(new_summary: str, *, local_id: str = "loc-1") -> dict[str, Any]:
    return {
        "action": "update",
        "key": "REB-1",
        "local_id": local_id,
        "fields": {"summary": new_summary},
    }


def _ctx(tmp_path: Path, client: _StubClient) -> BatchApplyContext:
    return BatchApplyContext(client=client, repo_root=tmp_path, pass_id="p1")


def test_default_context_routes_summary_only_update_through_legacy(tmp_path: Path) -> None:
    """An exact-summary update takes the legacy transport path and advances the baseline."""
    client = _StubClient()
    ctx = _ctx(tmp_path, client)

    handle_update(_summary_mutation("new title"), ctx)

    assert client.calls == [{"summary": "new title"}], "legacy update_issue path was used"
    assert ctx.synced_fields == {"loc-1": {"summary": "new title"}}, "baseline advanced"
