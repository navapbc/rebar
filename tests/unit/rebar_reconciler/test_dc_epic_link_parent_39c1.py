"""The outbound parent can never reach Data Center: the two gates are disjoint (ticket 39c1).

THE DEFECT, as two policies that are each defensible alone.

  * THE EMIT SIDE WANTS AN EPIC PARENT. ``outbound_field_diff._resolve_local_parent`` omits the
    parent field entirely unless the local parent's ``ticket_type`` is ``epic`` — bug 8b25's
    hierarchy guard, because Jira Cloud permits only Epic parents.
  * THE APPLY SIDE WANTS A SUB-TASK CHILD. ``JiraDataCenterTransport.set_parent`` declines with
    ``NotImplementedError`` for any child that is not a sub-task, correctly refusing to write
    ``fields.parent`` where DC would silently no-op it.

A DC sub-task's parent is a STANDARD issue, which imports as local ``ticket_type`` ``"task"``. So
the one child shape the apply side accepts can only have a parent shape the emit side refuses. The
sets never intersect and NO reconcile pass can emit an outbound parent-set that DC would accept.
Confirmed live before this module existed (ticket 39c1-2a32-b564-4b4b):
``test_outbound_clear_parent_round_trips`` — the local parent was detached and DC still carried
``fields.parent``.

THE FIX UNDER TEST is option B, recorded on the ticket: teach the DC apply side the EPIC LINK, and
leave the emit gate untouched. That intersects the sets on the shape the emit gate already prefers,
and it keeps bug 8b25's Cloud behaviour unchanged by not editing a line of it.

THE MECHANISM DETAIL THAT MAKES THIS NON-OBVIOUS, from the transport's own docstring: epic
membership on DC is not ``fields.parent`` at all — it is the "Epic Link" custom field, written
through the Agile API, which **DC serves under the ``greenhopper`` REST path while pycontribs'
``AGILE_BASE_REST_PATH`` defaults to ``agile``**. So a naive ``add_issues_to_epic`` call targets a
path DC does not expose, and the fix is only complete when the client is built for ``greenhopper``.
Whether that call succeeds against DC 8.17.1 is NOT established from documentation and is settled on
the live harness, per this epic's rule that the harness is the arbiter.

WHY THE LAST CELL MATTERS INDEPENDENTLY. This is the FIFTH instance of "Cloud has the translation,
DC never got its half" (d067, 8d68, 751e, 2b16/88d9), and every one was a SILENT success: no
traceback, no alert, the pass reports OK. ``dispatch_one`` swallows ``set_parent``'s exception, so
even the loud decline is invisible. A parent that genuinely cannot be represented must therefore
surface an ATTRIBUTED signal — that cell carries value even if the platform refuses the Epic Link
write outright.
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport


class _FakeIssue:
    def __init__(self, raw: dict[str, Any], sink: list[dict[str, Any]]) -> None:
        self.raw = raw
        self._sink = sink

    def update(self, fields: dict[str, Any]) -> None:
        self._sink.append({"call": "update", "fields": fields})


class _FakeClient:
    """A ``jira.JIRA``-shaped double recording which write path the transport chose.

    ``subtask`` decides the issue type the transport reads back. ``epic_calls`` records Agile-API
    epic-link writes so a cell can assert the transport chose the EPIC LINK rather than
    ``fields.parent`` — the distinction the whole ticket turns on.
    """

    def __init__(self, *, subtask: bool) -> None:
        self.updates: list[dict[str, Any]] = []
        self.epic_calls: list[tuple[str, list[str]]] = []
        self._subtask = subtask

    def issue(self, remote_id: str) -> _FakeIssue:
        return _FakeIssue(
            {"key": remote_id, "fields": {"issuetype": {"subtask": self._subtask}}},
            self.updates,
        )

    def add_issues_to_epic(self, epic_id: str, issue_keys: list[str]) -> None:
        self.epic_calls.append((epic_id, list(issue_keys)))


def _transport(client: _FakeClient) -> JiraDataCenterTransport:
    return JiraDataCenterTransport(client=client, project="RBJ")


# ---------------------------------------------------------------------------
# THE DEFECT — RED until the Epic Link path exists
# ---------------------------------------------------------------------------


def test_epic_parent_on_a_non_subtask_is_written_as_an_epic_link() -> None:
    """THE DEFECT. A non-sub-task child with an epic parent must be written, not declined.

    This is the exact shape the emit gate produces — it emits ONLY for an epic parent — and the
    exact shape the apply side refuses today, which is why no parent ever reaches DC. Pre-fix this
    raises ``NotImplementedError``, so the RED message names the decline rather than an absent
    attribute.

    The assertion is on the EPIC LINK call, not merely on "no exception": writing ``fields.parent``
    for a non-sub-task would satisfy a no-exception oracle while DC silently no-ops it, which is the
    failure mode the original decline was written to prevent. Do not weaken this to a raises-check.
    """
    client = _FakeClient(subtask=False)

    _transport(client).set_parent("RBJ-5", "RBJ-1")

    assert client.epic_calls == [("RBJ-1", ["RBJ-5"])], (
        "the epic parent was not written through the Agile API epic-link path; the transport "
        f"recorded epic_calls={client.epic_calls!r} and updates={client.updates!r}. An epic parent "
        "on DC is the 'Epic Link' custom field, NOT fields.parent."
    )
    assert not any("parent" in (u.get("fields") or {}) for u in client.updates), (
        f"fields.parent was written for a NON-sub-task, which DC no-ops: {client.updates!r}"
    )


def test_subtask_parent_still_uses_fields_parent() -> None:
    """REGRESSION. A sub-task's parent genuinely lives in ``fields.parent`` — do not reroute it.

    The fix must ADD the epic-link path, not replace the working one. A sub-task written through the
    epic-link API would be a new silent no-op in the opposite direction.
    """
    client = _FakeClient(subtask=True)

    _transport(client).set_parent("RBJ-9", "RBJ-2")

    assert {"call": "update", "fields": {"parent": {"key": "RBJ-2"}}} in client.updates, (
        f"a sub-task's parent must still be written via fields.parent; got {client.updates!r}"
    )
    assert client.epic_calls == [], (
        f"a sub-task's parent was rerouted to the epic-link API: {client.epic_calls!r}"
    )


def test_the_emit_gate_and_dc_apply_now_overlap() -> None:
    """THE TICKET'S HEADLINE AC: the two gates must intersect for at least one real configuration.

    Composed rather than asserted about one side, because each side in isolation looks correct and
    the defect is only visible where they meet. The emit gate is driven for real — an EPIC parent,
    which is the only shape it ever emits — and the resulting parent key is handed to the DC apply
    side, which must accept it.
    """
    from rebar_reconciler.outbound_field_diff import _resolve_local_parent

    class _Bindings:
        def get_jira_key(self, local_id: str) -> str | None:
            return {"local-epic": "RBJ-1"}.get(local_id)

    emit, parent_key = _resolve_local_parent(
        {"parent_id": "local-epic"},
        binding_store=_Bindings(),
        local_ticket_types={"local-epic": "epic"},
    )
    assert emit and parent_key == "RBJ-1", (
        f"the emit gate refused an EPIC parent, which contradicts bug 8b25's guard: "
        f"emit={emit}, parent_key={parent_key!r}"
    )

    client = _FakeClient(subtask=False)
    _transport(client).set_parent("RBJ-5", parent_key)

    assert client.epic_calls == [("RBJ-1", ["RBJ-5"])], (
        "the emit gate emitted a parent that the DC apply side still cannot carry — the two gates "
        f"remain DISJOINT, which is this ticket's defect. epic_calls={client.epic_calls!r}"
    )
