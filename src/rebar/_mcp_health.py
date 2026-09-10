"""MCP health, certified-op instrumentation, and graceful shutdown (ADR 0104).

The in-flight gauge counts only billable certified tools so SIGTERM retirement can
drain them. Unauthenticated ``/health`` remains outside auth/transport middleware and
therefore also reports a real startup ``initialize`` handshake; HTTP 200 alone is not
request-path proof. Shutdown grace is a module constant rather than config surface.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import threading
from collections.abc import Callable, Iterator
from typing import Any

from rebar._mcp_opcert_health import (
    _OPCERT_STATUS_ATTR,
)
from rebar._mcp_startup_handshake import (
    _HANDSHAKE_ATTR,
    DEFAULT_HANDSHAKE_BUDGET_SECONDS,
    drive_initialize,
    handshake_status,
    install_startup_handshake,
    run_startup_handshake,
    select_probe_host,
)

CERTIFIED_TOOLS = frozenset({"review_plan", "verify_completion", "review_code", "scan_spec"})
"""The certified, long-running LLM tools (``register_llm_tools`` in ``_mcp_llm.py``).
Only these move the in-flight gauge; ``sign_review`` is excluded (it runs no LLM)."""

DEFAULT_SHUTDOWN_GRACE_SECONDS = 1200
"""Upper bound (seconds) a retiring process waits for the gauge to drain before it
exits. compose ``stop_grace_period`` must be >= this so Docker never SIGKILLs mid-op."""

DEFAULT_UVICORN_BACKSTOP_SECONDS = 30
"""Short backstop (seconds) for uvicorn's OWN ``timeout_graceful_shutdown``, deliberately
DECOUPLED from :data:`DEFAULT_SHUTDOWN_GRACE_SECONDS` (bug 2f46). uvicorn's graceful
shutdown waits this long for still-open connections after :attr:`should_exit` is set.
Binding it to the 1200s certified-op grace made a retiring Streamable-HTTP container wait
the FULL 1200s for idle held-open client streams even at 0 in-flight ops — pinning a
blue-green port ~20 min and exhausting the two-port pool (``mcp_retire_cap`` /
``deploy_errors``). The certified-op drain is enforced by the in-flight gauge poll (which
runs BEFORE ``should_exit`` is set), never by this timeout, so keeping it short only sweeps
IDLE held-open streams fast and never truncates a real in-flight op."""

DEFAULT_UVICORN_GRACEFUL_SECONDS = DEFAULT_UVICORN_BACKSTOP_SECONDS
"""Backward-compatible alias for :data:`DEFAULT_UVICORN_BACKSTOP_SECONDS`."""

_GAUGE_ATTR = "_rebar_in_flight_gauge"


class MCPRetiringError(RuntimeError):
    """Raised when a NEW certified tool call arrives on a container that has already begun
    draining for retirement (SIGTERM received). The client should retry against the live
    (green) container. Refusing new intake — rather than counting it — is what lets the
    in-flight gauge actually reach 0 during the drain window: without it a landing burst
    could keep the gauge >0 and re-pin the retiring blue-green port for the full grace,
    re-creating the very port-exhaustion bug 2f46 fixes."""


class InFlightGauge:
    """Thread-safe count and retirement gate for certified tool calls.

    Tool bodies run on workers while the SIGTERM path reads the count elsewhere, so a
    lock protects all state. Non-certified names are no-ops. Once draining begins, new
    certified calls raise :class:`MCPRetiringError`; already-counted calls finish.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0
        self._draining = False

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    @property
    def draining(self) -> bool:
        with self._lock:
            return self._draining

    def begin_draining(self) -> None:
        """Close the gauge to NEW certified intake (idempotent). Ops already counted keep
        running; a subsequent :meth:`track` of a certified tool raises
        :class:`MCPRetiringError`. Called first thing on SIGTERM so the drain can complete."""
        with self._lock:
            self._draining = True

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
        with self._lock:
            if self._draining:
                raise MCPRetiringError(
                    f"certified tool {tool_name!r} refused: this MCP container is retiring "
                    "(draining for shutdown) — retry against the live container"
                )
            self._value += 1
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

    All four certified tools are sync ``def``; a missing one (a build that did not
    register the LLM tools) is skipped. An already-async CERTIFIED tool here is
    FAIL-LOUD, not a silent skip (ticket ``wounded-resident-bushbaby``):
    :func:`wire_health` instruments FIRST and offloads SECOND so the bodies are still
    sync here, and a reversed order — or a certified tool made ``async def`` — would make
    the SYNC gauge wrapper a no-op on it, so the gauge stops counting billable work and
    the SIGTERM drain fails OPEN with no signal. Raising surfaces that in a test."""

    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:
        return
    for name in CERTIFIED_TOOLS:
        tool = manager.get_tool(name)
        if tool is None:
            continue
        if getattr(tool, "is_async", False):
            raise RuntimeError(
                f"certified tool {name!r} is already async at instrument time; the "
                "in-flight gauge only wraps SYNC bodies, so the SIGTERM drain would go "
                "blind to this billable op. wire_health must instrument BEFORE it "
                "offloads; make a certified tool async only with async gauge instrumentation."
            )
        tool.fn = _wrap_tool_fn(tool.fn, gauge, name)


# FastMCP otherwise runs sync tools on the ASGI loop, making health and initialize wait
# behind multi-minute calls. AnyIO workers preserve signer ContextVars and apply their
# bounded thread limiter while leaving the event loop responsive.


def _thread_offloaded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """An async wrapper that runs ``fn`` on a worker thread, preserving its signature.

    ``functools.wraps`` matters: the SDK already built this tool's ``fn_metadata`` (and
    therefore its argument model and output schema) from the original callable at
    registration time, and ``Tool.run`` passes arguments by KEYWORD, so the wrapper must
    stay transparent to introspection and accept whatever the original accepted.

    ``abandon_on_cancel`` is also load-bearing: a client-abandoned request must release
    its worker-limiter token so later MCP calls are not queued behind work whose caller is
    already gone. The sync body still runs to completion in the background, and its result
    or exception is intentionally discarded by AnyIO because there is no caller left to
    receive it."""

    @functools.wraps(fn)
    async def _offloaded(*args: Any, **kwargs: Any) -> Any:
        import anyio.to_thread

        return await anyio.to_thread.run_sync(
            functools.partial(fn, *args, **kwargs),
            abandon_on_cancel=True,
        )

    return _offloaded


def offload_sync_tools(mcp: Any) -> int:
    """Move every synchronous tool to a worker and return the count.

    Ordinary reads can block too, so this covers all tools. It must run after certified
    instrumentation, whose synchronous wrapper fails loud if ordering is reversed; the
    gauge lock makes worker-thread increments safe.
    """

    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:
        return 0
    moved = 0
    for tool in manager.list_tools():
        # An already-async tool is not a problem and must not be double-wrapped.
        if getattr(tool, "is_async", False):
            continue
        tool.fn = _thread_offloaded(tool.fn)
        tool.is_async = True
        moved += 1
    return moved


def store_status() -> dict[str, Any]:
    """Report ``{path, present, expected}`` for this server's ticket store.

    Missing storage is a readiness fault only when an override declares it expected.
    Ordinary resolution failures become degraded data with error text so probes remain
    reportable; :class:`RemovedInputError` alone fails the server hard.
    """
    from rebar import config as _config
    from rebar._deprecations import RemovedInputError

    expected = False
    path = ""
    try:
        # Read expectedness from the deployment env override so health never parses or
        # blocks on config; config-only tracker paths retain non-strict readiness.
        expected = bool(_config.tracker_dir_override())
        path = str(_config.tracker_dir())
    except RemovedInputError:
        # Removed load-bearing inputs must fail hard, even if the handler later widens.
        raise
    except Exception as exc:  # noqa: BLE001 - see docstring: the probe never raises
        return {"path": path, "present": False, "expected": expected, "error": str(exc)}
    return {"path": path, "present": os.path.isdir(path), "expected": expected}


def run_startup_store_sweep() -> None:
    """Run the idempotent ensure registry at boot, best-effort.

    The sweep owns a short-lived lock and skips contention rather than delaying service;
    no lock survives into serving. Missing stores, imports, and sweep failures log and
    continue, while removed load-bearing inputs still abort startup.
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
    """Register ``GET /health`` returning ``{"in_flight", "store", "handshake", "opcert"}``.

    Uses FastMCP's ``custom_route`` so the route lives on the Starlette app OUTSIDE the
    auth and transport-security middleware — an unauthenticated probe gets 200.

    The status code stays 200 even with a degraded store or op-cert signer: see
    :func:`store_status` / :func:`opcert_signing_status` for why the signal is a field
    rather than a failure."""

    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def _health(_request: Any) -> Any:  # pragma: no cover - thin adapter
        return JSONResponse(
            {
                "in_flight": gauge.value,
                "store": store_status(),
                "handshake": handshake_status(mcp),
                "opcert": getattr(mcp, _OPCERT_STATUS_ATTR, {"bound": False, "expected": False}),
            }
        )


def wire_health(mcp: Any, gauge: InFlightGauge | None = None) -> InFlightGauge:
    """Instrument the certified tools, move sync tool bodies off the event loop, register
    ``/health``, and stash the gauge on the server so :func:`run_mcp` can drain it on
    SIGTERM. Returns the gauge.

    ORDER IS LOAD-BEARING. :func:`instrument_certified_tools` installs a SYNC wrapper and
    FAILS LOUD on a certified tool already marked async, so offloading first would leave
    the certified tools uninstrumented and the SIGTERM drain blind to in-flight work."""

    gauge = gauge or InFlightGauge()
    instrument_certified_tools(mcp, gauge)
    offload_sync_tools(mcp)
    register_health_route(mcp, gauge)
    setattr(mcp, _GAUGE_ATTR, gauge)
    return gauge


# Re-export the serving runtime for compatibility. Importing last lets it depend on the
# gauge and handshake primitives above without a cycle.
from rebar._mcp_serving import (  # noqa: E402
    drain_then_exit,
    make_sigterm_handler,
    run_http_with_grace,
    run_mcp,
)

__all__ = [
    "DEFAULT_HANDSHAKE_BUDGET_SECONDS",
    "_HANDSHAKE_ATTR",
    "drain_then_exit",
    "drive_initialize",
    "handshake_status",
    "install_startup_handshake",
    "make_sigterm_handler",
    "run_http_with_grace",
    "run_mcp",
    "run_startup_handshake",
    "select_probe_host",
]
