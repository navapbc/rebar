"""Health, in-flight instrumentation, and bounded graceful shutdown for the rebar
MCP HTTP transport (ADR deft-evolutive-mosasaur / docs/adr/0104-mcp-on-box.md).

``mcp_server.py`` sits at its 800-LOC module cap, so the box-facing concerns the ADR
adds — a ``/health`` endpoint exposing an ``in_flight`` gauge, instrumentation of the
certified LLM tools that feeds it, and a bounded SIGTERM grace window so a retiring
container lets an in-flight op finish — live here and are wired in from
``build_server``/``main`` via :func:`wire_health` and :func:`run_mcp`.

Design notes:

* The gauge counts ONLY the certified, long-running LLM tools
  (:data:`CERTIFIED_TOOLS`) — ``review_plan``/``verify_completion``/``review_code``/
  ``scan_spec`` — never trivial reads, so the autodeploy retire check
  (panicky-sylphish-foxterrier) waits on billable work rather than idle traffic.
* ``/health`` is a FastMCP ``custom_route`` — a Starlette route on the app OUTSIDE the
  SDK ``RequireAuthMiddleware`` and the DNS-rebinding transport-security guard — so an
  unauthenticated container HEALTHCHECK/probe still gets 200 even when auth is enabled.
* The grace window is a MODULE constant (:data:`DEFAULT_SHUTDOWN_GRACE_SECONDS`), the
  ``review_bot/config.py`` budget precedent, deliberately NOT a rebar config key so it
  does not ripple into ``MCP_ENV_VARS`` / ``server.json`` / the env-var docs generators.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

CERTIFIED_TOOLS = frozenset({"review_plan", "verify_completion", "review_code", "scan_spec"})
"""The certified, long-running LLM tools (``register_llm_tools`` in ``_mcp_llm.py``).
Only these move the in-flight gauge; ``sign_review`` is excluded (it runs no LLM)."""

DEFAULT_SHUTDOWN_GRACE_SECONDS = 1200
"""Upper bound (seconds) a retiring process waits for the gauge to drain before it
exits. compose ``stop_grace_period`` must be >= this so Docker never SIGKILLs mid-op."""

_GAUGE_ATTR = "_rebar_in_flight_gauge"


class InFlightGauge:
    """Thread-safe counter of in-flight certified tool calls.

    A sync MCP tool body does NOT run on a worker thread: the ``mcp`` SDK calls it
    DIRECTLY inside the ASGI request coroutine, so :meth:`track` runs on the event-loop
    thread (bug f643 / ``superior-trifling-dunlin`` — believing otherwise is exactly what
    let that bug ship). The lock is still required: the gauge is READ and acted on from
    other threads — notably the SIGTERM drain path, which polls :attr:`value` while
    in-flight calls mutate it. :meth:`track` only counts a call whose tool name is in
    :data:`CERTIFIED_TOOLS`; any other name is a no-op context so instrumentation can
    be applied uniformly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    def _increment(self) -> None:
        with self._lock:
            self._value += 1

    def _decrement(self) -> None:
        with self._lock:
            self._value -= 1

    @contextlib.contextmanager
    def track(self, tool_name: str) -> Iterator[None]:
        if tool_name not in CERTIFIED_TOOLS:
            yield
            return
        self._increment()
        try:
            yield
        finally:
            self._decrement()


def _wrap_tool_fn(fn: Callable[..., Any], gauge: InFlightGauge, name: str):
    """Wrap a tool's ``fn`` so the gauge tracks the call. Preserves sync/async by
    matching the original; the FastMCP tool manager calls ``fn`` via
    ``call_fn_with_arg_validation`` and honours the replaced attribute."""

    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with gauge.track(name):
            return fn(*args, **kwargs)

    return _sync_wrapper


def instrument_certified_tools(mcp: Any, gauge: InFlightGauge) -> None:
    """Replace each certified tool's ``fn`` with a gauge-tracking wrapper.

    All four certified tools are sync ``def`` functions; if one is missing (a build
    that did not register the LLM tools) it is skipped. An already-async tool is left
    untouched rather than mis-wrapped."""

    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:
        return
    for name in CERTIFIED_TOOLS:
        tool = manager.get_tool(name)
        if tool is None or getattr(tool, "is_async", False):
            continue
        tool.fn = _wrap_tool_fn(tool.fn, gauge, name)


def store_status() -> dict[str, Any]:
    """Whether this server can actually reach a ticket store — ``{path, present, expected}``.

    ``/health`` used to report only ``in_flight``, so a container with NO ticket store at all
    passed both the container HEALTHCHECK and the blue-green readiness gate. That is how a
    deployed server spent weeks answering every tracker query as though the store were merely
    empty (bugs kilted-nuclear-bronco / mobile-groovy-badger) with nothing in the pipeline able
    to notice.

    ``expected`` is the load-bearing field, and it is why this reports rather than fails.
    A missing store is only a FAULT for a deployment that declared it has one; for a deployment
    that never configured a tracker dir it is just a fact about that deployment. Keying the
    readiness gate on ``present AND expected`` means this can ship to a box that currently has
    no store without marking a working container unhealthy, and becomes strict on its own the
    moment a tracker dir is configured — no flag day, no second change.

    Never raises for an ordinary resolution fault: a health probe that can fail is worse than
    one that reports a degraded field, so those are reported as ``present: False`` with the
    error text. The ONE deliberate exception is ``RemovedInputError`` (a ``BaseException``),
    raised when a retired load-bearing input such as ``TICKETS_TRACKER_DIR`` is still set
    (``_config_sources.py:130``); that must fail the server hard rather than be reported as a
    merely-degraded store, and it is re-raised explicitly below.
    """
    from rebar import config as _config
    from rebar._deprecations import RemovedInputError

    expected = False
    path = ""
    try:
        # `expected` is deliberately read from the ENV OVERRIDE alone, not from the parsed
        # config. Two reasons: a health probe must not be able to fail (or block) on config
        # parsing, and `check_config_ownership` reserves `load_config` to approved seams. The
        # deployment surface that matters here sets REBAR_TRACKER_DIR explicitly
        # (infra/compose/docker-compose.yml), so this covers it. A project that instead
        # declares a non-default `tracker.dir` in config reports expected=False and simply
        # does not get the strict readiness gate -- it degrades to the old behaviour rather
        # than misreporting.
        expected = bool(_config.tracker_dir_override())
        path = str(_config.tracker_dir())
    except RemovedInputError:
        # A removed, still-set, load-bearing input must fail hard rather than be reported as
        # a merely-degraded store. RemovedInputError subclasses BaseException precisely so it
        # sails through broad handlers, so this re-raise is redundant TODAY — it is here so
        # the intent survives a future widening of the handler below, which is the same
        # reason the boot sweep carries one.
        raise
    except Exception as exc:  # noqa: BLE001 - see docstring: the probe never raises
        return {"path": path, "present": False, "expected": expected, "error": str(exc)}
    return {"path": path, "present": os.path.isdir(path), "expected": expected}


def run_startup_store_sweep() -> None:
    """Best-effort ensure-sweep at boot, and say so when there is no store.

    Lives here rather than in ``mcp_server`` because it is the same concern as
    :func:`store_status`: what this server can see of its ticket store at startup, and
    whether that is reportable. (Extracting it also keeps ``mcp_server`` under the
    module-size cap, which this function's own logging pushed it over.)

    Converges a store that is behind the idempotent registry. ``run_ensures`` acquires and
    RELEASES its own store write lock internally (a SHORT budget, so a contended lock skips
    rather than delays boot) — it is NOT held across ``build_server().run()``, which runs
    under no lock. Log-and-continue: a missing store, an import error, or a sweep failure
    never aborts boot.
    """
    from rebar._deprecations import RemovedInputError

    try:
        from rebar import config as _config
        from rebar._store import ensures as _ensures

        tracker = str(_config.tracker_dir())
        if os.path.isdir(tracker):
            _ensures.run_ensures(tracker, timeout=5, attempts=1)
        else:
            # Log-and-continue is the right posture (a missing store must not abort boot),
            # but combined with a /health probe that could not see the store it meant a
            # container serving NO tracker was indistinguishable from a healthy one, and
            # nothing in the pipeline ever reported it (bug mobile-groovy-badger). One line
            # naming the path is the difference between a silent misconfiguration and a
            # greppable one.
            logging.getLogger("rebar").warning(
                "startup: no ticket store at %s — tracker tools will report the store as "
                "uninitialized until it is provisioned",
                tracker,
            )
    except RemovedInputError:
        # A removed, still-set, load-bearing input must fail MCP startup hard rather than be
        # swallowed into a silent boot.
        raise
    except Exception:
        logging.getLogger("rebar").debug("startup ensure-sweep skipped", exc_info=True)


def register_health_route(mcp: Any, gauge: InFlightGauge) -> None:
    """Register ``GET /health`` returning ``{"in_flight": <int>, "store": {...}}``.

    Uses FastMCP's ``custom_route`` so the route lives on the Starlette app OUTSIDE the
    auth and transport-security middleware — an unauthenticated probe gets 200.

    The status code stays 200 even with no store: see :func:`store_status` for why the
    signal is a field rather than a failure."""

    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def _health(_request: Any) -> Any:  # pragma: no cover - thin adapter
        return JSONResponse({"in_flight": gauge.value, "store": store_status()})


def wire_health(mcp: Any, gauge: InFlightGauge | None = None) -> InFlightGauge:
    """Instrument the certified tools, register ``/health``, and stash the gauge on the
    server so :func:`run_mcp` can drain it on SIGTERM. Returns the gauge."""

    gauge = gauge or InFlightGauge()
    instrument_certified_tools(mcp, gauge)
    register_health_route(mcp, gauge)
    setattr(mcp, _GAUGE_ATTR, gauge)
    return gauge


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

    On SIGTERM it first tells uvicorn to stop accepting NEW connections
    (``should_exit`` — uvicorn begins its own graceful shutdown, bounded by
    ``timeout_graceful_shutdown``), THEN waits (bounded by ``grace_seconds``) for the
    in-flight certified op to finish before returning so the serving thread can exit.
    Stopping intake first is what keeps a newly-arriving op from extending the drain
    window indefinitely. Extracted as a seam so the handler BODY is directly testable
    without delivering a real signal."""

    def _on_sigterm(_signum: int, _frame: Any) -> None:
        server.should_exit = True
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
    poll_interval: float = 0.5,
    opcert_binding: Any = None,
) -> None:
    """Serve the Streamable-HTTP app with an owned, bounded SIGTERM grace.

    uvicorn only installs its own signal handlers on the main thread, so we run the
    server on a background thread (it therefore installs none) and own the SIGTERM
    handler on the main thread (see :func:`make_sigterm_handler`).
    ``timeout_graceful_shutdown`` bounds uvicorn's own drain as a backstop. The main
    thread joins with a timeout so it stays responsive to the signal (a bare
    ``join()`` would defer handler delivery).

    ``opcert_binding`` (the box's startup op-cert signer, or ``None``) is bound
    context-locally INSIDE the serving thread's target, not in the caller's thread:
    a :class:`contextvars.ContextVar` set on the main thread is NOT inherited by the
    background thread, so the binding must be entered where the request-handling event
    loop actually runs. ``None`` is a transparent no-op (the unprovisioned path)."""

    import signal

    import uvicorn

    from rebar._opcert_binding import bound_signer

    app = mcp.streamable_http_app()
    config = uvicorn.Config(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
        timeout_graceful_shutdown=int(grace_seconds),
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
