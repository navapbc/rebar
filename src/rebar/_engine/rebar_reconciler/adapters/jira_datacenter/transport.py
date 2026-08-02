"""``JiraDataCenterTransport`` — the DC ``TicketTransport`` built on ``pycontribs/jira``
(story J6, epic e369).

Satisfies rebar's ``TicketTransport`` role plus the ``SupportsLinks``,
``SupportsComments``, and ``SupportsAbsenceProbe`` capability Protocols
(``_backend.py:80-282``). The underlying library client is a CONSTRUCTOR
PARAMETER (``JiraDataCenterTransport(client=..., project=...)``), so unit tests
inject a stateful fake with no network and no need for the ``[jira-datacenter]``
extra to be installed; ``build_client_from_settings`` constructs the REAL
``jira.JIRA`` for production use.

**The library's object model is unwrapped at this boundary.** Every method here
returns rebar's raw payload shapes (``{"key": …, "fields": {…}}``, comment maps,
issuelink lists) — never a ``jira.Issue``/``Comment`` instance. ``pycontribs``
objects expose ``.raw`` (the parsed JSON the REST API actually returned), which
``_unwrap`` reads; a plain dict (as an already-unwrapped test fake might return)
passes through unchanged. This is asserted by ``tests/_jira_shape_contract.py``,
the SAME shape contract that holds the Cloud transport honest (see the story's
execution-decision comment on ticket 9fd4-a94c-156e-4a56).

``jira`` is imported LAZILY — inside ``_jira_client_class`` only — so
``import rebar`` stays dependency-free; a missing ``[jira-datacenter]`` extra
raises an ``ImportError`` naming the install command.

Retry semantics mirror ``adapters/jira/acli_rest._rest_urlopen_with_retry``:
a connection-level fault (``requests`` ``ConnectionError``/``Timeout``, the
transport ``pycontribs`` itself raises for those) is retried up to 2 times with
2s/5s backoff; an HTTP-level error (``jira.exceptions.JIRAError`` — any 4xx/5xx
response) is attempted EXACTLY ONCE, since retrying a mutation risks duplicates.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar_reconciler._backend import (
    BackendAssigneeNotFoundError,
    BackendEnvError,
    BackendHTTPError,
)

# Retry policy + error translation live in ``retry.py`` (story S1): the cluster was
# relocated there to buy headroom under the LOCKED 800-line module-size cap. The
# names are RE-EXPORTED here so existing importers keep working unedited.
from rebar_reconciler.adapters.jira_datacenter.retry import (
    TlsVerificationError,
    _as_backend_http_error,
    _connection_retry_exceptions,
    _jira_http_error_types,
    _tls_verification_error,
    _with_connection_retry,
)

# Transition resolution + status routing live in ``transitions.py``: the cluster was
# relocated along the call-graph seam it already formed (``update_issue`` ->
# ``route_status_to_transition`` -> ``resolve_transition``) for the same reason
# ``retry.py`` was, and is re-exported here for the same reason.
from rebar_reconciler.adapters.jira_datacenter.transitions import (
    IllegalTransitionError,
    route_status_to_transition,
    transition_to_status,
)

# Re-export facade. The retry/error cluster moved to ``retry.py`` (story S1) to buy
# headroom under the LOCKED 800-line module-size cap; these names are imported here
# solely so ``transport.<name>`` keeps resolving for callers and for the existing
# suites — ``test_jira_dc_config_settings.py`` reaches for ``TlsVerificationError``
# and ``_with_connection_retry`` through this module and must pass UNEDITED.
# ``__all__`` records them as intentional re-exports, mirroring the same facade the
# sibling Cloud adapter uses in ``adapters/jira/acli.py``.
__all__ = [
    "AssigneeNotFoundError",
    "IllegalTransitionError",
    "JiraDataCenterTransport",
    "TlsVerificationError",
    "_as_backend_http_error",
    "_connection_retry_exceptions",
    "_jira_http_error_types",
    "_tls_verification_error",
    "_with_connection_retry",
    "build_client_from_settings",
    "route_status_to_transition",
    "transition_to_status",
]

logger = logging.getLogger(__name__)

_MISSING = object()


class AssigneeNotFoundError(BackendAssigneeNotFoundError, ValueError):
    """A requested DC assignee (Jira ``name``) resolves to no assignable user.

    Subclasses the vendor-neutral ``BackendAssigneeNotFoundError`` (``_backend.py``)
    so core apply-path ``except`` clauses catch it without importing anything
    DC-specific — mirroring ``adapters/jira/acli_subprocess.AssigneeNotFoundError``.
    """


def _jira_client_class() -> type[Any]:
    """Lazily import and return ``jira.JIRA`` (the ``pycontribs/jira`` client class).

    An indirection point (rather than a bare ``import jira`` at each call site) so
    tests can monkeypatch this function to swap in a fake class without installing
    the extra. A missing extra raises ``ImportError`` naming the install command —
    the one contract every "missing optional dependency" error in this codebase
    follows (see ``rebar.llm.runner._import_pydantic_ai``).
    """
    try:
        import jira as _jira_pkg
    except ImportError as exc:
        raise ImportError(
            "the Jira Data Center transport needs the 'jira-datacenter' extra "
            "(pycontribs/jira). Install it with: pip install 'nava-rebar[jira-datacenter]'"
        ) from exc
    return _jira_pkg.JIRA


def build_client_from_settings(settings: Any) -> Any:
    """Construct the real ``jira.JIRA`` client from resolved DC settings.

    Auth is Bearer PAT (``token_auth=settings.pat``, Jira 8.14+). TLS is NEVER
    relaxed here: ``pycontribs/jira`` reads certificate verification from its
    **options dict** (``verify`` is a key of ``JIRA.DEFAULT_OPTIONS``, default
    ``True``, consumed as ``self._options["verify"]`` — there is no bare
    ``verify=`` constructor kwarg). When ``settings.ca_bundle`` is set it is
    passed as the ``verify`` option value (a CA bundle PATH, for a self-signed/
    internal-CA DC deployment); when unset, ``verify`` is left OUT of the options
    dict entirely so the library's own default (``True``) applies — this
    function never sets ``verify=False`` under any input. ``settings.
    allow_insecure`` affects ONLY the URL-scheme validation that already ran
    inside ``ReconcilerConfig`` before this settings object existed
    (``_config_schema.py``); it has no bearing on certificate verification.
    """
    # FAIL CLOSED on a missing credential, HERE — the one point an anonymous client could
    # come into existence (bug cd78). An empty PAT was handed to `token_auth=`, which
    # constructs fine and then issues every request ANONYMOUSLY: Jira answers with "The
    # value 'X' does not exist for the field 'project'" (it hides projects you cannot
    # browse), so a forgotten export reads as a project error; and where anonymous CAN
    # browse there is no error at all, just a silently partial pass.
    # `Backend.assert_env_ready` already checked this but is only reached on the
    # bootstrap-band path, so dry-run and ordinary passes went anonymous.
    # NOT at settings resolution: that is reached from a PROPERTY
    # (`JiraDataCenterBackend.query_project`), and on Python <= 3.11 Protocol `isinstance`
    # evaluates properties via `hasattr`, so raising there breaks conformance checks on the
    # 3.11 CI leg while passing on 3.12+ (which uses `inspect.getattr_static`).
    if not (settings.pat or "").strip():
        raise BackendEnvError(
            "JIRA_PAT is not set. The Jira Data Center backend authenticates with a "
            "Personal Access Token read from the environment — it is environment-only and "
            "is never accepted from a config file, so the credential cannot be committed by "
            "accident. Export it before reconciling:\n"
            "    export JIRA_PAT=<your personal access token>\n"
            "Without it the reconciler would fall back to ANONYMOUS access, which typically "
            'surfaces as a misleading "project does not exist" error (Jira hides projects '
            "you cannot browse) or, on a permissive instance, as a silently empty pass."
        )

    jira_cls = _jira_client_class()
    options: dict[str, Any] = {}
    if settings.ca_bundle:
        options["verify"] = settings.ca_bundle
    return jira_cls(server=settings.url, token_auth=settings.pat, options=options or None)


def _unwrap(obj: Any) -> Any:
    """Unwrap a ``pycontribs`` library object (``Issue``/``Comment``/…) to rebar's
    raw payload dict via its ``.raw`` attribute — the parsed JSON the REST API
    actually returned. An object with no ``.raw`` (e.g. an already-plain dict)
    passes through unchanged. This is THE unwrapping boundary the whole story
    exists to enforce: nothing downstream of this function ever sees a
    ``jira.Issue`` (or any other library object)."""
    raw = getattr(obj, "raw", None)
    return raw if raw is not None else obj


def _call_logged(member: str, remote_id: Any, fn: Any, *, rate_limit_retry: bool = False) -> Any:
    """Run ``fn()`` through :func:`_with_connection_retry`, logging a WARNING that
    names the transport MEMBER and the REMOTE ID before any failure propagates.

    Seven of the twelve members added by story J9 are invoked from core call sites
    that swallow ``Exception`` at EVERY site (comments, links, parents, issue
    properties, assignee validation), and three more swallow it at some sites. A
    failure there produces no crash and no record — which is precisely how a DC
    deployment can "converge" while syncing nothing. This log is the only signal
    those paths emit, so it is written HERE, at the single choke point every
    member routes through, rather than per method (where it would be forgotten by
    the thirteenth member). The exception is re-raised untouched: this observes,
    it never handles. Follows ``adapters/jira/acli_subprocess.py``'s module-level
    ``logger = logging.getLogger(__name__)`` convention.

    ``rate_limit_retry`` is FORWARDED, defaulting to False. This forward is load-bearing
    rather than cosmetic: before story S2 this function called ``_with_connection_retry(fn)``
    with no keyword at all, so a flag threaded only as far as here would have been a no-op
    that still type-checked — every ``_paged_search`` read would have looked opted-in and
    retried nothing.
    """
    try:
        return _with_connection_retry(fn, rate_limit_retry=rate_limit_retry)
    except Exception as exc:
        logger.warning(
            "jira-datacenter transport: %s failed for remote id %r: %r", member, remote_id, exc
        )
        raise


def _user_attr(user: Any, key: str) -> Any:
    """Read ``key`` off a ``jira.resources.User`` (attribute) or an already-raw
    dict (item) — the two shapes ``search_users`` yields against a real client and
    against an injected fake respectively."""
    raw = _unwrap(user)
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(user, key, None)


class JiraDataCenterTransport:
    """The DC ``TicketTransport`` + ``SupportsLinks``/``SupportsComments``/
    ``SupportsAbsenceProbe`` capabilities, built on an injected ``jira.JIRA``-shaped
    client.

    ``resolved_statuses`` defaults to Cloud/DIG's ``{Resolved, Done, Cancelled}``
    (``settings.DEFAULT_RESOLVED_STATUSES``) so a transport built directly with a
    fake client (as the unit tests do) never needs a loaded config; production
    construction threads the configured set through from
    ``resolve_jira_datacenter_settings`` (``reconciler.resolved_statuses``).
    """

    def __init__(
        self,
        *,
        client: Any,
        project: str,
        resolved_statuses: frozenset[str] | None = None,
    ) -> None:
        self._client = client
        self.project = project
        if resolved_statuses is None:
            from rebar_reconciler.adapters.jira_datacenter.settings import (
                DEFAULT_RESOLVED_STATUSES,
            )

            resolved_statuses = DEFAULT_RESOLVED_STATUSES
        self._resolved_statuses = resolved_statuses

    # ------------------------------------------------------------------
    # TicketTransport
    # ------------------------------------------------------------------

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        fields = dict(ticket_data)
        fields.setdefault("project", {"key": self.project})
        issue = _with_connection_retry(lambda: self._client.create_issue(**fields))
        return _unwrap(issue)

    def get_issue(self, remote_id: str) -> dict[str, Any]:
        issue = _with_connection_retry(lambda: self._client.issue(remote_id))
        return _unwrap(issue)

    def update_issue(self, remote_id: str, **kwargs: Any) -> dict[str, Any]:
        assignee = kwargs.pop("assignee", _MISSING)
        if kwargs:
            issue = _with_connection_retry(lambda: self._client.issue(remote_id))
            _with_connection_retry(lambda: issue.update(fields=kwargs))
        if assignee is not _MISSING:
            self._assign(remote_id, assignee)
        return self.get_issue(remote_id)

    def _assign(self, remote_id: str, assignee: Any) -> None:
        """Assign ``remote_id`` to ``assignee`` (a DC username), raising
        ``AssigneeNotFoundError`` when the library/server reports the user as
        unresolvable rather than letting a bare HTTP error escape.

        The HTTP error is caught as the already-translated ``BackendHTTPError``
        (:func:`_with_connection_retry` converts it), so this behaves exactly as
        before while needing no vendor import of its own."""
        try:
            _with_connection_retry(lambda: self._client.assign_issue(remote_id, assignee))
        except BackendHTTPError as exc:
            raise AssigneeNotFoundError(
                f"assignee {assignee!r} could not be resolved to a DC user on {remote_id}: {exc}"
            ) from exc

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

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict[str, Any]]:
        results = _with_connection_retry(
            lambda: self._client.search_issues(jql, startAt=start_at, maxResults=max_results)
        )
        return [_unwrap(issue) for issue in results]

    # ------------------------------------------------------------------
    # SupportsLinks
    # ------------------------------------------------------------------

    def set_relationship(
        self, from_id: str, to_id: str, link_type: str = "Blocks"
    ) -> dict[str, Any]:
        _with_connection_retry(lambda: self._client.create_issue_link(link_type, from_id, to_id))
        return self.get_issue(from_id)

    def _paged_search(
        self,
        jql: str,
        *,
        fields: str | None = None,
        page_size: int = 100,
        rate_limit_retry: bool = False,
    ) -> list[dict[str, Any]]:
        """Every issue matching ``jql``, paged to exhaustion — the ONE pager the
        whole-project readers share.

        Advances by what the server ACTUALLY returned and stops only on an EMPTY page.
        Jira DC silently truncates ``maxResults`` above ``jira.search.views.default.max``,
        so a SHORT page is not proof of exhaustion: advancing by the REQUESTED size reads
        a truncated FIRST page as the final one (measured: 20 of 250 recovered).

        SHARED rather than repeated per method because that defect was fixed once, in
        ``get_parent_map`` alone, leaving the two siblings here plus a third in
        ``fetcher._iter_pages``. A structural test now fails the build if any caller takes
        the ``search_issues`` default again (bug 9263).
        """
        out: list[dict[str, Any]] = []
        start_at = 0
        while True:
            results = _call_logged(
                "_paged_search",
                jql,
                lambda offset=start_at: self._client.search_issues(
                    jql, startAt=offset, maxResults=page_size, fields=fields
                ),
                rate_limit_retry=rate_limit_retry,
            )
            batch = [_unwrap(issue) for issue in results]
            if not batch:
                break
            out.extend(batch)
            start_at += len(batch)
        return out

    def get_issuelinks_map(self, project_key: str) -> dict[str, Any]:
        issues = self._paged_search(f"project = {project_key}", rate_limit_retry=True)
        return {
            issue["key"]: list(issue.get("fields", {}).get("issuelinks") or []) for issue in issues
        }

    # ------------------------------------------------------------------
    # SupportsComments
    # ------------------------------------------------------------------

    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        comment = _with_connection_retry(lambda: self._client.add_comment(remote_id, body))
        return _unwrap(comment)

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        issues = self._paged_search(f"project = {project_key}", rate_limit_retry=True)
        out: dict[str, Any] = {}
        for issue in issues:
            key = issue["key"]
            # PATH B: this per-issue fetch does NOT route through `_paged_search` or
            # `_call_logged`, so it must opt in DIRECTLY. It is one request PER ISSUE —
            # the highest-volume read in a pass, and therefore the call most likely to
            # trip a token bucket. Threading the flag only through `_call_logged` would
            # have left exactly this one unprotected while every test passed.
            comments = _with_connection_retry(
                lambda k=key: self._client.comments(k), rate_limit_retry=True
            )
            out[key] = {"comments": [_unwrap(c) for c in comments]}
        return out

    # ------------------------------------------------------------------
    # SupportsAbsenceProbe
    # ------------------------------------------------------------------

    def probe_remote(self, remote_id: str) -> Any:
        """Probe ``remote_id`` and classify via the SHARED ``jira_family``
        classifier (bound to this transport's configured ``resolved_statuses`` —
        never Cloud/DIG's hardcoded names, since a self-hosted DC workflow can
        name its resolved states anything).

        The failing read is caught as the translated ``BackendHTTPError``, whose
        ``.code`` carries the same status the raw library error did — so the
        classification is unchanged."""
        from rebar_reconciler.adapters.jira_family import classify_probe_response

        try:
            issue = _with_connection_retry(lambda: self._client.issue(remote_id))
        except BackendHTTPError as exc:
            return classify_probe_response(
                remote_id, exc.code or 0, {}, resolved_statuses=self._resolved_statuses
            )
        return classify_probe_response(
            remote_id, 200, _unwrap(issue), resolved_statuses=self._resolved_statuses
        )

    # ------------------------------------------------------------------
    # The twelve members the core reaches for (story J9). Each is written
    # against DC REST **v2** via ``pycontribs/jira`` — never by copying Cloud's
    # v3 endpoint, and never by hand-rolled REST. Every one routes through
    # :func:`_call_logged` so a failure at a call site that swallows
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

    def get_comments(self, remote_id: str) -> list[dict[str, Any]]:
        """All comments on an issue, as raw payload dicts.

        ``get_comment_map`` already made this exact call (``client.comments``);
        what was missing was the SURFACE, not the capability. The library pages
        internally, so there is no Cloud-style ``--paginate`` caveat here.
        """
        comments = _call_logged("get_comments", remote_id, lambda: self._client.comments(remote_id))
        return [_unwrap(c) for c in comments]

    def get_issue_links(self, remote_id: str) -> list[dict[str, Any]]:
        """The issue's ``issuelinks`` in REST-nested shape — the shape
        ``JiraDataCenterBackend.map_remote_links`` already canonicalizes and the
        shape ``dispatch_one._index_existing_links`` indexes. REST v2 and v3 carry
        an identical ``issuelinks`` payload, so no translation is needed."""
        issue = _call_logged("get_issue_links", remote_id, lambda: self._client.issue(remote_id))
        raw = _unwrap(issue)
        fields = raw.get("fields") if isinstance(raw, dict) else None
        links = fields.get("issuelinks") if isinstance(fields, dict) else None
        return links if isinstance(links, list) else []

    def get_parent_map(self, project_key: str, jql: str | None = None) -> dict[str, str | None]:
        """``{issue_key → parent_key | None}`` for a project, via DC REST **v2**
        OFFSET pagination.

        Deliberately NOT a port of Cloud's ``get_parent_map``: that one POSTs to
        ``/rest/api/3/search/jql`` and pages with an opaque ``nextPageToken``
        cursor, and its own docstring records (live-proven, ticket 8b25) that the
        legacy endpoint is retired with HTTP 410 and that sending ``startAt`` is
        rejected with HTTP 400. Both the endpoint and the pagination model are
        Cloud-v3 only. DC serves ``/rest/api/2/search`` with ``startAt`` /
        ``maxResults``, which is exactly what ``jira.JIRA.search_issues`` drives —
        so the library's native offset paging IS the DC-correct mechanism.

        Degradation contract (mirrors Cloud, and what ``fetcher`` expects): a
        failure logs a WARNING and returns ``{}``, so the inbound pass falls back
        to its parentless path rather than aborting.
        """
        query = jql or f"project = {project_key}"
        out: dict[str, str | None] = {}
        try:
            # Routed through the SHARED pager (9263). It was correct here first, and
            # leaving it as a fourth hand-rolled loop would mean the helper exists while
            # the method that motivated it does not use it — the two would drift.
            for issue in self._paged_search(query, fields="parent", rate_limit_retry=True):
                if not isinstance(issue, dict):
                    continue
                key = issue.get("key")
                if not key:
                    continue
                fields = issue.get("fields")
                parent = fields.get("parent") if isinstance(fields, dict) else None
                out[key] = parent.get("key") if isinstance(parent, dict) else None
        except Exception as exc:  # noqa: BLE001 — degradation contract: a parent-map failure must not abort the inbound pass
            logger.warning(
                "jira-datacenter transport: get_parent_map degraded to {} for project %r: %r",
                project_key,
                exc,
            )
            return {}
        return out

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

    def delete_issue_link(self, link_id: str) -> dict[str, Any]:
        """Delete an issue link by id, absorbing 404/409 as idempotent success
        **inside the transport**.

        That absorption is NOT redundant with the caller. ``dispatch_one`` treats a
        concurrent-removal failure as success only when it sees
        ``subprocess.CalledProcessError`` — a Cloud/ACLI-specific type, as its own
        comment says ("delete_issue_link shells out via ACLI"). DC raises
        ``BackendHTTPError``, which does not match that clause, so a raced 404 would
        escape and unwind the pass. Owning idempotence here makes the DC path
        correct under a handler written for a different transport.
        """
        try:
            _call_logged(
                "delete_issue_link", link_id, lambda: self._client.delete_issue_link(link_id)
            )
        except BackendHTTPError as exc:
            if exc.code in (404, 409):
                return {"status": "already_absent", "id": link_id}
            raise
        return {"status": "deleted", "id": link_id}

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

    def _put_property(self, member: str, remote_id: str, property_key: str, value: Any) -> None:
        """PUT ``value`` to ``issue/{remote_id}/properties/{property_key}`` via the
        library's ``add_issue_property``.

        THE value-shape contract: the value is passed **verbatim**. Jira's
        issue-properties API stores whatever JSON is PUT as the property's value,
        and an earlier Cloud implementation wrapped it as ``{"value": …}`` — which
        stored the wrong shape and broke correlation WITHOUT ever raising (bug
        0b27). So "it did not error" is not evidence here; the key and the value
        must reach the endpoint intact.

        ``member`` is the PUBLIC method name so the WARNING names the member the
        core actually called, not this private helper.
        """
        _call_logged(
            member,
            remote_id,
            lambda: self._client.add_issue_property(remote_id, property_key, value),
        )

    def set_issue_property(self, remote_id: str, property_key: str, value: Any) -> None:
        """Set an issue property (``PUT issue/{key}/properties/{prop}``, value verbatim)."""
        self._put_property("set_issue_property", remote_id, property_key, value)

    def set_entity_property(self, remote_id: str, prop_name: str, value: Any) -> None:
        """Set an entity property — the same endpoint as :meth:`set_issue_property`
        (Cloud's is literally an alias of it, ``acli_rest.py:266``).

        This is the member whose absence crashed the first live DC writing pass.
        """
        self._put_property("set_entity_property", remote_id, prop_name, value)

    def set_reporter(self, remote_id: str, account_id: str) -> None:
        """Set the reporter to a DC **username**.

        DC has no ``accountId``, so the payload is ``{"reporter": {"name": …}}``
        rather than Cloud's ``{"accountId": …}``. The parameter keeps Cloud's name
        because the core's identity seam (``jira_account_id``) is already
        vendor-neutral: it hands back whatever ``external_id`` the family stored,
        which for DC identities is the username. No call-site change is needed.

        Idempotence depends on the INBOUND half: ``inbound_fields._identity_of``
        must carry DC's ``name`` into the canonical identity's ``account_id`` key,
        or the next snapshot reads ``None`` and the differ re-emits the reporter
        mutation on every pass.
        """
        issue = _call_logged("set_reporter", remote_id, lambda: self._client.issue(remote_id))
        _call_logged(
            "set_reporter",
            remote_id,
            lambda: issue.update(fields={"reporter": {"name": account_id}}),
        )

    def set_parent(self, remote_id: str, parent_key: str | None) -> None:
        """Set or clear a SUB-TASK's parent via ``fields.parent``.

        DC splits what Cloud unifies. A sub-task's parent genuinely lives in
        ``fields.parent`` and this method writes it (``{"parent": {"key": …}}``, or
        ``{"parent": None}`` to clear — the same call path for a SET and a CLEAR,
        which is what ``dispatch_one`` relies on). But EPIC membership on DC is not
        ``parent`` at all: it is the "Epic Link" **custom field**
        (``customfield_NNNNN``, whose id differs per instance), written through the
        Agile API — which DC serves under ``greenhopper`` while the library's
        ``AGILE_BASE_REST_PATH`` defaults to ``agile`` (``jira/resources.py:1518``),
        so ``add_issues_to_epic`` with default options targets a path DC does not
        expose.

        Rather than write ``fields.parent`` for a non-subtask and let DC silently
        no-op the epic link — the failure mode this story exists to eliminate — the
        epic case raises ``NotImplementedError`` NAMING the limitation. It is a
        loud, attributed decline, not an absent attribute and not a silent success.
        Lifting it is the story's recorded spike (epic-link field id +
        ``add_issues_to_epic`` against a real DC instance under
        ``agile_rest_path='greenhopper'``).
        """
        issue = _call_logged("set_parent", remote_id, lambda: self._client.issue(remote_id))
        raw = _unwrap(issue)
        fields = raw.get("fields") if isinstance(raw, dict) else None
        issue_type = fields.get("issuetype") if isinstance(fields, dict) else None
        is_subtask = bool(issue_type.get("subtask")) if isinstance(issue_type, dict) else False
        if not is_subtask:
            logger.warning(
                "jira-datacenter transport: set_parent declined for remote id %r "
                "(parent=%r): not a sub-task, so this is an Epic Link, not fields.parent",
                remote_id,
                parent_key,
            )
            raise NotImplementedError(
                f"set_parent is not supported for {remote_id!r} on Jira Data Center: only a "
                "SUB-TASK's parent lives in fields.parent. For any other issue type the parent "
                "is an EPIC LINK, held in an instance-specific 'Epic Link' custom field "
                "(customfield_NNNNN) and written through the Agile API, which Data Center "
                "serves under the 'greenhopper' REST path rather than the 'agile' path "
                "pycontribs/jira defaults to. Writing fields.parent here would silently "
                "no-op, so the operation is declined instead."
            )
        body: dict[str, Any] = {"parent": {"key": parent_key}} if parent_key else {"parent": None}
        _call_logged("set_parent", remote_id, lambda: issue.update(fields=body))

    def validate_assignee_exists(
        self,
        assignee: str,
        *,
        issue_key: str | None = None,
        project_key: str | None = None,
    ) -> str:
        """Resolve ``assignee`` to an assignable DC **username**, or raise
        :class:`AssigneeNotFoundError`.

        RETURN CONTRACT, which differs from Cloud's on purpose: the caller does
        ``acct = client.validate_assignee_exists(...)`` and then flows the return
        value ON as the resolved assignee identity. Cloud returns an ``accountId``;
        DC has none, so DC returns the ``name`` — consistent with ``NameIdentity``
        and with the ``external_id`` DC identities are minted under. Returning an
        accountId-shaped value, or ``True``, would corrupt the identity.

        ERROR CONTRACT: a definitive miss raises ``AssigneeNotFoundError``
        *specifically*, because ``outbound_assignee`` branches on
        ``type(exc).__name__ == "AssigneeNotFoundError"``. Any other type — and in
        particular a ``NotImplementedError`` — would downgrade EVERY DC assignee
        resolution to the non-authoritative string-match fallback, permanently and
        silently. That is why this member is implemented rather than declined.

        Matching is EXACT (username, then email, then display name) — DC's user
        search is substring/relevance-based, so returning its first hit would
        mis-assign a local handle that is not a DC user at all.
        """
        scope = issue_key or project_key or assignee
        users = _call_logged(
            "validate_assignee_exists",
            scope,
            lambda: self._client.search_users(user=assignee, maxResults=50),
        )
        candidates = list(users or [])
        for field in ("name", "emailAddress", "displayName"):
            for user in candidates:
                if _user_attr(user, field) == assignee:
                    return str(_user_attr(user, "name") or assignee)
        raise AssigneeNotFoundError(
            f"validate_assignee_exists: no assignable Data Center user exactly matches "
            f"{assignee!r} (scope {scope!r})"
        )
