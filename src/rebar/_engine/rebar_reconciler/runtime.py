"""The reconciler operation-binding runtime (RP-04 S2, ticket e042).

One concern: compose ONE immutable ``ReconcilerRuntime`` per reconcile operation and
build EXACTLY the *selected* Jira backend from settings CAPTURED at composition time —
so the backend's read/query scope (``project`` / ``query_project``) and its connection
essentials are resolved ONCE here, not re-read from ambient ``env``/config on every
property access.

The composition delegates to the S1 seam
:func:`rebar._operation_config.compose_operation_snapshot`: that returns the frozen,
non-secret :class:`~rebar._operation_config.OperationSnapshot` (config values, their
source provenance, the selected repo root). From its ``values`` we derive a frozen
:class:`ReconcilerSettings` — the *scope* the built backend answers from — and pair it
with a provider-specific static-auth carrier that holds the send credential in a
stdlib-only string field excluded from observable dataclass state.

Secret hygiene (AC5). The static-auth carrier (Cloud ``JIRA_API_TOKEN`` / DC
``JIRA_PAT``) is stored in a plain string field marked ``repr=False, compare=False``,
so it can never enter a ``repr`` / ``str`` / equality / hash /
fingerprint of the runtime or its settings. The secret is revealed ONLY inside the
selected sending adapter, when it actually authenticates —
Cloud's ``AcliClient`` construction and DC's lazily-built ``jira.JIRA`` client.

Fail-closed (AC4). :meth:`ReconcilerRuntime.build_backend` (Cloud) and the built
backend's ``assert_env_ready`` raise a TYPED
:class:`~rebar_reconciler._backend.BackendEnvError` — never a bare ``AssertionError`` /
``KeyError`` — BEFORE any transport/network call when the Cloud read scope
(``jira.project``) is empty or a connection essential is missing/malformed. No
anonymous / cross-provider fallback.
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
    """Base static-auth carrier for an immutable send credential.

    The ``secret`` field is excluded from ``repr``/equality/hash (``repr=False,
    compare=False``), so the credential never leaks into a diagnostic, cache key, or
    fingerprint. It is revealed ONLY by the selected sending adapter at authentication
    time.
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
        """Construct EXACTLY the selected provider's backend from captured settings.

        Builds ONLY the selected backend (never the other). ``transport`` is a test
        seam: when supplied it is used as the backend's transport verbatim (no real
        transport is constructed and no network is touched); when ``None`` the real
        provider transport is built from the captured scope + revealed credential.
        """
        import rebar_reconciler.adapters  # noqa: F401  (side-effect: registers factories)

        name = self.settings.backend_name
        if name == _CLOUD_BACKEND:
            return self._build_cloud(transport)
        if name == _DC_BACKEND:
            return self._build_datacenter(transport)
        from rebar_reconciler._backend_registry import _REGISTRY, BackendRegistryError

        raise BackendRegistryError(
            f"unknown reconciler backend {name!r}; registered keys: {sorted(_REGISTRY)}"
        )

    def _build_cloud(self, transport: TicketTransport | None) -> Any:
        from rebar_reconciler._backend import assert_transport_conforms
        from rebar_reconciler.adapters.jira import acli
        from rebar_reconciler.adapters.jira.backend import JiraBackend

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
        from rebar_reconciler._backend import assert_transport_conforms
        from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend
        from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

        s = self.settings
        # Symmetry with ``_build_cloud`` (ticket 4698-d85c): assert the NON-SECRET half
        # of the DC scope (base url + project) at build, before any transport exists.
        # PAT presence and network validation stay LAZY in ``_LazyDataCenterClient`` /
        # ``build_client_from_settings``, so composing a DC backend still needs neither
        # the ``[agents]``/jira extra nor a ``JIRA_PAT``.
        assert_datacenter_nonsecret_scope_ready(s)
        if transport is not None:
            return JiraDataCenterBackend(transport=transport, scope=s)
        # Build the real ``jira.JIRA`` client LAZILY: composing the runtime (and reading
        # the captured read scope) must not require the ``[agents]``/jira extra or a live
        # ``JIRA_PAT`` — those are needed only when a network operation actually runs, at
        # which point the lazy client fails closed via ``build_client_from_settings``.
        client = _LazyDataCenterClient(self.auth, s)
        transport = JiraDataCenterTransport(client=client, project=s.project)
        assert_transport_conforms(transport, vendor=_DC_BACKEND)
        return JiraDataCenterBackend(transport=transport, client=client, scope=s)


class _LazyDataCenterClient:
    """A ``jira.JIRA`` stand-in that builds the real client on first attribute access.

    Lets ``build_backend`` construct the DC backend (and answer its captured read
    scope) without importing the optional jira extra or requiring ``JIRA_PAT`` up
    front — the real client, its TLS/auth, and the fail-closed missing-PAT guard in
    ``build_client_from_settings`` are all deferred to the first real network call.
    """

    def __init__(self, auth: StaticAuth, settings: ReconcilerSettings) -> None:
        self._auth = auth
        self._settings = settings
        self._real: Any | None = None

    def _resolve(self) -> Any:
        if self._real is None:
            from rebar_reconciler.adapters.jira_datacenter.settings import (
                JiraDataCenterSettings,
            )
            from rebar_reconciler.adapters.jira_datacenter.transport import (
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
        # Dunder / private probes (repr, pickle, copy, hasattr introspection, pytest's
        # attribute sniffing) must NOT trigger construction of the real client — that would
        # both defeat the lazy contract and prematurely raise the fail-closed missing-PAT
        # error on a mere introspection. Only forward genuine public API access.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)


def compose_reconciler_runtime(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    cli_overrides: dict | None = None,
) -> ReconcilerRuntime:
    """Compose the ONE immutable :class:`ReconcilerRuntime` for a reconcile operation.

    Delegates config composition to the S1 seam
    :func:`rebar._operation_config.compose_operation_snapshot` (defaults < user <
    project < env < cli precedence, repo-root selection, malformed-config fail-fast)
    and derives the reconciler binding from the resulting snapshot's ``values`` /
    ``sources`` / ``repo_root``.
    """
    from rebar import _operation_config

    snapshot = _operation_config.compose_operation_snapshot(
        cli_overrides=_normalize_overrides(cli_overrides), repo_root=repo_root
    )
    return _runtime_from_snapshot(snapshot)


def _normalize_overrides(cli_overrides: dict | None) -> dict | None:
    """Accept BOTH override spellings and return the nested
    ``{section: {key: value}}`` shape :func:`compose_operation_snapshot` consumes.

    A caller may pass the flat dotted form (``{"reconciler.backend": "jira-datacenter"}``)
    or the already-nested form (``{"reconciler": {"backend": "jira-datacenter"}}``). The
    dotted form is the natural spelling at a call site, but the config layer's
    ``coerce_sparse`` treats a dotted TOP-LEVEL key as an unknown SECTION and silently
    drops it — so without this normalization a dotted override would be ignored. Nested
    values are merged through unchanged.
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
    """Provider-specific scope + static-auth carrier.

    No provider applies an implicit create-time project default (AC2): the *write* and
    *read* (query) scope are BOTH the configured project verbatim, so an unset project
    stays empty and fails closed in :func:`assert_cloud_scope_ready` /
    :func:`assert_datacenter_scope_ready` before any transport is built. Only an
    operator who EXPLICITLY configures a project (including the literal ``"DIG"``) gets a
    non-empty scope.
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
    """Fail-closed Cloud readiness check on a CAPTURED scope — TYPED, no network.

    Raises :class:`~rebar_reconciler._backend.BackendEnvError` when a Cloud connection
    essential (``JIRA_URL`` / ``JIRA_USER`` / ``JIRA_API_TOKEN``) is missing, when
    ``JIRA_USER`` is not an email (Cloud's Basic-auth username IS the account email), or
    when the read scope (``jira.project``) is empty — before any transport is built.

    INTENDED TIGHTENING, not drift (ticket 4698-d85c). This check is DELIBERATELY
    stricter than the legacy ambient ``JiraBackend.assert_env_ready`` path, which only
    requires ``JIRA_URL``/``JIRA_USER``/``JIRA_API_TOKEN`` to be non-empty: the
    email-format check catches a username that Cloud Basic auth would silently reject
    (the Basic-auth username IS the Atlassian account email), and the non-empty-project
    checks catch a scope that would otherwise query EVERY project on the instance. Real
    Cloud deployments satisfy both, so the captured-scope path is the canonical
    fail-closed behavior; the weaker ambient check is the compatibility floor for
    backends built without a captured scope, not a parity target to relax to.
    """
    from rebar_reconciler._backend import BackendEnvError

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
    from rebar_reconciler._backend import BackendEnvError

    local, sep, domain = user.strip().partition("@")
    if not sep or not local or not domain:
        raise BackendEnvError(
            "invalid JIRA_USER: Jira Cloud authenticates with your Atlassian account "
            f"EMAIL as the Basic-auth username, but JIRA_USER={user.strip()!r} is not an "
            "email address. Set JIRA_USER to the email of the account that owns the token."
        )


def assert_datacenter_nonsecret_scope_ready(scope: ReconcilerSettings) -> None:
    """Fail-closed check of the NON-SECRET Data Center scope — TYPED, no network.

    The DC build-time analogue of :func:`assert_cloud_scope_ready` (ticket 4698-d85c):
    raises :class:`~rebar_reconciler._backend.BackendEnvError` when the base ``url``
    (``[tool.rebar.reconciler].base_url``) or the read/write project scope
    (``jira.project``) is empty — before any transport or client is built. Deliberately
    does NOT check ``JIRA_PAT``: the credential (and every network concern) stays lazy
    in ``_LazyDataCenterClient``, so composing/building a DC backend never requires the
    secret or the optional jira extra. The PAT is enforced by
    :func:`assert_datacenter_scope_ready` (the ``assert_env_ready`` path) and by
    ``build_client_from_settings`` at first real client use.
    """
    from rebar_reconciler._backend import BackendEnvError

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

    Raises :class:`~rebar_reconciler._backend.BackendEnvError` when a DC connection
    essential (the base ``url`` / the env-only ``JIRA_PAT``) is missing — before the
    real client is built.
    """
    from rebar_reconciler._backend import BackendEnvError

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
