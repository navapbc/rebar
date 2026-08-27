"""Transport statelessness for the rebar MCP HTTP server (nemophilic-prettyish-cockroach).

Written and confirmed RED before the fix; mutation-checked after.
"""

from __future__ import annotations

from rebar._config_schema import Config, McpConfig
from rebar.mcp_server import build_server


def _http_server():
    cfg = Config(mcp=McpConfig(transport="http", allow_unauthenticated_http=True))
    srv = build_server(cfg)
    # Materializes the SDK's session manager; this is what serves /mcp.
    srv.streamable_http_app()
    return srv


def test_the_http_server_is_stateless_so_a_cutover_cannot_orphan_client_sessions() -> None:
    """THE BUG. The blue-green deploy swaps the nginx /mcp upstream to a new container the
    moment it is healthy (observed: 18:54:29Z, "mcp cutover complete: /mcp upstream now
    127.0.0.1:8093"). MCP streamable-HTTP sessions live IN MEMORY inside the container that
    minted them, so every client holding an Mcp-Session-Id is instantly orphaned and its next
    request 404s as "Session expired" — surfaced to the user as an opaque
    `rmcp::transport::worker` transport error.

    Retiring the old container gracefully cannot help: the upstream has already moved, so no
    further request reaches it. The only fix that makes this structurally impossible is to hold
    no per-container session state at all.

    This asserts the mechanism itself — the StreamableHTTPSessionManager's `stateless` flag —
    rather than the settings kwarg, because the session manager is the object whose in-memory
    session table produces the 404.
    """
    srv = _http_server()

    manager = getattr(srv, "_session_manager", None)
    assert manager is not None, (
        "precondition: building an HTTP server must materialize a session manager; "
        "if this fails the SDK's internals moved and this test needs updating, "
        "which is NOT the same as the server being stateless"
    )
    assert manager.stateless is True, (
        "the HTTP server must run STATELESS: with per-container session state, any mcp "
        "redeploy (autodeploy ticks every 2 minutes) orphans every live client session"
    )


def test_the_stateless_setting_is_what_reaches_the_sdk() -> None:
    """Guards the wiring, not a duplicate of the above: a future refactor could set the
    setting on a Settings object that never reaches FastMCP, leaving the session manager
    stateful while the config looks correct. Both must agree.
    """
    srv = _http_server()

    assert srv.settings.stateless_http is True, (
        "settings.stateless_http must be True on the server actually built and served"
    )
