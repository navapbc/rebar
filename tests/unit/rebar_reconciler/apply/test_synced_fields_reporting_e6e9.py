"""Bug e6e9 — the apply layer must report only writes that CONFIRMEDLY landed.

``reconcile._advance_baselines`` advances the ADR-0026 baseline to what rebar SYNCED, and
this is the layer that decides what "synced" means. The operator decision on this ticket
made the granularity a hard requirement: the advance must key off PER-MUTATION success, not
the pass exit code and not the absence of an exception at pass level. A status transition
can soft-fail while the pass still exits 0 — ``transition_issue_by_name`` raises a bare
``RuntimeError`` for an unreachable transition (caught by the applier's per-mutation
backstop), a 400 illegal-transition is answered with a comment instead of the edit, and a
404 / unresolved assignee soft-fails the whole mutation.

Advancing the baseline for a write that did not land is strictly WORSE than the bug being
fixed: today's clobber self-corrects (the baseline advances to Jira's value, local then
differs, the next pass re-pushes), whereas a falsely-advanced baseline makes rebar believe
local and Jira agree when they do not, and never self-corrects.

These cells drive the real ``dispatch_one.update_one`` / ``apply_handlers.handle_update``
against a stub transport, asserting the reported set on each arm.
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

_ENGINE = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"
if str(_ENGINE) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_ENGINE))

from rebar_reconciler._errors import JiraAPIError  # noqa: E402
from rebar_reconciler.apply_handlers import BatchApplyContext, handle_update  # noqa: E402
from rebar_reconciler.dispatch_one import update_one  # noqa: E402

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
    """The happy path: ``update_issue`` returned, so the fields it carried are synced.

    This is the ONLY arm that may report anything. The reported values must be the
    vendor-shaped ones actually handed to the transport, because that is the shape
    ``peer_state.set_baseline`` / ``normalize_baseline_value`` consume — a mismatch would
    make the overlay compare against a value Jira never held.
    """
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
    """The production soft-fail from the ticket: a bare ``RuntimeError``.

    ``acli.transition_issue_by_name`` raises this when no legal transition reaches the
    target status. It propagates to the applier's per-mutation backstop, which records a
    failure and lets the pass continue at exit 0 — so the pass-level signal is useless
    here and only the per-mutation one is honest.
    """
    client = _StubClient(raises=RuntimeError("no transition to 'Done' from 'To Do'"))
    synced: dict[str, Any] = {}

    with pytest.raises(RuntimeError):
        update_one(_mutation(), client, fields_synced=synced)

    assert synced == {}, (
        "a transition that never landed must not advance the baseline; recording it would "
        "assert a sync that did not happen, and that does NOT self-correct"
    )


def test_a_400_illegal_transition_comment_fallback_reports_nothing() -> None:
    """The 400 arm posts a COMMENT instead of the edit — nothing was written.

    This is the subtle one: the pass exits 0, ``update_one`` returns normally, and the
    outcome carries no error. Only the fact that ``fields_synced`` is written on the far
    side of ``update_issue`` keeps this arm honest.
    """
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
    """No scalar write was issued at all, so there is nothing to sync.

    ``update_one`` deliberately skips the otherwise-empty ``update_issue`` call when the
    only changed field was ``parent``. Reporting here would advance the baseline off a
    call that never happened.
    """

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
    """The end-to-end soft-fail contract at the layer reconcile actually consumes.

    Two independent gates have to hold for this to stay empty: ``fields_synced`` is only
    written past a completed ``update_issue``, AND the handler's record site sits past
    every soft-fail return. Either one alone would be enough; asserting here pins the
    composition, which is what the operator decision required.
    """
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
