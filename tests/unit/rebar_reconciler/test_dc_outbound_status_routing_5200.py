"""DIAGNOSIS repro: the OUTBOUND status never reaches a transition on Data Center (ticket 5200).

WHY THIS EXISTS, AND WHY 7f93 DID NOT COVER IT. Bug 7f93 fixed
``JiraDataCenterTransport.transition_issue_by_name`` so a destination STATUS name
(``"In Progress"``) resolves to the transition that reaches it (``Start Progress``). That fixed
the INBOUND cell, whose setup calls ``transition_issue_by_name`` DIRECTLY. It cannot fix the
OUTBOUND cell, because the outbound apply path never calls that method at all.

THE OUTBOUND PATH, end to end:
  * ``dispatch_apply_phases._OUTBOUND_BATCH_ALLOWLIST`` (``dispatch_apply_phases.py:41``)
    contains ``"status"``, so ``_update_one_filter_fields`` KEEPS it;
  * ``dispatch_one._update_one_scalar_update`` (``dispatch_one.py:646``) forwards the whole
    allowlisted dict as ``client.update_issue(issue_key, **fields)``;
  * on Cloud, ``adapters/jira/acli.py:170-183`` pops ``status`` out of the kwargs and routes it
    to ``transition_issue`` -> ``transition_issue_by_name``. That is where the status→transition
    translation lives — in the ACLI transport, NOT in the shared dispatch path;
  * on Data Center, ``adapters/jira_datacenter/transport.py:248-255`` pops ONLY ``assignee`` and
    puts everything else into ``issue.update(fields=kwargs)`` — a REST field EDIT. ``status`` is
    not an editable field in Jira; it is only reachable through a transition.

So the DC transport is missing the status→transition routing its Cloud sibling has, and the
outbound status is submitted as if it were a text field.

THIS MODULE STARTED AS A DIAGNOSIS ARTEFACT AND IS NOW THE REGRESSION PIN (bug d067 fixed).
``JiraDataCenterTransport.update_issue`` now pops ``status`` and routes it through
``transitions.route_status_to_transition``. The first test, which used to PIN the defect, is
inverted with its intent preserved; the second lost its ``xfail(strict=True)`` marker. The
rest guard the ways a naive fix goes wrong: dropping the co-submitted editable fields,
turning an illegal-from-here transition into a pass-fatal error (in both of the shapes Jira
serves it), and over-broadening that softening until a real outage is hidden.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from rebar_reconciler._backend import BackendHTTPError
from rebar_reconciler.adapters.jira_datacenter.transport import (
    IllegalTransitionError,
    JiraDataCenterTransport,
)

# The CLASSIC workflow the Data Center harness actually serves — same fixture shape as
# test_dc_transition_by_status_heldout.py, deliberately keeping `Done` as both a transition
# name and a destination status.
_CLASSIC = [
    {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
    {"id": "31", "name": "Done", "to": {"name": "Done"}},
]


class _FakeIssue:
    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink

    def update(self, fields: dict[str, Any]) -> None:
        # The real pycontribs `Issue.update` PUTs these as `{"fields": ...}` to
        # /rest/api/2/issue/<key>. Recording rather than raising keeps the two failure
        # modes distinguishable: what the transport SENT vs. what the server would reject.
        self._sink.append(dict(fields))


class _FakeClient:
    def __init__(self) -> None:
        self.edited_fields: list[dict[str, Any]] = []
        self.transitioned: list[tuple[str, str]] = []
        self.transitions_probed: list[str] = []

    def issue(self, remote_id: str) -> Any:
        return _FakeIssue(self.edited_fields)

    def transitions(self, remote_id: str) -> list[dict[str, Any]]:
        self.transitions_probed.append(remote_id)
        return _CLASSIC

    def transition_issue(self, remote_id: str, transition_id: str) -> None:
        self.transitioned.append((remote_id, transition_id))


def _transport() -> JiraDataCenterTransport:
    return JiraDataCenterTransport(client=_FakeClient(), project="DIG")


def test_the_dc_transport_never_edits_status_as_a_field() -> None:
    """INVERTED (bug d067 fixed). ``update_issue(key, status=...)`` must not field-EDIT.

    THIS TEST USED TO PIN THE DEFECT, and its inversion is the point. It formerly asserted
    ``edited_fields == [{"status": "In Progress"}]`` and ``transitions_probed == []`` — the
    exact mechanism behind the live cell's ``fields.status.name is 'To Do'`` — so that the
    diagnosis was falsifiable in one file: fix the routing and this test fails while the
    xfail below turns green, and the pair says exactly what moved. That is what happened.

    The intent worth keeping is the NEGATIVE half, so it is kept as a negative: the assertion
    that no ``status`` key ever reaches the REST field-edit payload, and that the transitions
    endpoint IS probed. It is not the same claim as the test below (which pins WHICH
    transition fires); this one would still catch a fix that routed the transition correctly
    but ALSO left `status` in the field edit — a partial fix Jira would keep rejecting.
    """
    transport = _transport()
    transport.update_issue("DIG-1", status="In Progress")
    client = transport._client

    assert client.edited_fields == [], (
        "`status` is not an editable Jira field, so the REST field-edit payload must be empty "
        f"when status was the only kwarg. Got edited_fields={client.edited_fields!r}"
    )
    assert client.transitions_probed == ["DIG-1"], (
        "the outbound status path must probe the transitions endpoint to resolve the target "
        f"state. Got transitions_probed={client.transitions_probed!r}"
    )
    assert client.transitioned != [], (
        "a transition must actually be dispatched, not merely probed. "
        f"Got transitioned={client.transitioned!r}"
    )


def test_an_outbound_status_should_dispatch_the_transition_that_reaches_it() -> None:
    """WHAT THE LIVE OUTBOUND CELL NEEDS. ``status="In Progress"`` must fire ``Start Progress``.

    Asserting the dispatched transition ID rather than "nothing raised" — a transport that
    resolved the WRONG transition would satisfy a raises-nothing oracle while parking the issue
    in the wrong state, which is the exact shape of the bug this epic keeps rediscovering.
    """
    transport = _transport()
    transport.update_issue("DIG-1", status="In Progress")
    client = transport._client

    assert client.transitioned == [("DIG-1", "11")], (
        "the outbound status 'In Progress' must resolve to transition 'Start Progress' (id 11) "
        f"and be dispatched; got transitioned={client.transitioned!r} and "
        f"edited_fields={client.edited_fields!r}"
    )
    assert not any("status" in f for f in client.edited_fields), (
        "`status` must never be submitted through the field-edit endpoint — Jira rejects it; "
        f"got edited_fields={client.edited_fields!r}"
    )


def test_a_status_and_an_editable_field_are_both_applied_in_one_call() -> None:
    """Routing status must not cost the OTHER fields in the same mutation.

    The outbound differ emits one mutation carrying every changed field, and
    ``dispatch_one._update_one_scalar_update`` forwards the whole allowlisted dict in a
    single ``update_issue`` call. So the fix has two halves that a status-only test cannot
    tell apart: ``status`` must leave the field-edit payload AND everything else must stay
    in it. Assigning is checked here too, because ``assignee`` was the ONE kwarg this method
    already popped — regressing it while adding a second pop is the obvious way to break it.
    """
    transport = _transport()
    client = transport._client
    assigned: list[tuple[str, Any]] = []
    client.assign_issue = lambda key, who: assigned.append((key, who))  # type: ignore[attr-defined]

    transport.update_issue("DIG-1", status="In Progress", summary="a new summary", assignee="jdoe")

    assert client.edited_fields == [{"summary": "a new summary"}], (
        "the non-status editable fields must still go through the field edit, and only "
        f"those. Got edited_fields={client.edited_fields!r}"
    )
    assert client.transitioned == [("DIG-1", "11")], (
        f"the status must still be dispatched as a transition; got {client.transitioned!r}"
    )
    assert assigned == [("DIG-1", "jdoe")], (
        f"the pre-existing assignee routing must be unchanged; got {assigned!r}"
    )


class _RefusingClient(_FakeClient):
    """A DC client whose workflow offers no route to the requested state.

    Jira lists ONLY the transitions legal from the issue's CURRENT state, so
    "illegal from here" is what an empty/short transitions payload means on the wire.
    """

    def transitions(self, remote_id: str) -> list[dict[str, Any]]:
        self.transitions_probed.append(remote_id)
        return [{"id": "31", "name": "Done", "to": {"name": "Done"}}]


def test_an_illegal_transition_is_non_fatal_and_lands_in_bridge_alerts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable state records a bridge alert and lets the BATCH continue.

    SILENT SOFT-FAILURE IS WHAT LET BUG d067 SURVIVE, so "it did not raise" is not the
    contract — "it did not raise AND it left a record" is. Both halves are asserted, and
    both are asserted through the REAL sequencer ``applier._apply_one`` rather than by
    inspecting the exception, because the property is about what the pass does.

    The mechanism the sequencer keys on: ``applier.py:757`` re-raises
    ``urllib.error.HTTPError`` ABOVE the per-mutation backstop as a fail-fast contract.
    ``BackendHTTPError`` subclasses it, so a transport that let Jira's raw rejection escape
    would abort the whole pass over one issue sitting in an unexpected state — the shape of
    bug 449f-f9bf-be90-47fe. ``IllegalTransitionError`` is deliberately not an HTTPError, so
    it reaches ``record_backstop_failure`` and becomes a ``bridge_alerts`` entry instead.
    The follower mutation is the load-bearing half of "non-fatal".
    """
    from rebar_reconciler import applier, apply_handlers

    transport = JiraDataCenterTransport(client=_RefusingClient(), project="DIG")
    reached: list[str] = []

    def _update(mutation_key: str, **fields: Any) -> Any:
        if mutation_key == "DIG-1":
            return transport.update_issue(mutation_key, status="In Progress")
        reached.append(mutation_key)
        return {"key": mutation_key}

    monkeypatch.setattr(apply_handlers, "update_one", lambda m, c, **kw: _update(m["key"]))
    ctx = apply_handlers.BatchApplyContext(
        client=object(), repo_root=tmp_path, pass_id="d067-test-pass"
    )

    outcomes: list[dict[str, Any]] = []
    for mutation in (
        {"action": "update", "key": "DIG-1", "local_id": "aaaa-bbbb-cccc-dddd"},
        {"action": "update", "key": "DIG-2", "local_id": "1111-2222-3333-4444"},
    ):
        applier._apply_one(mutation, ctx, outcomes)

    assert reached == ["DIG-2"], (
        "an illegal transition must not abort the pass — the mutation behind it must still "
        f"dispatch; reached={reached!r}"
    )
    alert_dir = tmp_path / "bridge_state" / "bridge_alerts"
    alerts = sorted(alert_dir.glob("*.jsonl"))
    assert alerts, (
        "the illegal transition must be OBSERVABLE: a bridge_alerts record is the only "
        "signal an operator gets, and its absence is the silent soft-failure this bug "
        f"was made of. Nothing was written under {alert_dir}"
    )
    body = alerts[0].read_text(encoding="utf-8")
    assert "In Progress" in body and "DIG-1" in body, (
        "the alert must name the issue and the status that could not be applied, or an "
        f"operator cannot act on it; got {body!r}"
    )


class _RejectingClient(_FakeClient):
    """Offers the transition, then has Jira REJECT the execute call.

    The other way "illegal from here" reaches us: the transition is listed but a
    condition/validator on it fails, so ``POST .../transitions`` answers 400. Verified
    against pycontribs/jira 3.10.5 at runtime — ``JIRA.transitions`` and
    ``JIRA.transition_issue`` are both real client-level methods, and
    ``_with_connection_retry`` translates the library's ``JIRAError`` into
    ``BackendHTTPError`` before it reaches ``route_status_to_transition``.
    """

    def __init__(self, code: int, msg: str) -> None:
        super().__init__()
        self._code = code
        self._msg = msg

    def transition_issue(self, remote_id: str, transition_id: str) -> None:
        raise BackendHTTPError("", self._code, self._msg, None, None)  # type: ignore[arg-type]


def test_a_400_illegal_transition_becomes_a_non_fatal_error() -> None:
    """A 400 rejection must not stay an HTTPError, or it aborts the whole pass.

    ``BackendHTTPError`` subclasses ``urllib.error.HTTPError``, which
    ``applier._apply_one`` re-raises above its per-mutation backstop as a fail-fast
    contract. Letting Jira's raw 400 escape here would therefore kill the pass over one
    issue in an unexpected state. The type assertion is the mechanism, not a style check.
    """
    transport = JiraDataCenterTransport(
        client=_RejectingClient(400, "Illegal transition"), project="DIG"
    )

    with pytest.raises(IllegalTransitionError) as excinfo:
        transport.update_issue("DIG-1", status="In Progress")

    assert not isinstance(excinfo.value, urllib.error.HTTPError), (
        "an illegal transition must not surface as an HTTPError — applier.py:757 re-raises "
        f"that type above the per-mutation backstop; got {type(excinfo.value).__mro__}"
    )
    assert "In Progress" in str(excinfo.value) and "DIG-1" in str(excinfo.value)


def test_a_non_illegal_http_error_still_propagates_fail_fast() -> None:
    """CONTRAST CASE: only the 400 illegal-transition shape is softened.

    Guards against over-broadening the fix into "swallow every failure of a transition",
    which would hide a real outage — a 502 from the Jira node is not the issue being in the
    wrong state, and the pass should still fail loudly. Same line ``handle_update`` draws.
    """
    transport = JiraDataCenterTransport(client=_RejectingClient(502, "Bad Gateway"), project="DIG")

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        transport.update_issue("DIG-1", status="In Progress")
    assert excinfo.value.code == 502
    assert not isinstance(excinfo.value, IllegalTransitionError)
