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

THE MECHANISM, AND THE ROUTE THAT WAS REFUTED. Epic membership on DC is not ``fields.parent`` at
all — it is the "Epic Link" custom field, whose ``customfield_NNNNN`` id differs per instance.
Change 1302 wrote it through the Agile API (``add_issues_to_epic`` under
``agile_rest_path="greenhopper"``), reasoning from the transport's own docstring. **Harness run
30840572608 refuted that**: DC 8.17.1 answers ``POST /rest/greenhopper/1.0/epic/{key}/issue`` with
HTTP 404 "null for uri" — the endpoint does not exist, so it was never the epic-ID-versus-KEY
ambiguity that had been anticipated. The Epic Link is an ordinary custom field, so these cells pin
an ordinary field update instead, with the id DISCOVERED BY NAME rather than hardcoded.

Whether that write takes effect against DC 8.17.1 is settled on the live harness, not here — per
this epic's rule that the harness is the arbiter, and because 1302 had 2047 green unit tests.

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

    ``subtask`` decides the issue type the transport reads back. ``epic_calls`` records
    ``add_issues_to_epic`` calls — the REFUTED Agile-API route, kept on the double precisely so a
    cell can assert it is NOT taken. ``field_calls`` counts field-discovery round trips.

    ``epic_link_name`` lets a cell simulate an instance where "Epic Link" cannot be discovered
    (Jira Software absent, or a renamed field), which must DECLINE rather than fall back to
    ``fields.parent``.
    """

    def __init__(self, *, subtask: bool, epic_link_name: str | None = "Epic Link") -> None:
        self.updates: list[dict[str, Any]] = []
        self.epic_calls: list[tuple[str, list[str]]] = []
        self.field_calls = 0
        self._subtask = subtask
        self._epic_link_name = epic_link_name

    def issue(self, remote_id: str) -> _FakeIssue:
        return _FakeIssue(
            {"key": remote_id, "fields": {"issuetype": {"subtask": self._subtask}}},
            self.updates,
        )

    def fields(self) -> list[dict[str, Any]]:
        """``GET /rest/api/2/field`` — the instance's field inventory.

        Shaped like the real response: the Epic Link id is instance-specific, so the transport
        must MATCH ON NAME and must not hardcode a customfield number. The decoys are here so a
        substring match or a "first custom field" heuristic fails this cell.
        """
        self.field_calls += 1
        out: list[dict[str, Any]] = [
            {"id": "summary", "name": "Summary", "custom": False},
            {"id": "customfield_10001", "name": "Epic Name", "custom": True},
            {"id": "customfield_10008", "name": "Epic Status", "custom": True},
        ]
        if self._epic_link_name is not None:
            out.append({"id": "customfield_10014", "name": self._epic_link_name, "custom": True})
        return out

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

    assert {"call": "update", "fields": {"customfield_10014": "RBJ-1"}} in client.updates, (
        "the epic parent was not written to the instance-discovered 'Epic Link' custom field; "
        f"the transport recorded updates={client.updates!r} and epic_calls={client.epic_calls!r}"
    )
    assert not any("parent" in (u.get("fields") or {}) for u in client.updates), (
        f"fields.parent was written for a NON-sub-task, which DC no-ops: {client.updates!r}"
    )
    assert client.epic_calls == [], (
        "the transport used add_issues_to_epic, the route harness run 30840572608 REFUTED: DC "
        "8.17.1 answered POST /rest/greenhopper/1.0/epic/RBJONBO-1/issue with HTTP 404 "
        f"'null for uri' — the endpoint does not exist. Got {client.epic_calls!r}"
    )


def test_clearing_an_epic_parent_nulls_the_epic_link_field() -> None:
    """A CLEAR must null the same field a SET writes.

    ``dispatch_one`` routes a clear through the identical ``set_parent`` call with a falsy key, so
    if the clear took a different path it would diverge from the set. The refuted route expressed
    this with greenhopper's sentinel epic ``"none"``; the custom-field route expresses it as
    ``None``, which is how Jira clears any field.
    """
    client = _FakeClient(subtask=False)

    _transport(client).set_parent("RBJ-5", None)

    assert {"call": "update", "fields": {"customfield_10014": None}} in client.updates, (
        f"clearing an epic parent must null the Epic Link field; got {client.updates!r}"
    )
    wrote_sentinel = any(
        u.get("fields", {}).get("customfield_10014") == "none" for u in client.updates
    )
    assert not wrote_sentinel, (
        "the greenhopper sentinel string 'none' leaked into the custom-field route; on this path "
        f"a clear is a JSON null, and the literal string would set a bogus epic: {client.updates!r}"
    )


def test_the_epic_link_field_is_discovered_once_not_per_call() -> None:
    """Field discovery is an extra REST round trip; it must not happen per mutation.

    An outbound pass can carry many parent writes. Rediscovering the field id on each one turns a
    single reparent into two calls and would show up as rate-limit pressure on a large pass — the
    concern ticket b586 already had to address for this transport.
    """
    client = _FakeClient(subtask=False)
    transport = _transport(client)

    transport.set_parent("RBJ-5", "RBJ-1")
    transport.set_parent("RBJ-6", "RBJ-1")
    transport.set_parent("RBJ-7", None)

    assert client.field_calls == 1, (
        f"the Epic Link field id must be discovered once per transport, got {client.field_calls} "
        "discovery calls across three parent writes"
    )


def test_a_client_without_fields_declines_rather_than_raising_attributeerror() -> None:
    """A client that cannot enumerate fields must DECLINE, not blow up.

    ADDED AFTER THE FACT, and worth saying why: the six cells around it all passed while this was
    broken. The route this replaced guarded its client capability with
    ``getattr(self._client, "add_issues_to_epic", None)`` and declined when absent; the first cut
    of the replacement called ``self._client.fields()`` unguarded, so an injected client lacking it
    raised ``AttributeError`` — which reaches ``dispatch_one``'s bare ``except Exception`` as an
    UNCLASSIFIED failure and, since change 1305, is alerted as ``outbound-parent-failed`` rather
    than the accurate ``outbound-parent-unrepresentable``.

    Nothing in the happy-path suite demanded this, because every double here has ``fields()``. It
    was surfaced by the composed gate-overlap cell, which was written for a DIFFERENT implementation
    and so had no reason to accommodate this one.
    """

    class _NoFields:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        def issue(self, remote_id: str) -> _FakeIssue:
            return _FakeIssue(
                {"key": remote_id, "fields": {"issuetype": {"subtask": False}}}, self.updates
            )

    client = _NoFields()

    try:
        JiraDataCenterTransport(client=client, project="RBJ").set_parent("RBJ-5", "RBJ-1")
    except NotImplementedError:
        pass
    except AttributeError as exc:  # pragma: no cover - the regression this cell exists for
        raise AssertionError(
            "set_parent raised AttributeError instead of the attributed decline; the client "
            f"capability is unguarded: {exc!r}"
        ) from exc
    else:
        raise AssertionError(
            f"set_parent silently accepted an epic parent with no way to find the field: "
            f"{client.updates!r}"
        )

    assert not any("parent" in (u.get("fields") or {}) for u in client.updates), (
        f"declined, but still wrote fields.parent — DC would no-op it: {client.updates!r}"
    )


def test_an_undiscoverable_epic_link_declines_and_never_writes_fields_parent() -> None:
    """When the field cannot be found, DECLINE — do not fall back to ``fields.parent``.

    This is the cell that keeps the fix honest. An instance without Jira Software, or with the
    field renamed, has no Epic Link to write. Falling back to ``fields.parent`` would be silently
    no-op'd by DC, which is the precise failure this whole method exists to refuse — and since
    change 1305 the resulting NotImplementedError is what raises an
    ``outbound-parent-unrepresentable`` alert instead of vanishing.
    """
    client = _FakeClient(subtask=False, epic_link_name=None)

    try:
        _transport(client).set_parent("RBJ-5", "RBJ-1")
    except NotImplementedError as exc:
        assert "Epic Link" in str(exc), f"the decline must name the missing field; got {exc!r}"
    else:
        raise AssertionError(
            "set_parent silently accepted an epic parent on an instance with no Epic Link field; "
            f"updates={client.updates!r}"
        )

    assert not any("parent" in (u.get("fields") or {}) for u in client.updates), (
        f"declined, but still wrote fields.parent — DC would no-op it: {client.updates!r}"
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

    # Mechanism updated after harness run 30840572608 refuted the Agile route (HTTP 404); the
    # cell's INTENT — that the two gates intersect for a real configuration — is unchanged, and
    # this still fails if the apply side declines the shape the emit gate produces.
    assert {"call": "update", "fields": {"customfield_10014": "RBJ-1"}} in client.updates, (
        "the emit gate emitted a parent that the DC apply side still cannot carry — the two gates "
        f"remain DISJOINT, which is this ticket's defect. updates={client.updates!r}"
    )
