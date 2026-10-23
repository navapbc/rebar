"""Capture one immutable backend scope per reconcile operation.

Composition selects exactly one provider from the operation snapshot. Its
read/write project and connection settings stay fixed for the pass. Static
credentials are excluded from repr, equality, and hashes, and revealed only
by the selected sending adapter. Missing scope or connection essentials fail
with ``BackendEnvError`` before transport or network use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._backend import TicketTransport

_CLOUD_BACKEND = "jira"
_DC_BACKEND = "jira-datacenter"
# Consumed only by _resolve_tracker_dir below, as the fallback when the captured config
# snapshot carries no tracker.dir. Every relocation (the env override is folded into
# tracker.dir by config composition; an absolute value relocates) is applied there.
# tickets-boundary-ok: the absent-key default INSIDE the reconciler's own resolver
_DEFAULT_TRACKER_DIR = ".tickets-tracker"
_DEFAULT_TRACKER_BRANCH = "tickets"
_DEFAULT_COMMENT_MAX_CHARS = 32767


@dataclass(frozen=True)
class StaticAuth:
    """Hold a send credential outside repr, equality, and hash state.

    Only the selected adapter should reveal it when authenticating.
    """

    secret: str = field(default="", repr=False, compare=False)

    def reveal(self) -> str:
        """Reveal the credential — call ONLY where the adapter authenticates."""
        return self.secret

    def present(self) -> bool:
        """Whether a non-empty credential was captured (a non-secret boolean)."""
        return bool(self.reveal().strip())


@dataclass(frozen=True)
class CloudStaticAuth(StaticAuth):
    """Jira Cloud static auth: the env-only ``JIRA_API_TOKEN`` (HTTP Basic)."""


@dataclass(frozen=True)
class DataCenterStaticAuth(StaticAuth):
    """Jira Data Center static auth: the env-only ``JIRA_PAT`` (bearer token)."""


@dataclass(frozen=True)
class ReconcilerSettings:
    """The immutable scope a reconcile operation is bound to — captured ONCE.

    Every field is non-secret. The selected backend answers ``project`` /
    ``query_project`` / ``assert_env_ready`` from THIS record rather than re-resolving
    ambient state, so the binding is stable for the whole operation.
    """

    repo_root: str
    backend_name: str
    tracker_dir: Path
    tracker_branch: str
    project: str
    query_project: str
    url: str = ""
    user: str = ""
    base_url: str = ""
    allow_insecure: bool = False
    ca_bundle: str = ""
    comment_max_chars: int = _DEFAULT_COMMENT_MAX_CHARS
    jira_cli_timeout: int = 0
    auth_present: bool = False


@dataclass(frozen=True)
class ReconcilerRuntime:
    """The composed, immutable reconcile operation binding.

    Pairs the captured :class:`ReconcilerSettings` scope with the selected provider's
    static-auth carrier and builds EXACTLY that provider's backend on demand.
    """

    settings: ReconcilerSettings
    auth: StaticAuth = field(default_factory=StaticAuth, repr=False, compare=False)

    def build_backend(self, *, transport: TicketTransport | None = None) -> Any:
        """Build only the selected backend from captured settings.

        An injected transport is used verbatim without constructing a real
        client or making a network call.
        """
        import rebar.reconciler.adapters  # noqa: F401  (side-effect: registers factories)

        name = self.settings.backend_name
        if name == _CLOUD_BACKEND:
            return self._build_cloud(transport)
        if name == _DC_BACKEND:
            return self._build_datacenter(transport)
        from rebar.reconciler._backend_registry import _REGISTRY, BackendRegistryError

        raise BackendRegistryError(
            f"unknown reconciler backend {name!r}; registered keys: {sorted(_REGISTRY)}"
        )

    def _build_cloud(self, transport: TicketTransport | None) -> Any:
        from rebar.reconciler._backend import assert_transport_conforms
        from rebar.reconciler.adapters.jira import acli
        from rebar.reconciler.adapters.jira.backend import JiraBackend

        s = self.settings
        assert_cloud_scope_ready(s)
        if transport is None:
            transport = acli.AcliClient(
                jira_url=s.url,
                user=s.user,
                api_token=self.auth.reveal(),
                jira_project=s.project,
                # Ticket 2048-d289: bind the compose-captured per-call subprocess
                # timeout so the operation's ACLI dispatches never re-resolve
                # reconciler.jira_cli_timeout ambiently (0 = unset -> ambient floor).
                call_timeout=s.jira_cli_timeout,
            )
            assert_transport_conforms(transport, vendor=_CLOUD_BACKEND)
        return JiraBackend(transport=transport, scope=s)

    def _build_datacenter(self, transport: TicketTransport | None) -> Any:
        from rebar.reconciler._backend import assert_transport_conforms
        from rebar.reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend
        from rebar.reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

        s = self.settings
        # Check DC URL and project before transport; PAT and network validation
        # stay lazy so composition needs neither the optional extra nor a PAT.
        assert_datacenter_nonsecret_scope_ready(s)
        if transport is not None:
            return JiraDataCenterBackend(transport=transport, scope=s)
        # Construct the real DC client on first network use, when PAT readiness
        # is enforced.
        client = _LazyDataCenterClient(self.auth, s)
        transport = JiraDataCenterTransport(client=client, project=s.project)
        assert_transport_conforms(transport, vendor=_DC_BACKEND)
        return JiraDataCenterBackend(transport=transport, client=client, scope=s)


class _LazyDataCenterClient:
    """Delay optional Jira client import, TLS, and PAT checks until public use."""

    def __init__(self, auth: StaticAuth, settings: ReconcilerSettings) -> None:
        self._auth = auth
        self._settings = settings
        self._real: Any | None = None

    def _resolve(self) -> Any:
        if self._real is None:
            from rebar.reconciler.adapters.jira_datacenter.settings import (
                JiraDataCenterSettings,
            )
            from rebar.reconciler.adapters.jira_datacenter.transport import (
                build_client_from_settings,
            )

            s = self._settings
            self._real = build_client_from_settings(
                JiraDataCenterSettings(
                    url=s.base_url,
                    project=s.project,
                    allow_insecure=s.allow_insecure,
                    ca_bundle=s.ca_bundle,
                    pat=self._auth.reveal(),
                )
            )
        return self._real

    def __getattr__(self, name: str) -> Any:
        # Dunder probes must not construct the client or trigger missing-PAT errors.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)


def compose_reconciler_runtime(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    cli_overrides: dict | None = None,
) -> ReconcilerRuntime:
    """Bind one runtime to the composed operation snapshot and its config precedence."""
    from rebar import _operation_config

    snapshot = _operation_config.compose_operation_snapshot(
        cli_overrides=_normalize_overrides(cli_overrides), repo_root=repo_root
    )
    return _runtime_from_snapshot(snapshot)


def _normalize_overrides(cli_overrides: dict | None) -> dict | None:
    """Nest dotted overrides before config coercion so they are not discarded.

    Already nested values pass through unchanged.
    """
    if not cli_overrides:
        return cli_overrides
    nested: dict[str, Any] = {}
    for key, value in cli_overrides.items():
        if "." in key:
            sect, subkey = key.split(".", 1)
            nested.setdefault(sect, {})[subkey] = value
        elif isinstance(value, dict):
            nested.setdefault(key, {}).update(value)
        else:
            # A bare top-level scalar override: pass it through unchanged rather than
            # silently discarding its value, so the config layer's coerce_sparse sees it
            # (and rejects/handles it) instead of the value vanishing here.
            nested[key] = value
    return nested


def _runtime_from_snapshot(snapshot: Any) -> ReconcilerRuntime:
    values = snapshot.values
    root = str(snapshot.repo_root)
    recon = _section(values, "reconciler")
    jira = _section(values, "jira")
    tracker = _section(values, "tracker")

    backend_name = str(recon.get("backend") or _CLOUD_BACKEND)
    raw_project = str(jira.get("project") or "")
    auth, project, query_project = _resolve_provider_scope(backend_name, raw_project)

    settings = ReconcilerSettings(
        repo_root=root,
        backend_name=backend_name,
        tracker_dir=_resolve_tracker_dir(tracker, root),
        tracker_branch=str(tracker.get("branch") or _DEFAULT_TRACKER_BRANCH),
        project=project,
        query_project=query_project,
        url=str(jira.get("url") or ""),
        user=str(jira.get("user") or ""),
        base_url=str(recon.get("base_url") or ""),
        allow_insecure=bool(recon.get("allow_insecure", False)),
        ca_bundle=str(recon.get("ca_bundle") or ""),
        comment_max_chars=int(recon.get("comment_max_chars", _DEFAULT_COMMENT_MAX_CHARS)),
        jira_cli_timeout=int(recon.get("jira_cli_timeout", 0)),
        auth_present=auth.present(),
    )
    return ReconcilerRuntime(settings=settings, auth=auth)


def _resolve_provider_scope(backend_name: str, raw_project: str) -> tuple[StaticAuth, str, str]:
    """Capture provider auth and use the explicit project for reads and writes.

    An unset project stays empty for the fail-closed readiness checks.
    """
    if backend_name == _CLOUD_BACKEND:
        auth: StaticAuth = CloudStaticAuth(os.environ.get("JIRA_API_TOKEN", ""))
        return auth, raw_project, raw_project
    if backend_name == _DC_BACKEND:
        dc_auth = DataCenterStaticAuth(os.environ.get("JIRA_PAT", ""))
        return dc_auth, raw_project, raw_project
    return StaticAuth(), raw_project, raw_project


def _section(values: Any, name: str) -> Any:
    section = values.get(name)
    return {} if section is None else section


def _resolve_tracker_dir(tracker: Any, root: str) -> Path:
    """Resolve ``tracker.dir`` against the captured repo root (mirrors
    :func:`rebar.config.tracker_dir`): an absolute value relocates the store, a
    relative value is a dir name under the repo root."""
    name = str(tracker.get("dir") or _DEFAULT_TRACKER_DIR)
    path = Path(name)
    return path if path.is_absolute() else Path(root) / name


def assert_cloud_scope_ready(scope: ReconcilerSettings) -> None:
    """Reject incomplete captured Cloud scope with ``BackendEnvError`` before I/O.

    Require URL, token, account email, and explicit read/write project. This is
    stricter than the legacy ambient backend check to prevent broad queries.
    """
    from rebar.reconciler._backend import BackendEnvError

    missing = [
        name
        for name, value in (("JIRA_URL", scope.url), ("JIRA_USER", scope.user))
        if not (value or "").strip()
    ]
    if not scope.auth_present:
        missing.append("JIRA_API_TOKEN")
    if missing:
        raise BackendEnvError(
            f"missing Jira Cloud configuration: {', '.join(missing)}. The Jira Cloud "
            "backend authenticates with HTTP Basic auth using your Atlassian account "
            "email (JIRA_USER) and an API token (JIRA_API_TOKEN) against JIRA_URL."
        )
    _assert_cloud_user_is_email(scope.user)
    if not scope.query_project.strip():
        raise BackendEnvError(
            "empty Jira Cloud read scope: the configured project (jira.project / "
            "JIRA_PROJECT) is unset, so the inbound query would target every project. "
            "Set jira.project (or JIRA_PROJECT) to the project the reconciler owns."
        )
    if not scope.project.strip():
        raise BackendEnvError(
            "empty Jira Cloud write scope: the configured project (jira.project / "
            "JIRA_PROJECT) is unset, so a create/write would have no target project. "
            "Set jira.project (or JIRA_PROJECT) to the project the reconciler owns."
        )


def _assert_cloud_user_is_email(user: str) -> None:
    from rebar.reconciler._backend import BackendEnvError

    local, sep, domain = user.strip().partition("@")
    if not sep or not local or not domain:
        raise BackendEnvError(
            "invalid JIRA_USER: Jira Cloud authenticates with your Atlassian account "
            f"EMAIL as the Basic-auth username, but JIRA_USER={user.strip()!r} is not an "
            "email address. Set JIRA_USER to the email of the account that owns the token."
        )


def assert_datacenter_nonsecret_scope_ready(scope: ReconcilerSettings) -> None:
    """Check DC URL and read/write project before transport construction.

    PAT and optional client requirements stay lazy until first real use; a
    missing non-secret scope raises ``BackendEnvError``.
    """
    from rebar.reconciler._backend import BackendEnvError

    if not (scope.base_url or "").strip():
        raise BackendEnvError(
            "missing Jira Data Center configuration: url "
            "(set url via [tool.rebar.reconciler].base_url)."
        )
    if not scope.query_project.strip():
        raise BackendEnvError(
            "empty Jira Data Center read scope: the configured project (jira.project / "
            "JIRA_PROJECT) is unset, so the inbound query would target every project. "
            "Set jira.project (or JIRA_PROJECT) to the project the reconciler owns."
        )
    if not scope.project.strip():
        raise BackendEnvError(
            "empty Jira Data Center write scope: the configured project (jira.project / "
            "JIRA_PROJECT) is unset, so a create/write would have no target project. "
            "Set jira.project (or JIRA_PROJECT) to the project the reconciler owns."
        )


def assert_datacenter_scope_ready(scope: ReconcilerSettings) -> None:
    """Fail-closed Data Center readiness check on a CAPTURED scope — TYPED, no network.

    Raises :class:`~rebar.reconciler._backend.BackendEnvError` when a DC connection
    essential (the base ``url`` / the env-only ``JIRA_PAT``) is missing — before the
    real client is built.
    """
    from rebar.reconciler._backend import BackendEnvError

    missing = [
        name
        for name, present in (
            ("url", bool((scope.base_url or "").strip())),
            ("JIRA_PAT", scope.auth_present),
        )
        if not present
    ]
    if missing:
        raise BackendEnvError(
            f"missing Jira Data Center configuration: {', '.join(missing)} "
            "(set url via [tool.rebar.reconciler].base_url; JIRA_PAT is env-only)."
        )
