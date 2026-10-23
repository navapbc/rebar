"""Serve MCP HTTP with a bounded certified-op drain before uvicorn shutdown.

On SIGTERM, close new certified intake, wait for active operations, then stop
uvicorn with a separate short idle-stream backstop. ``_mcp_health`` re-exports
this call cluster and retains its gauge and handshake primitives.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from rebar._mcp_health import (
    _GAUGE_ATTR,
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    DEFAULT_UVICORN_BACKSTOP_SECONDS,
    InFlightGauge,
    install_startup_handshake,
)
from rebar._mcp_opcert_health import (
    _OPCERT_STATUS_ATTR,
    opcert_signing_status,
    run_startup_opcert_check,
)


def drain_then_exit(
    gauge: InFlightGauge,
    *,
    grace_seconds: float,
    exit_fn: Callable[[], None],
    poll_interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Wait (bounded by ``grace_seconds``) for the gauge to reach 0, then call
    ``exit_fn``. The clock/sleep are injectable so the bound is unit-testable without
    real time. Returns immediately when the gauge is already idle."""

    deadline = monotonic() + grace_seconds
    while gauge.value > 0 and monotonic() < deadline:
        sleep(poll_interval)
    exit_fn()


def make_sigterm_handler(
    server: Any,
    gauge: InFlightGauge,
    *,
    grace_seconds: float,
    poll_interval: float,
) -> Callable[[int, Any], None]:
    """Close certified intake, drain active ops, then request uvicorn shutdown.

    This order preserves active streaming responses while the short uvicorn
    backstop handles only idle streams. The handler is testable without a signal.
    """

    def _on_sigterm(_signum: int, _frame: Any) -> None:
        gauge.begin_draining()
        drain_then_exit(
            gauge,
            grace_seconds=grace_seconds,
            poll_interval=poll_interval,
            exit_fn=lambda: setattr(server, "should_exit", True),
        )

    return _on_sigterm


def run_http_with_grace(
    mcp: Any,
    gauge: InFlightGauge,
    *,
    grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    uvicorn_backstop_seconds: float = DEFAULT_UVICORN_BACKSTOP_SECONDS,
    poll_interval: float = 0.5,
    opcert_binding: Any = None,
) -> None:
    """Run HTTP in a serving thread while the main thread handles SIGTERM.

    Gauge drain precedes ``should_exit``; uvicorn's short idle-stream backstop is
    separate from certified-op grace. Timed joins keep signals responsive.
    Bind the optional signer inside the serving thread, where requests run.
    """

    import signal

    import uvicorn

    from rebar._opcert_binding import bound_signer

    app = mcp.streamable_http_app()
    # Prove the MCP REQUEST PATH works before uvicorn accepts its first connection: the
    # handshake runs inside this app's ASGI lifespan, so a later /health 200 means this
    # container has already served an `initialize` (bug vaccinated-flavorous-solenodon).
    app = install_startup_handshake(mcp, app)
    config = uvicorn.Config(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
        timeout_graceful_shutdown=int(uvicorn_backstop_seconds),
    )
    server = uvicorn.Server(config)

    def _serve() -> None:
        # push_mode=None: sign under the box environment but leave the outbound push policy
        # to env/config, so the box still auto-pushes its ticket writes to the shared store.
        with bound_signer(opcert_binding, push_mode=None):
            server.run()

    thread = threading.Thread(target=_serve, name="rebar-mcp-http")
    thread.start()

    signal.signal(
        signal.SIGTERM,
        make_sigterm_handler(
            server, gauge, grace_seconds=grace_seconds, poll_interval=poll_interval
        ),
    )

    while thread.is_alive():
        thread.join(timeout=0.2)


def run_mcp(server: Any, mcp_cfg: Any, *, opcert_binding: Any = None) -> None:
    """Run HTTP with bounded drain or stdio through FastMCP.

    Bind the optional op-cert signer where each transport executes requests.
    """

    # Expose pinned-key match status through /health and warn without aborting.
    setattr(server, _OPCERT_STATUS_ATTR, opcert_signing_status(opcert_binding))
    run_startup_opcert_check(opcert_binding)

    if mcp_cfg.transport == "http":
        gauge = getattr(server, _GAUGE_ATTR, None)
        if gauge is None:
            # build_server always wire_health()s the gauge; a missing one means this
            # server was assembled another way. Log it (an empty gauge would silently
            # read 0 and skip the drain) and fall back to a fresh gauge so the run
            # still starts rather than crashing.
            import logging

            logging.getLogger("rebar").warning(
                "MCP HTTP server has no wired in-flight gauge; SIGTERM drain will not "
                "observe in-flight ops. Was build_server() used?"
            )
            gauge = InFlightGauge()
        run_http_with_grace(server, gauge, opcert_binding=opcert_binding)
    else:
        from rebar._opcert_binding import bound_signer

        with bound_signer(opcert_binding, push_mode=None):
            server.run(transport="stdio")
