"""Serve MCP HTTP with a bounded certified-op drain before uvicorn shutdown.

On SIGTERM, close new certified intake, wait for active operations, then stop
uvicorn with a separate short idle-stream backstop. ``_mcp_health`` re-exports
this call cluster and retains its gauge and handshake primitives.
"""

from __future__ import annotations

import logging
import os
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
    serving_gauge,
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


def wait_for_idle(
    gauge: InFlightGauge,
    *,
    poll_interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Block until the gauge reads 0, so no drained tool or daemon is still running.

    :func:`run_mcp` calls this before returning, so ``mcp_server.main()``'s
    ``binding.cleanup()`` never deletes the key copy under a close or gate that is still
    signing (bug external-taxonomic-iguanodon). It has no deadline of its own: each counted
    operation is bounded by its own cap, and the container stop timeout bounds the rest.
    """
    while gauge.value > 0:
        sleep(poll_interval)


Retire = Callable[[], Callable[[float], None]]
"""Start retiring background store work; return ``finish(deadline)`` for the exit path."""


def make_sigterm_handler(
    server: Any,
    gauge: InFlightGauge,
    *,
    grace_seconds: float,
    poll_interval: float,
    retire: Retire | None = None,
) -> Callable[[int, Any], None]:
    """Close certified intake, drain active ops, then request uvicorn shutdown.

    This order preserves active streaming responses while the short uvicorn
    backstop handles only idle streams. ``retire`` runs first, so a detached compaction
    sweep is asked to stop while the drain runs, and its ``finish`` runs last, before
    ``should_exit``, within the same grace (bug topnotch-brazen-slug). The handler is
    testable without a signal.
    """

    def _on_sigterm(_signum: int, _frame: Any) -> None:
        gauge.begin_draining()
        deadline = time.monotonic() + grace_seconds
        finish = _start_retire(retire)

        def _exit() -> None:
            if finish is not None:
                _run_finish(finish, deadline)
            server.should_exit = True

        drain_then_exit(
            gauge,
            grace_seconds=grace_seconds,
            poll_interval=poll_interval,
            exit_fn=_exit,
        )

    return _on_sigterm


def _start_retire(retire: Retire | None) -> Callable[[float], None] | None:
    if retire is None:
        return None
    try:
        return retire()
    except Exception:
        logging.getLogger("rebar").warning("retire: could not stop background work", exc_info=True)
        return None


def _run_finish(finish: Callable[[float], None], deadline: float) -> None:
    try:
        finish(deadline)
    except Exception:
        logging.getLogger("rebar").warning("retire: wait for background work failed", exc_info=True)


def retire_store_work() -> Callable[[float], None]:
    """Stop this container's compaction sweep; ``finish`` waits for it and the write lock."""
    from rebar import config
    from rebar._commands import compact_trigger

    tracker = str(config.tracker_dir())
    if not os.path.isdir(tracker):
        return lambda _deadline: None
    return compact_trigger.retire_local_sweep(tracker)


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
        with bound_signer(opcert_binding, push_mode=None), serving_gauge(gauge):
            server.run()

    thread = threading.Thread(target=_serve, name="rebar-mcp-http")
    thread.start()

    signal.signal(
        signal.SIGTERM,
        make_sigterm_handler(
            server,
            gauge,
            grace_seconds=grace_seconds,
            poll_interval=poll_interval,
            retire=retire_store_work,
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

    gauge = getattr(server, _GAUGE_ATTR, None)
    try:
        _run_transport(server, mcp_cfg, gauge, opcert_binding)
    finally:
        # Work a stopped transport abandoned keeps running on its worker; wait for it so
        # the caller's signer cleanup cannot pull the key out from under it.
        if gauge is not None:
            wait_for_idle(gauge)


def _run_transport(server: Any, mcp_cfg: Any, gauge: Any, opcert_binding: Any) -> None:
    """Serve one transport: HTTP with the bounded drain, or stdio."""
    if mcp_cfg.transport == "http":
        if gauge is None:
            # build_server always wire_health()s the gauge; a missing one means this
            # server was assembled another way. Log it (an empty gauge would silently
            # read 0 and skip the drain) and fall back to a fresh gauge so the run
            # still starts rather than crashing.
            logging.getLogger("rebar").warning(
                "MCP HTTP server has no wired in-flight gauge; SIGTERM drain will not "
                "observe in-flight ops. Was build_server() used?"
            )
            gauge = InFlightGauge()
        run_http_with_grace(server, gauge, opcert_binding=opcert_binding)
    else:
        from rebar._opcert_binding import bound_signer

        with bound_signer(opcert_binding, push_mode=None), serving_gauge(gauge):
            server.run(transport="stdio")
