"""Advance scalar baselines only for writes confirmed by the transport. Successful values use
the vendor payload; soft-failed transitions, comment fallback, stale bindings, parent-only
edits, and handler failures report no synced fields."""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest

_ENGINE = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"

from rebar.reconciler._errors import JiraAPIError  # noqa: E402
from rebar.reconciler.apply_handlers import BatchApplyContext, handle_update  # noqa: E402
from rebar.reconciler.dispatch_one import update_one  # noqa: E402

_ALL_FIVE = {
    "summary": "pushed title",
    "description": "pushed body",
    "priority": "High",
    "status": "Done",
    "assignee": "bob@x.com",
}


class _StubClient:
    """A transport whose ``update_issue`` outcome each test dictates."""

    def __init__(self, raises: Exception | None = None) -> None:
        self._raises = raises
        self.calls: list[dict[str, Any]] = []
        self.comments: list[str] = []

    def update_issue(self, jira_key: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(dict(fields))
        if self._raises is not None:
            raise self._raises
        return {"key": jira_key}

    def add_comment(self, jira_key: str, body: str) -> None:
        self.comments.append(body)


def _mutation(**ov: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "action": "update",
        "key": "REB-1",
        "local_id": "loc-1",
        "fields": dict(_ALL_FIVE),
    }
    m.update(ov)
    return m


def _run(client: _StubClient, mutation: dict[str, Any] | None = None) -> dict[str, Any]:
    synced: dict[str, Any] = {}
    update_one(mutation or _mutation(), client, fields_synced=synced)
    return synced


# --- the success arm ------------------------------------------------------------


def test_a_completed_write_reports_every_field_it_sent() -> None:
    """A completed update reports every vendor-shaped scalar field sent to Jira for baseline
    advancement."""
    client = _StubClient()
    synced = _run(client)

    for field, value in _ALL_FIVE.items():
        assert synced.get(field) == value, f"{field} was sent and landed, so it is synced"
    assert client.calls, "the transport was actually exercised"
    assert synced == client.calls[0], (
        "the reported set must be exactly what was handed to update_issue — not the "
        "mutation's pre-allowlist fields"
    )


# --- the soft-fail arms: nothing may be reported ---------------------------------


def test_an_unreachable_transition_reports_nothing() -> None:
    """A bare RuntimeError for an unreachable transition soft-fails the mutation and reports
    no synced fields."""
    client = _StubClient(raises=RuntimeError("no transition to 'Done' from 'To Do'"))
    synced: dict[str, Any] = {}

    with pytest.raises(RuntimeError):
        update_one(_mutation(), client, fields_synced=synced)

    assert synced == {}, (
        "a transition that never landed must not advance the baseline; recording it would "
        "assert a sync that did not happen, and that does NOT self-correct"
    )


def test_a_400_illegal_transition_comment_fallback_reports_nothing() -> None:
    """A 400 transition fallback posts a comment rather than a field edit, so it reports no
    synced fields."""
    client = _StubClient(raises=JiraAPIError("illegal transition", status_code=400))
    synced = _run(client)

    assert synced == {}, "the comment fallback wrote no fields, so none are synced"
    assert client.comments, "the fallback did post its divergence comment"


def test_a_404_on_a_stale_binding_reports_nothing() -> None:
    """A 404 means the Jira issue is gone; nothing could have landed."""
    client = _StubClient(
        raises=urllib.error.HTTPError("http://x", 404, "Not Found", None, None)  # type: ignore[arg-type]
    )
    synced: dict[str, Any] = {}

    with pytest.raises(urllib.error.HTTPError):
        update_one(_mutation(), client, fields_synced=synced)

    assert synced == {}


def test_a_parent_only_mutation_reports_nothing() -> None:
    """A parent-only mutation skips scalar update_issue and therefore reports no synced
    fields."""

    class _ParentClient(_StubClient):
        def set_parent(self, jira_key: str, parent_key: Any) -> None:
            return None

    client = _ParentClient()
    synced = _run(client, _mutation(fields={"parent": "REB-9"}))

    assert synced == {}
    assert client.calls == [], "no scalar update was issued"


# --- the handler seam: what reaches the batch context ----------------------------


def _ctx(tmp_path: Path, client: _StubClient) -> BatchApplyContext:
    return BatchApplyContext(client=client, repo_root=tmp_path, pass_id="p1")


def test_the_handler_records_a_successful_write_under_its_local_id(tmp_path: Path) -> None:
    """``handle_update`` is what feeds ``reconcile``; a landed write reaches it keyed by
    local_id, which is how ``_advance_baselines`` finds the binding to overlay."""
    ctx = _ctx(tmp_path, _StubClient())
    handle_update(_mutation(), ctx)

    assert ctx.synced_fields == {"loc-1": dict(_ALL_FIVE)}


def test_the_handler_records_nothing_for_a_soft_failed_mutation(tmp_path: Path) -> None:
    """The handler records no local-ID-keyed scalar baseline for a soft-failed mutation."""
    ctx = _ctx(tmp_path, _StubClient(raises=JiraAPIError("illegal transition", status_code=400)))
    handle_update(_mutation(), ctx)

    assert ctx.synced_fields == {}, (
        "a soft-failed mutation must contribute nothing to the baseline advance"
    )


def test_a_mutation_without_a_local_id_is_not_recorded(tmp_path: Path) -> None:
    """No local_id means no binding to overlay; the entry would be unusable anyway."""
    ctx = _ctx(tmp_path, _StubClient())
    handle_update(_mutation(local_id=None), ctx)

    assert ctx.synced_fields == {}
