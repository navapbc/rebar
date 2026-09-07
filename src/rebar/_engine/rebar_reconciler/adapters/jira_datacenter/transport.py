"""Compose Jira Data Center transport capabilities over an injected client.

The transport unwraps library objects into raw payloads at its boundary. The
optional ``jira`` dependency is imported lazily. Capability mixins share one
``_TransportBase`` initializer, pager, logger, and retry boundary.
``build_client_from_settings`` creates the production client while tests can
inject stateful fakes.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar_reconciler._backend import (
    BackendEnvError,
    BackendPaginationStallError,
)
from rebar_reconciler.adapters.jira_datacenter._base import _call_logged

# Private mixins compose into the public transport. Only the composed facade and
# exported helpers belong to ``__all__``.
from rebar_reconciler.adapters.jira_datacenter._hierarchy import _HierarchyMixin
from rebar_reconciler.adapters.jira_datacenter._issues import _IssuesMixin
from rebar_reconciler.adapters.jira_datacenter._links import _CommentsMixin, _LinksMixin

# Re-export ``AssigneeNotFoundError`` from the mixin module with the public facade.
from rebar_reconciler.adapters.jira_datacenter._people import (
    AssigneeNotFoundError,
    _PeopleMixin,
    _PropertiesMixin,
)

# Re-export retry and error types from their implementation module.
from rebar_reconciler.adapters.jira_datacenter.retry import (
    TlsVerificationError,
    _as_backend_http_error,
    _connection_retry_exceptions,
    _jira_http_error_types,
    _tls_verification_error,
    _with_connection_retry,
)

# Re-export transition resolution and routing from their implementation module.
from rebar_reconciler.adapters.jira_datacenter.transitions import (
    IllegalTransitionError,
    route_status_to_transition,
    transition_to_status,
)

# Declare the compatibility facade for the composed transport and helper exports.
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
        # Selected-backend boundary (RP-05 S4): the Jira Data Center client is being
        # constructed, so enforce the ``jira_datacenter`` semantic capability here; its
        # install guidance is single-sourced from the capability registry.
        from rebar._capabilities import install_hint

        raise ImportError(
            "the Jira Data Center transport needs the 'jira-datacenter' extra "
            f"(pycontribs/jira). Install it with: {install_hint('jira_datacenter')}"
        ) from exc
    return _jira_pkg.JIRA


def build_client_from_settings(settings: Any) -> Any:
    """Construct a ``jira.JIRA`` client with bearer PAT authentication.

    A configured CA bundle becomes the library's ``verify`` option. Otherwise
    the secure library default remains active. ``allow_insecure`` affects URL
    validation only and never disables certificate checks.
    """
    # Reject a blank PAT at client construction so no anonymous client can be
    # created. Settings resolution stays total for protocol property checks.
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
    """Combine required ticket, link, and comment operations.

    All capability mixins use one ``_TransportBase`` initializer over the
    injected client.
    """
