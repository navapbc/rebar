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

**Organising principle (ticket 465d): one module per capability, aligned to the
Protocols in ``_backend.py``.** ``TicketTransport`` -> ``_issues.py``,
``SupportsLinks``/``SupportsComments`` -> ``_links.py`` (co-located: each is
too small alone to clear the module-size policy's 100-LOC floor),
``SupportsAbsenceProbe`` -> ``probe_remote`` below (small enough to live on
the composition root), plus the ungated "twelve members" clusters
``_hierarchy.py`` (parent/Epic Link, ticket 9bb9's shared
``_resolve_epic_link_field_id``) and ``_people.py`` (identity + properties).
``_base.py`` holds the shared substrate (construction, the unwrap boundary,
the logged-retry choke point, the shared pager) every mixin inherits;
``transport.py`` (this module) is the thin composition root plus construction
from settings. A NEW transport operation therefore has a determined home:
decide which capability it belongs to (or whether it is genuinely a new one,
meriting a new ``_*.py`` mixin, or growing an existing one past its
co-location reason) — it does not default back to this file.
See ``retry.py`` (rate-limit + error translation) and ``transitions.py``
(transition resolution + status routing), extracted earlier along the same
call-graph-seam discipline and unchanged by this split.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar_reconciler._backend import (
    BackendEnvError,
    BackendHTTPError,
    BackendPaginationStallError,
)
from rebar_reconciler.adapters.jira_datacenter._base import _call_logged, _unwrap

# The capability mixins (ticket 465d): one module per Protocol in ``_backend.py``
# (``_links.py`` covers the two smallest, ``SupportsLinks`` + ``SupportsComments``,
# co-located per its own module docstring), composed below into the single
# ``JiraDataCenterTransport`` class. Their names are NOT part of the public
# re-export facade (``__all__``) — only the composed class and the free
# functions/errors below are.
from rebar_reconciler.adapters.jira_datacenter._hierarchy import _HierarchyMixin
from rebar_reconciler.adapters.jira_datacenter._issues import _IssuesMixin
from rebar_reconciler.adapters.jira_datacenter._links import _CommentsMixin, _LinksMixin

# ``AssigneeNotFoundError`` moved to ``_people.py`` WITH the members that raise it
# (``_assign``, ``validate_assignee_exists``); re-exported here for the same reason
# the retry/transition clusters are. ``_PropertiesMixin`` is co-located in the same
# module per its own module docstring.
from rebar_reconciler.adapters.jira_datacenter._people import (
    AssigneeNotFoundError,
    _PeopleMixin,
    _PropertiesMixin,
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
from rebar_reconciler.adapters.jira_family import classify_probe_response

# Re-export facade. The retry/error cluster moved to ``retry.py`` (story S1) to buy
# headroom under the LOCKED 800-line module-size cap; these names are imported here
# solely so ``transport.<name>`` keeps resolving for callers and for the existing
# suites — ``test_jira_dc_config_settings.py`` reaches for ``TlsVerificationError``
# and ``_with_connection_retry`` through this module and must pass UNEDITED.
# ``__all__`` records them as intentional re-exports, mirroring the same facade the
# sibling Cloud adapter uses in ``adapters/jira/acli.py``.
__all__ = [
    "AssigneeNotFoundError",
    # Re-exported (not defined here): the DC pager raises it, and DC readers must be able
    # to NAME it to re-raise past their fail-open handlers (ticket 18a4).
    "BackendPaginationStallError",
    "IllegalTransitionError",
    "JiraDataCenterTransport",
    "TlsVerificationError",
    "_as_backend_http_error",
    "_call_logged",
    "_connection_retry_exceptions",
    "_jira_http_error_types",
    "_tls_verification_error",
    "_with_connection_retry",
    "build_client_from_settings",
    "route_status_to_transition",
    "transition_to_status",
]

logger = logging.getLogger(__name__)


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


class JiraDataCenterTransport(
    _IssuesMixin,
    _HierarchyMixin,
    _LinksMixin,
    _CommentsMixin,
    _PeopleMixin,
    _PropertiesMixin,
):
    """The DC ``TicketTransport`` + ``SupportsLinks``/``SupportsComments``/
    ``SupportsAbsenceProbe`` capabilities, composed from one mixin per
    capability (ticket 465d) on an injected ``jira.JIRA``-shaped client.

    ``__init__`` and the attributes every mixin depends on
    (``self._client`` / ``self.project`` / ``self._epic_link_field_id`` /
    ``self._resolved_statuses``) live on ``_base._TransportBase``, which every
    mixin above inherits — so construction happens exactly once regardless of
    how many capability mixins are composed.
    """

    def probe_remote(self, remote_id: str) -> Any:
        """Probe ``remote_id`` and classify via the SHARED ``jira_family``
        classifier (bound to this transport's configured ``resolved_statuses`` —
        never Cloud/DIG's hardcoded names, since a self-hosted DC workflow can
        name its resolved states anything).

        The failing read is caught as the translated ``BackendHTTPError``, whose
        ``.code`` carries the same status the raw library error did — so the
        classification is unchanged."""
        try:
            issue = _with_connection_retry(lambda: self._client.issue(remote_id))
        except BackendHTTPError as exc:
            return classify_probe_response(
                remote_id, exc.code or 0, {}, resolved_statuses=self._resolved_statuses
            )
        return classify_probe_response(
            remote_id, 200, _unwrap(issue), resolved_statuses=self._resolved_statuses
        )
