"""rebar MCP server (FastMCP).

Exposes the ticket system as MCP tools, built on the rebar Python library.
Reads (``show``/``list``) run in-process via rebar._reads (no subprocess);
``bridge_sync`` defaults to non-mutating dry-run behavior.

Safety:
  * ``bridge_sync`` requires REBAR_MCP_ALLOW_JIRA_SYNC=1 for live Jira sync.
  * Write tools (create/transition/edit/link/unlink/tag/untag/archive/comment)
    are gated by REBAR_MCP_READONLY: set it to 1 to expose a read-only server.

The ``mcp`` dependency is an optional extra and is imported lazily.

Structure: ``build_server`` is a thin assembler — it builds the FastMCP server,
packs the shared handles + gate helpers into a ``ctx`` namespace, and calls the
three per-cluster registrars (``register_read_tools`` / ``register_llm_tools`` /
``register_write_tools`` in ``_mcp_reads`` / ``_mcp_llm`` / ``_mcp_writes``). The
gate helpers, the workflow-payload budget cap, and the
startup entry points live here. Tool results are plain dicts; ``rebar._mcp_schema``
advertises each one's canonical JSON Schema as its ``outputSchema``.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import rebar
from rebar._mcp_budget import (
    _LIST_TOKEN_BUDGET_BYTES,  # noqa: F401  (re-exported: tests read the budget here)
    _bound_list_payload,
    _cap_workflow_payload,
)
from rebar._mcp_llm import register_llm_tools
from rebar._mcp_reads import register_read_tools
from rebar._mcp_writes import register_write_tools

logger = logging.getLogger(__name__)

__all__ = [
    "MCP_ENV_VARS",
    "McpStartupError",
    "build_server",
    "main",
]


class McpStartupError(RuntimeError):
    """Raised when the MCP server cannot start under the requested configuration —
    a fail-closed startup gate (e.g. an unauthenticated HTTP transport without the
    acknowledgement, or a non-loopback bind missing its allowlists / TLS ack)."""


# ── Canonical MCP environment-variable contract ──────────────────────────────
# Keep MCP env names here for server.json drift checks and derived --help text.
# Each entry names one gate and gives its short description.
MCP_ENV_VARS: tuple[dict, ...] = (
    {
        "name": "REBAR_ROOT",
        "description": (
            "Path to the repo root that holds the .tickets-tracker store "
            "(defaults to the git toplevel of the working dir)."
        ),
    },
    {
        "name": "REBAR_MCP_READONLY",
        "description": "Set to 1 to expose only the read tools (no write/mutation tools).",
    },
    {
        "name": "REBAR_MCP_ALLOW_LLM",
        "description": (
            "Set to 1 to enable the billable LLM tools (review_code / scan_spec / "
            "verify_completion / review_plan); off by default."
        ),
    },
    {
        "name": "REBAR_MCP_DEDUP",
        "description": (
            "In-flight de-duplication of the long-running gate tools (review_plan / "
            "verify_completion): a concurrent duplicate call for the same ticket+basis "
            "attaches to the running gate instead of launching a second billable LLM "
            "pass. On by default; set to 0 to disable (kill-switch)."
        ),
    },
    {
        "name": "REBAR_MCP_ALLOW_JIRA_SYNC",
        "description": (
            "Set to 1 to allow bridge_sync to perform live (mutating) Jira sync; "
            "otherwise bridge_sync uses dry-run behavior."
        ),
    },
    {
        "name": "REBAR_MCP_TRANSPORT",
        "description": (
            "Transport for the MCP server: 'stdio' (default) or 'http' (the optional "
            "Streamable-HTTP transport)."
        ),
    },
    {
        "name": "REBAR_MCP_HTTP_HOST",
        "description": "Bind host for the Streamable-HTTP transport (default 127.0.0.1).",
    },
    {
        "name": "REBAR_MCP_HTTP_PORT",
        "description": "Bind port for the Streamable-HTTP transport (1-65535; default 8000).",
    },
    {
        "name": "REBAR_MCP_HTTP_PATH",
        "description": "URL path the Streamable-HTTP transport serves on (default /mcp).",
    },
    {
        "name": "REBAR_MCP_HTTP_ALLOWED_HOSTS",
        "description": (
            "Comma-separated allowlist of exact host:port values accepted by the "
            "Streamable-HTTP DNS-rebinding protection; empty defaults to loopback."
        ),
    },
    {
        "name": "REBAR_MCP_HTTP_ALLOWED_ORIGINS",
        "description": (
            "Comma-separated allowlist of exact Origin values accepted by the "
            "Streamable-HTTP DNS-rebinding protection; empty defaults to loopback."
        ),
    },
    {
        "name": "REBAR_MCP_HTTP_TLS_AT_EDGE",
        "description": (
            "Set to 1 to acknowledge TLS is terminated at the edge; required to bind "
            "the Streamable-HTTP transport to a non-loopback host."
        ),
    },
    {
        "name": "REBAR_MCP_ALLOW_UNAUTHENTICATED_HTTP",
        "description": (
            "Set to 1 to acknowledge running the Streamable-HTTP transport without a "
            "token verifier; required to boot the HTTP transport while auth is off."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_ENABLED",
        "description": (
            "Set to 1 to enable MCP authentication (the composite token verifier + "
            "Resource-Server wiring); off by default."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_STRATEGIES",
        "description": (
            "Comma-separated, ordered list of token-verifier strategies to compose "
            "(closed set: static, jwt, introspection, proxy, custom)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_ISSUER_URL",
        "description": (
            "OAuth authorization-server issuer URL advertised in the Protected-Resource "
            "Metadata (RFC 9728) when auth is enabled."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_RESOURCE_SERVER_URL",
        "description": (
            "The single resource identifier (RFC 8707 audience) for this server; the "
            "composite verifier re-checks every accepted token against it."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_REQUIRED_SCOPES",
        "description": (
            "Comma-separated scopes a caller must hold; the SDK returns 403 "
            "insufficient_scope when a principal lacks one."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_STATIC_TOKENS_FILE",
        "description": (
            "Path to the JSON secrets file for the static-bearer verifier (stores only "
            "SHA-256 digests of the accepted tokens)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_JWKS_URI",
        "description": (
            "HTTPS JWKS endpoint the `jwt` verifier fetches signing keys from (an OIDC "
            "provider's .well-known/jwks.json)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_ISSUER",
        "description": (
            "Expected `iss` claim for the `jwt` verifier; falls back to "
            "REBAR_MCP_AUTH_ISSUER_URL when unset."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_ALGORITHMS",
        "description": (
            "Comma-separated PINNED, asymmetric-only JWS algorithms for the `jwt` verifier "
            "(default RS256,ES256); a symmetric algorithm on a JWKS source is refused."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_LEEWAY",
        "description": (
            "Clock-skew leeway in seconds applied to exp/nbf validation by the `jwt` "
            "verifier (default 60)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_JWKS_REFETCH_COOLDOWN",
        "description": (
            "Minimum seconds between JWKS refetches triggered by an unknown key id "
            "(the concurrency-safe flood guard; default 30)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_JWKS_TIMEOUT",
        "description": (
            "HTTP timeout in seconds for the `jwt` verifier's JWKS fetch (default 10)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_EXPECTED_TYP",
        "description": (
            "When set, the `jwt` verifier requires the JWT header `typ` to equal this "
            "(e.g. at+JWT per RFC 9068); unset skips the check."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_JWT_ALLOW_PRIVATE_JWKS_HOST",
        "description": (
            "Set to 1 to permit a private/link-local/loopback JWKS host (SSRF guard is "
            "on by default); off by default."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_INTROSPECTION_ENDPOINT",
        "description": (
            "The `introspection` verifier's RFC 7662 endpoint URL (must be https://); the "
            "opaque token is POSTed here on every request (no caching)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_INTROSPECTION_CLIENT_ID",
        "description": (
            "The client id the `introspection` verifier presents to the Authorization "
            "Server via HTTP Basic (client_secret_basic)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_INTROSPECTION_CLIENT_SECRET_ENV",
        "description": (
            "The NAME of the env var holding the introspection client secret (never the "
            "secret itself); must be present + non-empty at startup or the server refuses "
            "to start (fail-closed)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_INTROSPECTION_ALLOW_PRIVATE_HOST",
        "description": (
            "Set to 1 to permit a private/link-local/loopback introspection endpoint host "
            "(SSRF guard is on by default); off by default."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_INTROSPECTION_ALLOW_MISSING_AUD",
        "description": (
            "Set to 1 to accept an active introspection response that OMITS `aud` (many "
            "AS do); off by default (fail-closed reject)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_PROXY_SECRET_ENV",
        "description": (
            "The NAME of the env var holding the trusted-proxy shared secret (never the "
            "secret itself); must be present + non-empty at startup or the `proxy` verifier "
            "refuses to start (fail-closed)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_PROXY_SECRET_HEADER",
        "description": (
            "The header the fronting proxy sends its shared secret on; the identity is "
            "trusted only when this matches (constant-time; default x-proxy-auth)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_PROXY_IDENTITY_HEADER",
        "description": (
            "The header carrying the proxy-authenticated principal identity, trusted only "
            "when the secret header validates (default x-forwarded-user)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_PROXY_SCOPES",
        "description": (
            "Comma-separated fixed scope set granted to proxy-authenticated principals; "
            "empty by default (the principal holds no scopes)."
        ),
    },
    {
        "name": "REBAR_MCP_AUTH_CUSTOM_IMPORT",
        "description": (
            "The `custom` strategy's `module:factory` import string, resolving to a factory "
            "that returns a TokenVerifier; a TRUSTED operator config value that loads and "
            "executes the operator-configured code at startup (fail-closed on any load error)."
        ),
    },
)


def _mcp_gate(attr: str) -> bool:
    """Resolve typed MCP booleans with env precedence and explicit config errors.

    The structured-error guard reports unreadable config, not a policy value.
    """
    return rebar.config.mcp_gate(attr)


def _readonly() -> bool:
    # Share the typed read-only resolver with LLM tools; malformed config errors.
    return rebar.config.mcp_readonly()


def _allow_llm() -> bool:
    # Off by default; a malformed config ERRORS (never silently enables billable LLM
    # calls — and never silently reads as the operator's "off" either).
    return _mcp_gate("allow_llm")


def _allow_jira_sync() -> bool:
    # Off by default; a malformed config ERRORS (never silently enables live/applying
    # Jira writes — and never silently reads as the operator's "off" either).
    return _mcp_gate("allow_jira_sync")


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _resolve_http_runtime(mcp_cfg, *, auth_enabled: bool):
    """Resolve HTTP bind and security settings with fail-closed startup gates.

    Unauthenticated HTTP needs explicit acknowledgement. Non-loopback binds need
    both allowlists and TLS-at-edge acknowledgement; loopback fills defaults.
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

    # When enabled, pass one composite verifier and resource-server settings to
    # FastMCP; invalid auth config fails startup.
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
            # Stateless HTTP survives blue-green upstream swaps without orphaning
            # container-local session IDs. Tools are request/response; workflows poll.
            stateless_http=True,
        )
    else:
        # stdio has no HTTP surface — the verifier is constructed (so a bad config still
        # fails closed at startup) but the SDK only enforces auth over HTTP.
        mcp = FastMCP("rebar", token_verifier=token_verifier, auth=auth_settings)

    # Proxy auth validates the shared secret, strips spoofable forwarding headers,
    # and binds verified identity to this request before token verification.
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
        bound_list_payload=_bound_list_payload,
        logger=logger,
    )

    # Install the structured-error guard BEFORE tool registration (ticket 8a31), then the
    # JS-safe-integer result guard (bug 6fe7), then the canonical outputSchema layer
    # (story quilted-nepotic-kitty). All three rebind `mcp.tool` and compose.
    from rebar._mcp_errors import install_error_guard, install_js_safe_guard
    from rebar._mcp_schema import install_canonical_output_schemas

    install_error_guard(mcp)
    install_js_safe_guard(mcp)
    install_canonical_output_schemas(mcp)

    # Register reads, LLM tools, then gated writes. Scoped advisory suppression
    # avoids duplicate warning channels; each tool binds one operation snapshot.
    # LLM calls also bind their own LLMConfig in the delegated library seam.
    from rebar._lib_warn import suppress_library_double_advisory
    from rebar._operation_config import bind_operation_snapshot_for_tools

    def _instrumented(inner_mcp):
        return bind_operation_snapshot_for_tools(suppress_library_double_advisory(inner_mcp))

    register_read_tools(_instrumented(mcp), ctx)
    register_llm_tools(_instrumented(mcp), ctx)
    register_write_tools(_instrumented(mcp), ctx)

    # Box-facing health/gauge/grace (ADR deft-evolutive-mosasaur): expose /health with
    # the certified-op in-flight gauge and instrument the LLM tools. Harmless for stdio
    # (the custom route is only served by the HTTP app). See rebar._mcp_health.
    from rebar._mcp_health import wire_health

    wire_health(mcp)
    return mcp


def compose_startup_opcert_binding(cfg):
    """Bind the pinned environment's startup signer when both inputs exist.

    Compose a process-owned Ed25519 key copy so certified tools sign under the
    claimed principal. Invalid provisioned keys fail startup; missing inputs
    retain the developer-local env/genesis path.
    """
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
    # Handle --help before starting stdio so it cannot wait for MCP input.
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        # Env list is DERIVED from MCP_ENV_VARS so --help can't drift from the
        # manifest (server.json) or the real gates.
        env_lines = "\n".join(f"       {v['name']}" for v in MCP_ENV_VARS)
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

    # Bind a valid box signer inside the serving thread; unprovisioned runs
    # retain env/genesis signing.
    binding = compose_startup_opcert_binding(cfg)
    try:
        run_mcp(server, cfg.mcp, opcert_binding=binding)
    finally:
        if binding is not None:
            binding.cleanup()


if __name__ == "__main__":
    main()
