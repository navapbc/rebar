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

THIS MODULE IS A DIAGNOSIS ARTEFACT, NOT THE FIX. The first test PINS the observed defective
behaviour (so it is green on today's code and names the mechanism precisely); the second
asserts the behaviour the outbound cell needs and is EXPECTED TO FAIL until the product bug is
fixed, so it is marked ``xfail(strict=True)`` — which means it also fails the build the moment
the bug IS fixed, forcing the marker off rather than leaving a stale skip behind.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

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


def test_the_dc_transport_edits_status_as_a_field_instead_of_transitioning() -> None:
    """PINS THE DEFECT. ``update_issue(key, status=...)`` becomes a field EDIT on DC.

    This is a characterization test: it asserts what the code does TODAY, which is the
    mechanism behind the live cell's ``fields.status.name is 'To Do'``. It is deliberately
    NOT an assertion of desired behaviour — see the xfail below for that. Keeping both makes
    the diagnosis falsifiable in one file: if a future change routes status correctly, THIS
    test fails and the xfail below turns green, and the pair says exactly what moved.
    """
    transport = _transport()
    transport.update_issue("DIG-1", status="In Progress")
    client = transport._client

    assert client.edited_fields == [{"status": "In Progress"}], (
        "expected the defect: the DC transport puts `status` into the REST field-edit payload. "
        f"Got edited_fields={client.edited_fields!r}"
    )
    assert client.transitions_probed == [], (
        "expected the defect: no transitions probe happens at all on the outbound status path. "
        f"Got transitions_probed={client.transitions_probed!r}"
    )
    assert client.transitioned == [], (
        "expected the defect: no transition is dispatched. "
        f"Got transitioned={client.transitioned!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PRODUCT DEFECT (ticket 5200 -> filed as its own bug): JiraDataCenterTransport."
        "update_issue has no status->transition routing, unlike its Cloud sibling "
        "adapters/jira/acli.py:182-183. Remove this marker when the bug is fixed."
    ),
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
