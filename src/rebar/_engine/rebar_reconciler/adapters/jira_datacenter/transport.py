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

import sys
import time
from typing import Any

from rebar_reconciler._backend import BackendAssigneeNotFoundError

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


def _connection_retry_exceptions() -> tuple[type[BaseException], ...]:
    """The exception types worth retrying: ``requests``' ``ConnectionError`` /
    ``Timeout`` (the underlying transport ``pycontribs`` itself raises for a
    transient connectivity fault). ``requests`` ships as a transitive dependency
    of the ``[jira-datacenter]`` extra, so it is present whenever a REAL client
    is in play; a transport built with a fake client (the unit tests — no
    extra installed) never raises these, so an empty tuple here is harmless:
    ``except ()`` matches nothing and every call just runs straight through.
    """
    # Builtin TimeoutError is ALWAYS retryable, independent of requests: since
    # Python 3.10 ``socket.timeout`` is an alias of it, so a read-timeout from the
    # ssl/socket layer can surface as this rather than as a requests exception.
    # ``acli_rest._rest_urlopen_with_retry`` — the policy this module mirrors —
    # retries it explicitly for exactly that reason ("read-timeout from ssl/socket
    # layer"); omitting it here would leave the DC path failing a transient fault
    # the Cloud path already survives.
    try:
        import requests.exceptions as _req_exc
    except ImportError:
        return (TimeoutError,)
    return (_req_exc.ConnectionError, _req_exc.Timeout, TimeoutError)


class TlsVerificationError(ConnectionError):
    """A TLS certificate verification failure reaching the DC instance.

    Distinct from the transient connectivity faults :func:`_with_connection_retry`
    retries, because ``requests.exceptions.SSLError`` SUBCLASSES
    ``requests.exceptions.ConnectionError`` — so without this it is swallowed by the
    retry set and re-attempted three times with backoff. A certificate does not
    become valid on retry: that is seven wasted seconds and a guaranteed failure,
    ending in an opaque SSL error that never mentions the setting which fixes it.
    """


def _tls_verification_error(exc: BaseException) -> Exception | None:
    """Return an actionable :class:`TlsVerificationError` for a cert failure, else None."""

    try:
        from requests.exceptions import SSLError
    except ImportError:  # no extra installed → no requests → nothing to classify
        return None
    if not isinstance(exc, SSLError):
        return None
    return TlsVerificationError(
        f"TLS certificate verification failed for the Data Center instance: {exc}. "
        "This is NOT retried — a certificate does not become valid on a retry. If this "
        "deployment presents a certificate from an internal CA, set reconciler.ca_bundle "
        "to that CA bundle's PATH; certificate verification is never disabled, and "
        "reconciler.allow_insecure does not affect it (it governs the URL scheme only)."
    )


def _with_connection_retry(fn: Any) -> Any:
    """Run ``fn()`` with the transport's retry policy.

    Retries up to 2 times (3 total attempts), 2s then 5s backoff, on a
    connection-level fault (see :func:`_connection_retry_exceptions`).
    ``jira.exceptions.JIRAError`` (any HTTP 4xx/5xx response) is NOT one of
    those exception types, so it propagates on the FIRST attempt, unretried —
    mirroring ``acli_rest._rest_urlopen_with_retry``'s HTTP-vs-connection
    distinction exactly (retrying a mutation on an HTTP error risks
    duplicates).
    """
    retryable = _connection_retry_exceptions()
    backoffs = (2, 5)
    last_exc: BaseException | None = None
    for attempt in range(3):
        try:
            return fn()
        except retryable as exc:
            # Checked BEFORE the retry bookkeeping: SSLError is a ConnectionError
            # subclass, so it lands in `retryable` and would otherwise be re-attempted.
            tls_error = _tls_verification_error(exc)
            if tls_error is not None:
                raise tls_error from exc
            last_exc = exc
        if attempt < 2:
            delay = backoffs[attempt]
            print(
                f"[jira-dc-retry] attempt {attempt + 1} failed ({last_exc!r}); "
                f"retrying in {delay}s …",
                file=sys.stderr,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


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
        unresolvable rather than letting a bare ``JIRAError`` escape."""
        from jira.exceptions import JIRAError

        try:
            _with_connection_retry(lambda: self._client.assign_issue(remote_id, assignee))
        except JIRAError as exc:
            raise AssigneeNotFoundError(
                f"assignee {assignee!r} could not be resolved to a DC user on {remote_id}: {exc}"
            ) from exc

    def transition_issue_by_name(self, remote_id: str, target_status: str) -> None:
        transitions = _with_connection_retry(lambda: self._client.transitions(remote_id))
        match = next(
            (t for t in transitions if isinstance(t, dict) and t.get("name") == target_status),
            None,
        )
        if match is None:
            available = sorted(t.get("name", "") for t in transitions if isinstance(t, dict))
            raise ValueError(
                f"no transition named {target_status!r} is available for {remote_id} "
                f"(available: {available})"
            )
        _with_connection_retry(lambda: self._client.transition_issue(remote_id, match["id"]))

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

    def get_issuelinks_map(self, project_key: str) -> dict[str, Any]:
        issues = self.search_issues(f"project = {project_key}")
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
        issues = self.search_issues(f"project = {project_key}")
        out: dict[str, Any] = {}
        for issue in issues:
            key = issue["key"]
            comments = _with_connection_retry(lambda k=key: self._client.comments(k))
            out[key] = {"comments": [_unwrap(c) for c in comments]}
        return out

    # ------------------------------------------------------------------
    # SupportsAbsenceProbe
    # ------------------------------------------------------------------

    def probe_remote(self, remote_id: str) -> Any:
        """Probe ``remote_id`` and classify via the SHARED ``jira_family``
        classifier (bound to this transport's configured ``resolved_statuses`` —
        never Cloud/DIG's hardcoded names, since a self-hosted DC workflow can
        name its resolved states anything)."""
        from jira.exceptions import JIRAError

        from rebar_reconciler.adapters.jira_family import classify_probe_response

        try:
            issue = _with_connection_retry(lambda: self._client.issue(remote_id))
        except JIRAError as exc:
            status_code = getattr(exc, "status_code", None) or 0
            return classify_probe_response(
                remote_id, status_code, {}, resolved_statuses=self._resolved_statuses
            )
        return classify_probe_response(
            remote_id, 200, _unwrap(issue), resolved_statuses=self._resolved_statuses
        )
