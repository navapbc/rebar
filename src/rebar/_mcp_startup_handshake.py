"""Startup-handshake call-graph leaf for the rebar MCP HTTP transport.

This module owns the ONE real ``initialize`` probe that the HTTP transport drives
through its own session manager during ASGI startup, before uvicorn accepts
connections. It intentionally has no imports from :mod:`rebar._mcp_health` or
:mod:`rebar._mcp_serving`: ``_mcp_health`` re-exports these helpers as its public
facade, and ``_mcp_serving`` continues to call that facade unchanged.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

DEFAULT_HANDSHAKE_BUDGET_SECONDS = 10.0
"""Upper bound (seconds) on the startup MCP handshake. A module constant for the same
reason as ``DEFAULT_SHUTDOWN_GRACE_SECONDS`` in ``_mcp_health`` — a config key would
ripple into ``MCP_ENV_VARS`` / ``server.json`` / the env-var docs generators."""

_HANDSHAKE_ATTR = "_rebar_startup_handshake"

_INITIALIZE_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "rebar-startup-handshake", "version": "1"},
        },
    }
).encode()
"""The one real MCP request the startup handshake drives through this server."""


def _declared_public_host(resource_server_url: Any) -> str:
    """The Host real traffic carries, from the deployment's declared public resource URL."""

    raw = str(resource_server_url or "").strip()
    if not raw:
        return ""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return ""
    if not host:
        return ""
    default_port = 443 if parts.scheme == "https" else 80
    return host if port in (None, default_port) else f"{host}:{port}"


def select_probe_host(
    security: Any,
    *,
    resource_server_url: Any = None,
    bind_host: str = "",
    bind_port: int = 0,
) -> str:
    """The Host header the startup handshake presents to this server's own guard."""

    declared = _declared_public_host(resource_server_url)
    if declared:
        return declared
    entries = [str(entry).strip() for entry in (getattr(security, "allowed_hosts", None) or [])]
    for entry in entries:
        if entry and "*" not in entry:
            return entry
    for entry in entries:
        if entry.endswith(":*") and "*" not in entry[:-2]:
            return f"{entry[:-2]}:{bind_port}"
    host = (bind_host or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return f"{host}:{bind_port}"


def _probe_scope(*, probe_host: str, path: str, bind_port: int) -> dict[str, Any]:
    """The ASGI scope of the startup ``initialize`` POST."""

    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", probe_host.encode()),
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
            (b"content-length", str(len(_INITIALIZE_BODY)).encode()),
        ],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", int(bind_port)),
    }


async def drive_initialize(
    session_manager: Any, *, probe_host: str, path: str, bind_port: int
) -> int | None:
    """Drive one real ``initialize`` through ``session_manager`` and return its HTTP status."""

    scope = _probe_scope(probe_host=probe_host, path=path, bind_port=bind_port)
    statuses: list[int] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": _INITIALIZE_BODY, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message.get("type") == "http.response.start":
            statuses.append(int(message["status"]))

    await session_manager.handle_request(scope, receive, send)
    return statuses[0] if statuses else None


async def run_startup_handshake(
    drive: Callable[[], Any],
    *,
    probe_host: str,
    budget_seconds: float = DEFAULT_HANDSHAKE_BUDGET_SECONDS,
    fail_after: Any = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run ``drive`` under a bound and report its outcome."""

    import anyio

    from rebar._deprecations import RemovedInputError

    scope_factory = fail_after or anyio.fail_after
    started = monotonic()
    record: dict[str, Any] = {
        "ok": False,
        "status": None,
        "host": probe_host,
        "elapsed_ms": 0.0,
        "error": None,
    }
    try:
        with scope_factory(budget_seconds):
            status = await drive()
        record["status"] = status
        record["ok"] = status == 200
        if not record["ok"]:
            record["error"] = f"initialize returned HTTP {status}"
    except TimeoutError:
        record["error"] = f"handshake exceeded its {budget_seconds}s budget"
    except RemovedInputError:
        raise
    except Exception as exc:  # noqa: BLE001 - the handshake never raises
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["elapsed_ms"] = round((monotonic() - started) * 1000.0, 3)
    return record


def handshake_status(mcp: Any) -> dict[str, Any]:
    """The recorded startup-handshake outcome, or a fail-closed not-run record."""

    record = getattr(mcp, _HANDSHAKE_ATTR, None)
    if isinstance(record, dict):
        return record
    return {"ok": False, "status": None, "error": "startup handshake did not run"}


def _starlette_app(app: Any) -> Any:
    """The Starlette app inside ``app``, unwrapping ASGI middleware."""

    for _ in range(8):
        if hasattr(app, "router"):
            return app
        app = getattr(app, "app", None)
        if app is None:
            return None
    return None


def install_startup_handshake(
    mcp: Any,
    app: Any,
    *,
    budget_seconds: float = DEFAULT_HANDSHAKE_BUDGET_SECONDS,
    drive: Callable[[], Any] | None = None,
) -> Any:
    """Wrap ``app``'s ASGI lifespan so the handshake runs before uvicorn serves."""

    from rebar._deprecations import RemovedInputError

    inner = _starlette_app(app)
    if inner is None:  # pragma: no cover - defensive: a stub server in a test
        return app
    try:
        settings = getattr(mcp, "settings", None)
        probe_host = select_probe_host(
            getattr(settings, "transport_security", None),
            resource_server_url=getattr(
                getattr(settings, "auth", None), "resource_server_url", None
            ),
            bind_host=str(getattr(settings, "host", "") or ""),
            bind_port=int(getattr(settings, "port", 0) or 0),
        )
        path = str(getattr(settings, "streamable_http_path", "/mcp") or "/mcp")
        bind_port = int(getattr(settings, "port", 0) or 0)
        original = inner.router.lifespan_context
    except RemovedInputError:
        raise
    except Exception:
        logging.getLogger("rebar").warning(
            "startup MCP handshake not installed; /health will report it as not run",
            exc_info=True,
        )
        return app

    if drive is None:

        async def drive() -> int | None:
            return await drive_initialize(
                mcp.session_manager, probe_host=probe_host, path=path, bind_port=bind_port
            )

    @contextlib.asynccontextmanager
    async def _lifespan(scope_app: Any) -> Any:
        async with original(scope_app):
            record = await run_startup_handshake(
                drive, probe_host=probe_host, budget_seconds=budget_seconds
            )
            setattr(mcp, _HANDSHAKE_ATTR, record)
            if not record["ok"]:
                logging.getLogger("rebar").error(
                    "startup MCP handshake FAILED for host %s: %s — this container answers "
                    "/health but may not be able to serve an MCP request",
                    record["host"],
                    record["error"],
                )
            yield

    inner.router.lifespan_context = _lifespan
    return app
