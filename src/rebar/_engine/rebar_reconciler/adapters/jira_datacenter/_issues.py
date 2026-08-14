"""Core issue CRUD + status/label mutation mixin for the Jira Data Center
transport (ticket 465d, epic e369) — the ``TicketTransport`` capability.

Extracted from ``transport.py`` under the module-size cap (see ADR 0058); no behaviour change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rebar_reconciler._backend import BackendHTTPError
from rebar_reconciler.adapters.jira_datacenter._base import (
    _MISSING,
    _call_logged,
    _TransportBase,
    _unwrap,
)
from rebar_reconciler.adapters.jira_datacenter.retry import _with_connection_retry
from rebar_reconciler.adapters.jira_datacenter.transitions import (
    route_status_to_transition,
    transition_to_status,
)
from rebar_reconciler.adapters.jira_family import sanitize_summary as _sanitize_summary
from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec, cutover_clients

#: Bridge-schema keys the create payload carries for Cloud's ``AcliClient`` and that
#: Jira has no field for — forwarded as field ids they 400 the WHOLE create. Their
#: content is not lost: it is translated into ``summary``/``issuetype`` below.
_BRIDGE_ONLY_CREATE_FIELDS: frozenset[str] = frozenset(
    {"title", "ticket_type", "_bridge_target_project"}
)

#: Fields Jira refuses to SET at create time regardless of spelling. ``status`` is
#: not a rejected name — a status is reached by a workflow transition, never by a
#: create-time field write — so it is dropped here exactly as Cloud drops it, and
#: the outbound status lands later through ``route_status_to_transition``.
_UNSETTABLE_AT_CREATE_FIELDS: frozenset[str] = frozenset({"status"})


#: DC's REST v2 descriptions are plain text/wiki markup with the instance's
#: ``jira.text.field.character.limit`` cap; the codec is the one place that fit
#: is spelled, shared with the backend's description sanitizer.
#:
#: Built per CALL, not once at import: the rich-text cutover flag
#: (``reconciler.rich_text_cutover``, story 3388) is read at call time, and a
#: module-level codec would freeze whatever the flag said when this module was first
#: imported — which for a long-lived reconciler process is "whatever it was at boot".
def _description_codec() -> WikiTextCodec:
    return WikiTextCodec(rich="dc" in cutover_clients())


def _create_summary(ticket_data: dict[str, Any]) -> str:
    """Resolve the Jira ``summary`` from the create payload's two spellings.

    ``title`` is the bridge-side name (added by ``dispatch_one`` for Cloud) and
    ``summary`` is the differ's Jira-side name; both arrive in the same payload, so
    prefer the bridge value and fall back to the Jira one. An empty result RAISES
    rather than creating an untitled issue, because the create's whole purpose is to
    bind a local ticket to a recognisable remote one (Cloud raises here too).
    """
    stripped = ""
    for key in ("title", "summary"):
        # A whitespace-only value counts as absent, so a blank ``title`` falls
        # through to the differ's ``summary`` instead of aborting the create.
        candidate = ticket_data.get(key)
        if candidate is not None and str(candidate).strip():
            stripped = str(candidate).strip()
            break
    if not stripped:
        raise ValueError(
            "cannot create a Jira Data Center issue: neither 'title' nor 'summary' "
            f"carries a non-empty headline (payload keys: {sorted(ticket_data)})"
        )
    # Data Center hard-rejects an over-length summary with a 400 (measured against
    # 8.17.1); truncate rather than fail the pass on one oversize ticket.
    return _sanitize_summary(stripped)


def _create_issuetype(ticket_data: dict[str, Any]) -> dict[str, str]:
    """Resolve Jira's ``{"name": …}`` issue-type object from the create payload.

    Three shapes reach this function, and at least two of them in the SAME payload:
    ``ticket_type`` (a bridge-side string such as ``"task"``, capitalized the way
    Cloud's ``AcliClient.create_issue`` capitalizes it), and ``issuetype``, which the
    differ emits either as Jira's nested ``{"name": "Story"}`` or as a bare string.
    A payload that yields nothing usable defaults to ``Task``, matching Cloud.
    """
    bridge_type = ticket_data.get("ticket_type")
    if isinstance(bridge_type, str) and bridge_type.strip():
        return {"name": bridge_type.strip().capitalize()}
    jira_type = ticket_data.get("issuetype")
    if isinstance(jira_type, dict):
        # Already Jira-canonical (``{"name": "Sub-task"}``) — do NOT recapitalize it.
        name = str(jira_type.get("name") or "").strip()
        if name:
            return {"name": name}
    elif isinstance(jira_type, str) and jira_type.strip():
        return {"name": jira_type.strip()}
    return {"name": "Task"}


def _translate_create_fields(ticket_data: dict[str, Any]) -> dict[str, Any]:
    """Translate a dual-schema create payload into Jira's own field schema.

    Everything the payload carries that Jira DOES accept — ``priority``,
    ``assignee``, ``parent``, ``labels``, custom fields — passes through untouched;
    only the bridge-only names and the create-unsettable ones are dropped, and only
    ``summary``/``issuetype``/``description`` are rewritten. Narrowing this to an
    allowlist instead would silently drop real content the differ emitted.
    """
    fields = {
        name: value
        for name, value in ticket_data.items()
        if name not in _BRIDGE_ONLY_CREATE_FIELDS and name not in _UNSETTABLE_AT_CREATE_FIELDS
    }
    fields["summary"] = _create_summary(ticket_data)
    fields["issuetype"] = _create_issuetype(ticket_data)
    description = fields.get("description")
    if isinstance(description, str):
        # CREATE is a SECOND DC send path, distinct from the update path's mapper. It
        # must render too: fitting without ``to_wire`` would post raw Markdown on create
        # and rendered wiki on every later update. In plain mode both ops are the
        # identity, so this is byte-for-byte today's behaviour.
        codec = _description_codec()
        fields["description"] = codec.to_wire(codec.fit_outbound(description))
    # Jira's REST API wants OBJECTS for these two, and a live run is what said so — with the
    # bridge-only names fixed the create got further and failed differently:
    #   "priority":"Could not find valid 'id' or 'name' in priority object."
    #   "assignee":"data was not an object"
    # A bare string arrives because the SHARED outbound mapper has already resolved rebar's
    # integer priority to a Jira NAME, and because Data Center identifies users by ``name``
    # (never Cloud's accountId — see ``validate_assignee_exists``). ACLI accepts the bare forms,
    # which is why the Cloud path never needed this: the same per-transport seam again.
    # Wrapping only a STRING keeps this idempotent in shape — a caller that already passed the
    # object form must not end up with ``{"name": {"name": ...}}``, a third distinct 400 — and an
    # ABSENT field is never invented, which would assign the issue to nobody in particular.
    # ``parent`` wraps DIFFERENTLY: Jira identifies a parent by ``key``, not by ``name``. Reusing
    # one wrapper for all three would still 400, so the shape is per-field.
    for name, wrapper in (("priority", "name"), ("assignee", "name"), ("parent", "key")):
        if name not in fields:
            continue
        value = fields[name]
        if isinstance(value, str) and value:
            fields[name] = {wrapper: value}
        elif not value:
            # PRESENT BUT EMPTY is not the same as absent, and it is what the differ actually
            # emits for a ticket with no assignee: the key arrives carrying ``None``. Passing
            # that through sends a null where Jira expects an object and the whole create is
            # rejected — ``"assignee":"data was not an object"``. Dropping is the only correct
            # handling: there is no object that means "unassigned"/"no priority" at create time,
            # and inventing one would assign the issue to somebody.
            del fields[name]
    return fields


class _IssuesMixin(_TransportBase):
    """``create``/``read``/``update``/``delete``/``search`` + label + transition
    members — the always-present ``TicketTransport`` surface."""

    if TYPE_CHECKING:
        # Provided by the sibling ``_PeopleMixin``, resolved via the composed
        # transport's MRO. Declared type-only so mypy sees this mixin's surface.
        def _assign(self, remote_id: str, assignee: Any) -> None: ...

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        """Create an issue, TRANSLATING the payload into Jira's field schema first.

        The create payload carries TWO schemas at once: ``dispatch_one`` starts from
        the differ's Jira-shaped fields (``summary``, ``issuetype``, ``status``, …)
        and then ADDS the bridge-shaped names Cloud's client needs (``title``,
        ``ticket_type``), passing everything else through. Cloud's
        ``AcliClient.create_issue`` EXTRACTS the fields it wants and ignores the
        rest, so the duplication is harmless there. This method used to splat the
        whole dict into ``client.create_issue(**fields)``, which sent rebar's own
        field names to Jira as field ids and got the entire request rejected —
        ``HTTP 400 … Field 'ticket_type'/'title'/'status' cannot be set. It is not on
        the appropriate screen, or unknown.`` No issue meant no binding, so the
        failure surfaced three steps downstream as ``get_jira_key`` returning
        ``None`` (bug 18a5-2bd8-3e56-4bd8).

        The translation lives PER TRANSPORT — the same seam :meth:`update_issue`
        documents for ``status``→transition, and Data Center simply never got its
        half. :func:`_translate_create_fields` therefore drops the bridge-only names
        and ``status`` (not settable at create at all — a status is reached by a
        workflow transition), rewrites ``summary``/``issuetype``/``description`` into
        Jira's shapes, and leaves every other genuinely Jira-valid field alone.
        """
        fields = _translate_create_fields(ticket_data)
        fields.setdefault(
            "project",
            {"key": ticket_data.get("_bridge_target_project") or self.project},
        )
        issue = _with_connection_retry(lambda: self._client.create_issue(**fields))
        return _unwrap(issue)

    def get_issue(self, remote_id: str) -> dict[str, Any]:
        issue = _with_connection_retry(lambda: self._client.issue(remote_id))
        return _unwrap(issue)

    def update_issue(self, remote_id: str, **kwargs: Any) -> dict[str, Any]:
        """Apply an outbound field update, ROUTING ``status`` to a transition.

        ``status`` is not an editable Jira field. It arrives here anyway —
        ``dispatch_apply_phases._OUTBOUND_BATCH_ALLOWLIST`` contains it and
        ``dispatch_one._update_one_scalar_update`` forwards the whole allowlisted
        dict as ``update_issue(key, **fields)`` — and this method used to hand it
        straight to ``issue.update(fields=…)``, a REST field EDIT. Jira rejected it,
        the rejection was soft-failed, and the outbound status silently never
        changed (bug d067). Cloud does the same translation inside its own transport
        (``adapters/jira/acli.py:170,182-183``); the status→transition seam lives
        PER TRANSPORT, and Data Center simply never got its half.

        ``status`` and ``assignee`` are therefore both popped BEFORE the field edit,
        which then carries only genuinely editable fields — the field edit and the
        transition happen in this one call, in that order, so a mutation that
        changes a summary and a status still does both.
        """
        assignee = kwargs.pop("assignee", _MISSING)
        status = kwargs.pop("status", _MISSING)
        if kwargs:
            issue = _with_connection_retry(lambda: self._client.issue(remote_id))
            _with_connection_retry(lambda: issue.update(fields=kwargs))
        if assignee is not _MISSING:
            self._assign(remote_id, assignee)
        if status is not _MISSING and status is not None:
            route_status_to_transition(self._client, remote_id, str(status))
        return self.get_issue(remote_id)

    def transition_issue_by_name(self, remote_id: str, target_status: str) -> None:
        """Move ``remote_id`` to ``target_status``, resolving EITHER spelling.

        A transition's NAME is not its destination STATUS name, and every production
        caller passes the latter (bug 7f93); both resolve here. The resolution rules,
        the ambiguity refusal, and why they are what they are live with the code in
        :func:`transitions.resolve_transition`. Raises ``ValueError`` when no
        transition reaches the requested state — the error type callers already
        expect, so this stays a pure delegation.
        """
        transition_to_status(self._client, remote_id, target_status)

    def add_label(self, remote_id: str, label: str) -> None:
        """Append ``label`` without resetting the issue's existing labels.

        ``add_field_value`` lives on the ISSUE resource, not on the client:
        ``jira.JIRA`` has no such attribute (verified against jira 3.10.5), so the
        earlier client-level call raised ``AttributeError`` on every invocation —
        this method could never have worked. It shipped because it had no test at
        any tier; the live test that now covers it caught the bug on its first
        real execution. ``Issue.add_field_value(field, value)`` is documented as
        "add a value to a field that supports multiple values, without resetting
        the existing values ... should work with: labels", which is exactly the
        append semantics this method's callers expect (a read-modify-write of the
        whole ``labels`` list would clobber concurrent edits).
        """
        issue = _with_connection_retry(lambda: self._client.issue(remote_id))
        _with_connection_retry(lambda: issue.add_field_value("labels", label))

    def remove_label(self, remote_id: str, label: str) -> None:
        """Remove ONE label, leaving every other label intact.

        This is **not** the mirror image of :meth:`add_label`. ``add_label`` uses
        ``Issue.add_field_value("labels", …)``, which has no removal counterpart;
        removal goes through ``Issue.update``'s ``update`` verb —
        ``{"labels": [{"remove": <label>}]}`` — which is target-specific, so a
        concurrent label edit is not clobbered the way a read-modify-write of the
        whole list would clobber it.
        """
        issue = _call_logged("remove_label", remote_id, lambda: self._client.issue(remote_id))
        _call_logged(
            "remove_label",
            remote_id,
            lambda: issue.update(update={"labels": [{"remove": label}]}),
        )

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict[str, Any]]:
        results = _with_connection_retry(
            lambda: self._client.search_issues(jql, startAt=start_at, maxResults=max_results)
        )
        return [_unwrap(issue) for issue in results]

    # ------------------------------------------------------------------
    # The twelve members the core reaches for (story J9). Each is written
    # against DC REST **v2** via ``pycontribs/jira`` — never by copying Cloud's
    # v3 endpoint, and never by hand-rolled REST. Every one routes through
    # ``_call_logged`` so a failure at a call site that swallows
    # ``Exception`` still leaves a WARNING naming the member and the remote id.
    # ------------------------------------------------------------------

    def get_issue_by_rest(self, remote_id: str) -> dict[str, Any]:
        """Read an issue straight from the primary store (no search-index lag).

        On DC this is the SAME call ``get_issue`` makes — ``client.issue(key)`` is
        already ``GET /rest/api/2/issue/{key}``. Cloud needs the distinction only
        because its ``get_issue`` goes through ACLI's JQL search; DC has no such
        indirection, so the two coincide. The method still exists separately
        because ``outbound_differ`` calls it by name, and it is the ONE member of
        the twelve whose call site lets the error propagate.
        """
        issue = _call_logged("get_issue_by_rest", remote_id, lambda: self._client.issue(remote_id))
        return _unwrap(issue)

    def delete_issue(self, remote_id: str) -> dict[str, Any]:
        """Delete an issue (``Issue.delete()`` — ``DELETE /rest/api/2/issue/{key}``).

        A 404 is idempotent success (the post-state we want is "gone"), matching
        Cloud's contract; a 403 becomes ``PermissionError`` so the rollback callers
        that special-case a permissions denial keep behaving identically.
        """

        def _delete() -> None:
            issue = self._client.issue(remote_id)
            issue.delete()

        try:
            _call_logged("delete_issue", remote_id, _delete)
        except BackendHTTPError as exc:
            if exc.code == 404:
                return {"status": "already_absent", "key": remote_id}
            if exc.code == 403:
                raise PermissionError(
                    f"permission denied deleting {remote_id} on Jira Data Center: {exc}"
                ) from exc
            raise
        return {"status": "deleted", "key": remote_id}
