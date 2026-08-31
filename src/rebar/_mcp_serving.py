"""Serving runtime and bounded graceful shutdown for the rebar MCP HTTP transport.

This is the serving/shutdown call-graph seam of :mod:`rebar._mcp_health`: the four
functions here form one cluster that already called each other
(``run_mcp`` -> ``run_http_with_grace`` -> ``make_sigterm_handler`` ->
``drain_then_exit``) and depend on the gauge/handshake primitives that stay in
``_mcp_health``. They were extracted so ``_mcp_health`` stays under the 800-LOC module
cap after the bug-2f46 fast-drain fix; ``_mcp_health`` re-exports every public name here
so ``from rebar._mcp_health import run_mcp`` (and the monkeypatch paths the tests use)
keep working unchanged.

Bug 2f46: on SIGTERM a retiring container now (1) CLOSES new certified intake via
:meth:`rebar._mcp_health.InFlightGauge.begin_draining`, (2) waits (bounded by the
certified-op ``grace_seconds``) for the in-flight op to finish, and only THEN tells
uvicorn to stop. uvicorn's own ``timeout_graceful_shutdown`` is bound to the SHORT
:data:`rebar._mcp_health.DEFAULT_UVICORN_GRACEFUL_SECONDS`, decoupled from the op grace,
so a 0-in-flight container fast-drains instead of pinning a blue-green port for ~20 min.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from rebar._mcp_health import (
    _GAUGE_ATTR,
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    DEFAULT_UVICORN_GRACEFUL_SECONDS,
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
    """Build the SIGTERM handler used by :func:`run_http_with_grace`.

    On SIGTERM it (1) CLOSES new certified intake immediately
    (:meth:`InFlightGauge.begin_draining` — a new certified call is then refused with
    :class:`MCPRetiringError` so it retries the live container and cannot re-inflate the
    gauge), (2) waits (bounded by ``grace_seconds``) for the in-flight certified op to
    finish, and only THEN (3) tells uvicorn to stop serving (``should_exit`` — uvicorn runs
    its own graceful shutdown, bounded by the SHORT ``timeout_graceful_shutdown`` backstop).

    Draining the gauge BEFORE setting ``should_exit`` is the bug-2f46 fix: a real in-flight
    op's streaming response completes normally while uvicorn keeps serving, and uvicorn's
    (now short) graceful-shutdown backstop only ever sweeps IDLE held-open streams once the
    gauge is already 0 — it never truncates a real op, and a retiring container no longer
    burns the full 1200s op grace pinning a blue-green port. Intake is closed via the gauge
    (not via ``should_exit`` first) precisely so the short backstop cannot force-close a
    still-in-flight op. Extracted as a seam so the handler BODY is directly testable without
    delivering a real signal."""

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
    uvicorn_graceful_seconds: float = DEFAULT_UVICORN_GRACEFUL_SECONDS,
    poll_interval: float = 0.5,
    opcert_binding: Any = None,
) -> None:
    """Serve the Streamable-HTTP app with an owned, bounded SIGTERM grace.

    uvicorn only installs its own signal handlers on the main thread, so we run the
    server on a background thread (it therefore installs none) and own the SIGTERM
    handler on the main thread (see :func:`make_sigterm_handler`).
    ``timeout_graceful_shutdown`` is uvicorn's OWN backstop and is bounded by the SHORT
    ``uvicorn_graceful_seconds`` (default :data:`DEFAULT_UVICORN_GRACEFUL_SECONDS`),
    DELIBERATELY DECOUPLED from ``grace_seconds`` (the certified-op drain budget) — bug
    2f46: binding the two made a retiring container wait the full op grace for idle
    held-open client streams and pin a blue-green port ~20 min. The certified-op drain is
    enforced by the gauge poll in the SIGTERM handler (which runs before ``should_exit``),
    so this short backstop only sweeps idle streams and never truncates a real op. The main
    thread joins with a timeout so it stays responsive to the signal (a bare ``join()``
    would defer handler delivery).

    ``opcert_binding`` (the box's startup op-cert signer, or ``None``) is bound
    context-locally INSIDE the serving thread's target, not in the caller's thread:
    a :class:`contextvars.ContextVar` set on the main thread is NOT inherited by the
    background thread, so the binding must be entered where the request-handling event
    loop actually runs. ``None`` is a transparent no-op (the unprovisioned path)."""

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
        timeout_graceful_shutdown=int(uvicorn_graceful_seconds),
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
    """Run ``server`` for the configured transport. HTTP uses the bounded-grace runner
    above; stdio delegates to FastMCP's own run loop (no HTTP surface to drain).

    ``opcert_binding`` (or ``None``) is the box's startup op-cert signer; it is threaded to
    the serving thread so the certified-op tools mint certs under the box environment. HTTP
    binds it inside the uvicorn thread target (:func:`run_http_with_grace`); stdio binds it
    around ``server.run`` here (same thread). ``None`` is a transparent no-op."""

    # Bug 879b serve-degraded surface: stash the pinned-key match status for /health and log a
    # boot warning if the bound signer is not the pinned trusted-environment key. Never aborts.
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
