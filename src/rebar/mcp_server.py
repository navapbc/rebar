"""rebar MCP server (FastMCP).

Exposes the ticket system as MCP tools, built on the rebar Python library.
Reads (``show``/``list``) run in-process via rebar._reads (no subprocess);
``reconcile`` defaults to a non-mutating dry-run.

Safety:
  * ``reconcile`` defaults to ``dry-run``; ``live`` additionally requires
    REBAR_MCP_ALLOW_JIRA_SYNC=1.
  * Write tools (create/transition/edit/link/unlink/tag/untag/archive/comment)
    are gated by REBAR_MCP_READONLY: set it to 1 to expose a read-only server.

The ``mcp`` dependency is an optional extra and is imported lazily.

Structure: ``build_server`` is a thin assembler — it builds the FastMCP server,
packs the shared handles + gate helpers into a ``ctx`` namespace, and calls the
three per-cluster registrars (``register_read_tools`` / ``register_llm_tools`` /
``register_write_tools`` in ``_mcp_reads`` / ``_mcp_llm`` / ``_mcp_writes``). The
gate helpers, the workflow-payload budget cap, ``_dump``, the ``MODE_CAPS`` table,
and the output models (re-exported from ``_mcp_models`` for back-compat) live here.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from types import SimpleNamespace

import rebar

# Output models live in the leaf module rebar._mcp_models (imported only by pydantic)
# so the per-cluster registrars can share them WITHOUT importing this module (which
# would form an import cycle). Re-exported here for back-compat: existing callers
# import e.g. ``rebar.mcp_server.NextBatchOut`` / ``ValidateReportOut`` directly.
from rebar._mcp_llm import register_llm_tools
from rebar._mcp_models import (
    BridgeAccessCheckOut,
    BridgeControlOut,
    BridgeFsckOut,
    BridgeRunOut,
    BridgeStatusOut,
    ClaimResultOut,
    ClarityResultOut,
    CreateResultOut,
    DepsGraphOut,
    FileImpactItemOut,
    GateResultOut,
    GroundingBackendOut,
    GroundingInfoOut,
    NextBatchOut,
    SignResultOut,
    TicketStateOut,
    ValidateReportOut,
    VerifyCommandItemOut,
    VerifySignatureResultOut,
    WorkflowRunOut,
)
from rebar._mcp_reads import register_read_tools
from rebar._mcp_writes import register_write_tools

logger = logging.getLogger(__name__)

__all__ = [
    "MCP_ENV_VARS",
    "BridgeAccessCheckOut",
    "BridgeControlOut",
    "BridgeFsckOut",
    "BridgeRunOut",
    "BridgeStatusOut",
    "ClaimResultOut",
    "ClarityResultOut",
    "CreateResultOut",
    "DepsGraphOut",
    "FileImpactItemOut",
    "GateResultOut",
    "GroundingBackendOut",
    "GroundingInfoOut",
    "McpStartupError",
    "NextBatchOut",
    "SignResultOut",
    "TicketStateOut",
    "ValidateReportOut",
    "VerifyCommandItemOut",
    "VerifySignatureResultOut",
    "WorkflowRunOut",
    "build_server",
    "main",
]


class McpStartupError(RuntimeError):
    """Raised when the MCP server cannot start under the requested configuration —
    a fail-closed startup gate (e.g. an unauthenticated HTTP transport without the
    acknowledgement, or a non-loopback bind missing its allowlists / TLS ack)."""


# ── Canonical MCP environment-variable contract ──────────────────────────────
# The SINGLE SOURCE OF TRUTH for the env vars the MCP server honors. The published
# manifest (server.json) MUST advertise exactly this set — a CI drift-guard
# (scripts/check_server_manifest.py, wired into .github/workflows/test.yml) diffs
# server.json against this list and fails the build on divergence, so the manifest
# can never silently drift from the real gates again. The ``--help`` text below is
# also derived from this list, so the three stay in lockstep.
#
# Each entry: name, a one-line description, and whether it is a deprecated alias.
# The active gates are read in mcp_server.build_server / _mcp_reads / _mcp_llm /
# _mcp_writes / config.py. (The REBAR_MCP_ALLOW_RECONCILE_LIVE alias of
# REBAR_MCP_ALLOW_JIRA_SYNC was removed pre-1.0 — DE7.)
MCP_ENV_VARS: tuple[dict, ...] = (
    {
        "name": "REBAR_ROOT",
        "description": (
            "Path to the repo root that holds the .tickets-tracker store "
            "(defaults to the git toplevel of the working dir)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_READONLY",
        "description": "Set to 1 to expose only the read tools (no write/mutation tools).",
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_ALLOW_LLM",
        "description": (
            "Set to 1 to enable the billable LLM tools (review_code / scan_spec / "
            "verify_completion / review_plan); off by default."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_ALLOW_JIRA_SYNC",
        "description": (
            "Set to 1 to allow the live (mutating) Jira reconcile mode; otherwise "
            "reconcile is dry-run only."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_TRANSPORT",
        "description": (
            "Transport for the MCP server: 'stdio' (default) or 'http' (the optional "
            "Streamable-HTTP transport)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_HTTP_HOST",
        "description": "Bind host for the Streamable-HTTP transport (default 127.0.0.1).",
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_HTTP_PORT",
        "description": "Bind port for the Streamable-HTTP transport (1-65535; default 8000).",
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_HTTP_PATH",
        "description": "URL path the Streamable-HTTP transport serves on (default /mcp).",
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_HTTP_ALLOWED_HOSTS",
        "description": (
            "Comma-separated allowlist of exact host:port values accepted by the "
            "Streamable-HTTP DNS-rebinding protection; empty defaults to loopback."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_HTTP_ALLOWED_ORIGINS",
        "description": (
            "Comma-separated allowlist of exact Origin values accepted by the "
            "Streamable-HTTP DNS-rebinding protection; empty defaults to loopback."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_HTTP_TLS_AT_EDGE",
        "description": (
            "Set to 1 to acknowledge TLS is terminated at the edge; required to bind "
            "the Streamable-HTTP transport to a non-loopback host."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_ALLOW_UNAUTHENTICATED_HTTP",
        "description": (
            "Set to 1 to acknowledge running the Streamable-HTTP transport without a "
            "token verifier; required to boot the HTTP transport while auth is off."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_ENABLED",
        "description": (
            "Set to 1 to enable MCP authentication (the composite token verifier + "
            "Resource-Server wiring); off by default."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_STRATEGIES",
        "description": (
            "Comma-separated, ordered list of token-verifier strategies to compose "
            "(closed set: static, jwt, introspection, proxy, custom)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_ISSUER_URL",
        "description": (
            "OAuth authorization-server issuer URL advertised in the Protected-Resource "
            "Metadata (RFC 9728) when auth is enabled."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_RESOURCE_SERVER_URL",
        "description": (
            "The single resource identifier (RFC 8707 audience) for this server; the "
            "composite verifier re-checks every accepted token against it."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_REQUIRED_SCOPES",
        "description": (
            "Comma-separated scopes a caller must hold; the SDK returns 403 "
            "insufficient_scope when a principal lacks one."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_STATIC_TOKENS_FILE",
        "description": (
            "Path to the JSON secrets file for the static-bearer verifier (stores only "
            "SHA-256 digests of the accepted tokens)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_JWKS_URI",
        "description": (
            "HTTPS JWKS endpoint the `jwt` verifier fetches signing keys from (an OIDC "
            "provider's .well-known/jwks.json)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_ISSUER",
        "description": (
            "Expected `iss` claim for the `jwt` verifier; falls back to "
            "REBAR_MCP_AUTH_ISSUER_URL when unset."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_ALGORITHMS",
        "description": (
            "Comma-separated PINNED, asymmetric-only JWS algorithms for the `jwt` verifier "
            "(default RS256,ES256); a symmetric algorithm on a JWKS source is refused."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_LEEWAY",
        "description": (
            "Clock-skew leeway in seconds applied to exp/nbf validation by the `jwt` "
            "verifier (default 60)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_JWKS_REFETCH_COOLDOWN",
        "description": (
            "Minimum seconds between JWKS refetches triggered by an unknown key id "
            "(the concurrency-safe flood guard; default 30)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_JWKS_TIMEOUT",
        "description": (
            "HTTP timeout in seconds for the `jwt` verifier's JWKS fetch (default 10)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_EXPECTED_TYP",
        "description": (
            "When set, the `jwt` verifier requires the JWT header `typ` to equal this "
            "(e.g. at+JWT per RFC 9068); unset skips the check."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_ALLOW_PRIVATE_JWKS_HOST",
        "description": (
            "Set to 1 to permit a private/link-local/loopback JWKS host (SSRF guard is "
            "on by default); off by default."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_INTROSPECTION_ENDPOINT",
        "description": (
            "The `introspection` verifier's RFC 7662 endpoint URL (must be https://); the "
            "opaque token is POSTed here on every request (no caching)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_INTROSPECTION_CLIENT_ID",
        "description": (
            "The client id the `introspection` verifier presents to the Authorization "
            "Server via HTTP Basic (client_secret_basic)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_INTROSPECTION_CLIENT_SECRET_ENV",
        "description": (
            "The NAME of the env var holding the introspection client secret (never the "
            "secret itself); must be present + non-empty at startup or the server refuses "
            "to start (fail-closed)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_INTROSPECTION_ALLOW_PRIVATE_HOST",
        "description": (
            "Set to 1 to permit a private/link-local/loopback introspection endpoint host "
            "(SSRF guard is on by default); off by default."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_INTROSPECTION_ALLOW_MISSING_AUD",
        "description": (
            "Set to 1 to accept an active introspection response that OMITS `aud` (many "
            "AS do); off by default (fail-closed reject)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_PROXY_SECRET_ENV",
        "description": (
            "The NAME of the env var holding the trusted-proxy shared secret (never the "
            "secret itself); must be present + non-empty at startup or the `proxy` verifier "
            "refuses to start (fail-closed)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_PROXY_SECRET_HEADER",
        "description": (
            "The header the fronting proxy sends its shared secret on; the identity is "
            "trusted only when this matches (constant-time; default x-proxy-auth)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_PROXY_IDENTITY_HEADER",
        "description": (
            "The header carrying the proxy-authenticated principal identity, trusted only "
            "when the secret header validates (default x-forwarded-user)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_PROXY_SCOPES",
        "description": (
            "Comma-separated fixed scope set granted to proxy-authenticated principals; "
            "empty by default (the principal holds no scopes)."
        ),
        "deprecated": False,
    },
    {
        "name": "REBAR_MCP_AUTH_CUSTOM_IMPORT",
        "description": (
            "The `custom` strategy's `module:factory` import string, resolving to a factory "
            "that returns a TokenVerifier; a TRUSTED operator config value that loads and "
            "executes the operator-configured code at startup (fail-closed on any load error)."
        ),
        "deprecated": False,
    },
)


# The reconcile tool gates modes by the engine's canonical MODE_CAPS table, which
# lives in the bundled engine at rebar_reconciler/mode.py. We load it ONCE here by
# FILE PATH (not `from rebar_reconciler.mode import ...`) and bind the names as
# module globals. Loading by path is deliberate: the dotted import is unreliable
# because the top-level name `rebar_reconciler` is shadowed in sys.modules in some
# contexts (notably the unit-test package of the same name under pytest), which
# makes `rebar_reconciler.mode` raise ModuleNotFoundError. mode.py is stdlib-only
# and self-contained, so a standalone path-load is safe.
def _load_engine_mode():
    from rebar._engine import engine_dir

    mode_path = engine_dir() / "rebar_reconciler" / "mode.py"
    spec = importlib.util.spec_from_file_location("rebar._engine_mode", mode_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MODE_CAPS, mod.Mode


MODE_CAPS, Mode = _load_engine_mode()


def _mcp_gate(attr: str) -> bool:
    """Resolve a typed ``mcp.<attr>`` boolean gate through the single-source config
    (env ``REBAR_MCP_<ATTR>`` wins over a ``[tool.rebar.mcp]`` config file; the
    ``_as_bool`` coercion accepts 1/true/yes/on, any case, whitespace-tolerant). On a
    MALFORMED config the resolver raises ``ConfigError`` (operator ruling 39f8-ae7c) —
    the structured-error guard delivers it to the client as a ``config_unreadable``
    envelope, so the value reported by ``rebar config`` is exactly what's enforced
    here and a fault never reads as a policy choice."""
    return rebar.config.mcp_gate(attr)


def _readonly() -> bool:
    # ERRORS on a malformed config (operator ruling 39f8-ae7c) — consistent with the
    # verify gates; a broken config surfaces the fault instead of silently locking
    # read-only. Routed through the ONE shared resolver in rebar.config so the LLM
    # runner's read-only gate (runner._readonly_gate) resolves identically and the two
    # can't drift. (_mcp_gate stays for the allow_llm / allow_jira_sync gates below.)
    return rebar.config.mcp_readonly()


def _allow_llm() -> bool:
    # Off by default; a malformed config ERRORS (never silently enables billable LLM
    # calls — and never silently reads as the operator's "off" either).
    return _mcp_gate("allow_llm")


def _allow_jira_sync() -> bool:
    # Off by default; a malformed config ERRORS (never silently enables live/applying
    # Jira writes — and never silently reads as the operator's "off" either).
    return _mcp_gate("allow_jira_sync")


# Keep MCP workflow status/result payloads under the client's ~25K-token budget
# (WS-ffc4). ~90 KB ≈ 25K tokens; over it, elide the bulky step outputs (which an
# agent can re-read via the library/CLI) while preserving the schema-valid shape.
_WORKFLOW_TOKEN_BUDGET_BYTES = 90_000


def _payload_bytes(payload: dict) -> int:
    import json

    return len(json.dumps(payload, default=str))


def _cap_workflow_payload(payload: dict) -> dict:
    """Bound a status/result payload under the ~25K-token MCP budget (WS-ffc4).

    Truncates the bulky carriers in escalating order until the WHOLE payload fits —
    bulk can live in `outputs`/`terminal_output` (result read) OR `steps` (status
    read) OR `error`/elsewhere — so the budget is airtight regardless of shape. The
    full result stays available via the library/CLI."""
    if _payload_bytes(payload) <= _WORKFLOW_TOKEN_BUDGET_BYTES:
        return payload
    note = (
        "[truncated to stay under the MCP token budget — read the full result via "
        "rebar.get_workflow_result / `rebar workflow result`]"
    )
    capped = dict(payload)
    capped["truncated"] = True
    # 1) elide the result carriers.
    if capped.get("terminal_output"):
        capped["terminal_output"] = {"_truncated": note}
    if isinstance(capped.get("outputs"), dict):
        capped["outputs"] = {sid: {"_truncated": note} for sid in capped["outputs"]}
    # 2) still over? collapse the per-step status map to a count (status read).
    if _payload_bytes(capped) > _WORKFLOW_TOKEN_BUDGET_BYTES and isinstance(
        capped.get("steps"), dict
    ):
        capped["steps"] = {"_truncated": f"{len(capped['steps'])} steps; {note}"}
    # 3) last resort: a minimal envelope that is guaranteed to fit + schema-valid.
    if _payload_bytes(capped) > _WORKFLOW_TOKEN_BUDGET_BYTES:
        capped = {
            "run_id": str(payload.get("run_id", "")),
            "status": str(payload.get("status", "")),
            "ticket_id": payload.get("ticket_id"),
            "workflow_name": payload.get("workflow_name"),
            "truncated": True,
            "error": note,
        }
    return capped


def _dump(item):
    """Normalize a typed list-item param to a plain dict (FastMCP may deliver a
    validated pydantic model or a raw dict depending on version). Drops keys whose
    value is None so the engine receives a clean {path,reason}/{dd_id,…} object."""
    if hasattr(item, "model_dump"):
        return {k: v for k, v in item.model_dump().items() if v is not None}
    return item


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _resolve_http_runtime(mcp_cfg, *, auth_enabled: bool):
    """Resolve the Streamable-HTTP runtime (host, port, path, TransportSecuritySettings)
    from config, applying the two fail-closed startup gates and the loopback-bind
    defaults. Raises :class:`McpStartupError` when a gate refuses the boot.

    Gates:
      * Unauthenticated-HTTP gate — refuse an auth-off HTTP boot unless the operator
        sets ``REBAR_MCP_ALLOW_UNAUTHENTICATED_HTTP``.
      * Non-loopback bind — a non-loopback host must supply BOTH allowlists AND
        acknowledge TLS-at-edge; a loopback host fills any empty allowlist with the
        explicit loopback defaults for the configured port.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    host = mcp_cfg.http_host
    port = mcp_cfg.http_port
    path = mcp_cfg.http_path

    # Gate 1: refuse to serve HTTP without either a token verifier or an explicit ack.
    if not auth_enabled and not mcp_cfg.allow_unauthenticated_http:
        raise McpStartupError(
            "refusing to start the Streamable-HTTP transport without authentication: "
            "no token verifier is configured. Set "
            "REBAR_MCP_ALLOW_UNAUTHENTICATED_HTTP=1 (mcp.allow_unauthenticated_http) to "
            "acknowledge running the HTTP transport unauthenticated."
        )

    allowed_hosts = list(mcp_cfg.http_allowed_hosts)
    allowed_origins = list(mcp_cfg.http_allowed_origins)
    is_loopback = host.strip().lower() in _LOOPBACK_HOSTS

    if is_loopback:
        # Fill each empty allowlist independently with explicit loopback defaults.
        if not allowed_hosts:
            allowed_hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
        if not allowed_origins:
            allowed_origins = [
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
                f"http://[::1]:{port}",
            ]
    else:
        # Gate 2: a non-loopback bind must be explicit about who may reach it AND that
        # TLS is terminated in front of it. Distinguish the two failure modes.
        if not allowed_hosts or not allowed_origins:
            raise McpStartupError(
                f"refusing to bind the Streamable-HTTP transport to non-loopback host "
                f"{host!r} with an empty allowlist: set both mcp.http_allowed_hosts "
                "(REBAR_MCP_HTTP_ALLOWED_HOSTS) and mcp.http_allowed_origins "
                "(REBAR_MCP_HTTP_ALLOWED_ORIGINS) to the exact host:port / origin values "
                "that may reach this server."
            )
        if not mcp_cfg.http_tls_at_edge:
            raise McpStartupError(
                f"refusing to bind the Streamable-HTTP transport to non-loopback host "
                f"{host!r} without a TLS acknowledgement: set mcp.http_tls_at_edge "
                "(REBAR_MCP_HTTP_TLS_AT_EDGE=1) to confirm TLS is terminated at the edge "
                "in front of this server."
            )

    ts = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    return host, port, path, ts


def build_server(cfg=None):
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.server import Settings
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "The rebar MCP server requires the 'mcp' extra. "
            "Install it with: pip install 'nava-rebar[mcp]'"
        ) from exc

    # MCP v1 leaves Settings.lifespan unresolved until the model is rebuilt after
    # FastMCP is defined; complete it before pydantic-settings inspects the field.
    Settings.model_rebuild()

    if cfg is None:
        cfg = rebar.config.compose_config()
    mcp_cfg = cfg.mcp

    # Auth seam (S2): OFF by default. When enabled, build the composite token verifier
    # (the SINGLE audience/fail-closed choke point) and the Resource-Server AuthSettings,
    # and pass BOTH to FastMCP so the SDK serves RFC 9728 PRM + the 401 challenge and
    # enforces required_scopes. A misconfigured composite raises AuthConfigError, which
    # propagates out of build_server (fail-closed startup).
    auth_enabled = bool(mcp_cfg.auth_enabled)
    token_verifier = None
    auth_settings = None
    if auth_enabled:
        from mcp.server.auth.settings import AuthSettings

        from rebar._mcp_auth import build_composite_verifier

        token_verifier = build_composite_verifier(mcp_cfg)
        auth_settings = AuthSettings(
            issuer_url=mcp_cfg.auth_issuer_url,
            resource_server_url=mcp_cfg.auth_resource_server_url,
            required_scopes=list(mcp_cfg.auth_required_scopes) or None,
        )

    if mcp_cfg.transport == "http":
        # Pass the real auth_enabled: an authenticated HTTP boot no longer needs the
        # allow_unauthenticated_http acknowledgement.
        host, port, path, ts = _resolve_http_runtime(mcp_cfg, auth_enabled=auth_enabled)
        # Construct the bind + transport-security AT FastMCP() time, not via late
        # attribute assignment, so the SDK wires the ASGI app with them.
        mcp = FastMCP(
            "rebar",
            host=host,
            port=port,
            streamable_http_path=path,
            transport_security=ts,
            token_verifier=token_verifier,
            auth=auth_settings,
        )
    else:
        # stdio has no HTTP surface — the verifier is constructed (so a bad config still
        # fails closed at startup) but the SDK only enforces auth over HTTP.
        mcp = FastMCP("rebar", token_verifier=token_verifier, auth=auth_settings)

    # Trusted-proxy header-guard (S5): when the `proxy` strategy is active, the forwarded
    # identity lives on HTTP headers the token-only verify_token seam never sees, so wrap
    # the Streamable-HTTP ASGI app with ProxyAuthMiddleware — it validates the shared
    # secret (constant-time), strips the X-Forwarded-* family / secret / identity headers
    # to defeat spoofing, and on a valid secret records the identity in a request-scoped
    # ContextVar that ProxyTokenVerifier (already in the composite) reads. The factory
    # already validated the secret env var is non-empty, so `secret` here is non-empty.
    if auth_enabled and "proxy" in mcp_cfg.auth_strategies:
        from rebar._mcp_auth import ProxyAuthMiddleware

        _secret = rebar.config.read_secret_env(mcp_cfg.auth_proxy_secret_env)
        _orig_app = mcp.streamable_http_app

        def _wrapped_streamable_http_app(*args, **kwargs):
            return ProxyAuthMiddleware(
                _orig_app(*args, **kwargs),
                secret=_secret,
                secret_header=mcp_cfg.auth_proxy_secret_header,
                identity_header=mcp_cfg.auth_proxy_identity_header,
            )

        mcp.streamable_http_app = _wrapped_streamable_http_app  # type: ignore[method-assign]

    # Shared handles + gate helpers the tool closures capture. Each registrar rebinds
    # these to their original local names so the tool bodies are copied verbatim.
    ctx = SimpleNamespace(
        readonly=_readonly,
        allow_llm=_allow_llm,
        allow_jira_sync=_allow_jira_sync,
        cap_workflow_payload=_cap_workflow_payload,
        dump=_dump,
        MODE_CAPS=MODE_CAPS,
        Mode=Mode,
        logger=logger,
    )

    # Install the structured-error guard BEFORE tool registration (ticket 8a31)
    from rebar._mcp_errors import install_error_guard

    install_error_guard(mcp)

    # Registration order matches the original in-line definition order (reads, then
    # the always-registered LLM tools, then the READONLY-gated writes).
    register_read_tools(mcp, ctx)
    register_llm_tools(mcp, ctx)
    register_write_tools(mcp, ctx)

    # Box-facing health/gauge/grace (ADR deft-evolutive-mosasaur): expose /health with
    # the certified-op in-flight gauge and instrument the LLM tools. Harmless for stdio
    # (the custom route is only served by the HTTP app). See rebar._mcp_health.
    from rebar._mcp_health import wire_health

    wire_health(mcp)
    return mcp


def compose_startup_opcert_binding(cfg):
    """Compose the ONE startup op-cert signer this server signs its certified-op certs under, or
    ``None`` when op-cert signing is not provisioned (the developer-local / stdio default).

    The on-box deployment materializes the box environment's Ed25519 key OUTSIDE the app and wires
    the mcp compose service with ``REBAR_OPCERT_ENV_ID`` (the pinned env_id) +
    ``REBAR_IDENTITY_SIGNING_KEY`` (the bind-mounted key path). When BOTH are set this composes an
    immutable :class:`~rebar.opcert_service.keyprov.OpcertSigner` from them — the SAME startup
    composition the trusted op-cert gate service uses (validate + 0600 process-owned copy) — so
    every cert the certified-op tools mint carries that env_id and verifies against the pinned
    public key (:mod:`rebar.attest.trusted_env`). The binding is threaded context-locally around
    serving (see :func:`rebar._mcp_health.run_mcp`); WITHOUT it the certified tools would sign the
    principal from ``REBAR_OPCERT_ENV_ID`` but under the wrong (genesis) key, so the cert would
    fail the required-environment verify.

    Fail-closed: when both inputs are present but the key is invalid (missing / not 0600 /
    encrypted / non-Ed25519), :func:`compose_signer` raises and startup aborts rather than serving
    with an unusable signer. When EITHER input is absent the binding is skipped and signing keeps
    its exact env/genesis behavior (regression-safe for the developer-local CLI / stdio path)."""
    import os

    raw_env_id = os.environ.get("REBAR_OPCERT_ENV_ID")  # read-via: identity-deployment-override
    env_id = (raw_env_id or "").strip()
    key_path = (cfg.identity.signing_key or "").strip()
    if not env_id or not key_path:
        return None
    from rebar.opcert_service.config import OpcertServiceConfig
    from rebar.opcert_service.keyprov import compose_signer

    return compose_signer(OpcertServiceConfig(env_id=env_id, key_path=key_path))


def main() -> None:
    # ``rebar-mcp`` takes no options — it speaks MCP-over-stdio. Respond to
    # ``--help`` / ``-h`` with a short usage and exit 0 instead of starting the
    # stdio server (so a curious `rebar-mcp --help` does not hang waiting on stdin,
    # and a CI boot check can confirm the entry point resolves).
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        # Env list is DERIVED from MCP_ENV_VARS so --help can't drift from the
        # manifest (server.json) or the real gates.
        env_lines = "\n".join(
            f"       {v['name']}{'  (deprecated alias)' if v['deprecated'] else ''}"
            for v in MCP_ENV_VARS
        )
        print(  # noqa: T201 — --help output belongs on stdout (server not yet started)
            "rebar-mcp — the rebar MCP server (FastMCP, stdio transport).\n"
            "Usage: rebar-mcp            # serve MCP over stdio (takes no options)\n"
            "Env:\n" + env_lines
        )
        return
    # Observability floor: install a stderr handler on the ``rebar`` root logger so
    # swallowed failures surface. Never stdout — MCP-over-stdio reserves stdout for
    # JSON-RPC framing. See ``rebar._logging`` for the convention.
    from rebar._logging import install_stderr_handler

    install_stderr_handler("rebar")

    # Boot store sweep + the no-store warning; see _mcp_health.run_startup_store_sweep.
    from rebar._mcp_health import run_startup_store_sweep

    run_startup_store_sweep()

    # Load config once; a malformed config (ConfigError) may propagate and fail startup
    # (fail-closed). The transport selection drives both build_server and run().
    cfg = rebar.config.compose_config()
    server = build_server(cfg)
    from rebar._mcp_health import run_mcp

    # Compose the box's op-cert signer (fail-closed on a bad key) and hand it to run_mcp, which
    # binds it context-locally INSIDE the serving thread so the certified-op tools mint certs
    # under the box environment. None ⇒ unprovisioned (dev / stdio) ⇒ unchanged env/genesis path.
    binding = compose_startup_opcert_binding(cfg)
    try:
        run_mcp(server, cfg.mcp, opcert_binding=binding)
    finally:
        if binding is not None:
            binding.cleanup()


if __name__ == "__main__":
    main()
