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
  That exemption is also why a 200 alone proves nothing about the MCP request path, so
  ``/health`` additionally reports the STARTUP HANDSHAKE: one real ``initialize`` driven
  through this server's own session manager inside the ASGI lifespan, before uvicorn
  accepts a connection. That handshake cluster now lives in
  :mod:`rebar._mcp_startup_handshake` and is re-exported here (:func:`install_startup_handshake`,
  bug vaccinated-flavorous-solenodon).
* The grace window is a MODULE constant (:data:`DEFAULT_SHUTDOWN_GRACE_SECONDS`), the
  ``review_bot/config.py`` budget precedent, deliberately NOT a rebar config key so it
  does not ripple into ``MCP_ENV_VARS`` / ``server.json`` / the env-var docs generators.
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
    """Thread-safe counter of in-flight certified tool calls.

    The ``mcp`` SDK calls a sync tool body DIRECTLY inside the ASGI request coroutine, so
    :meth:`track` USED to run on the event-loop thread (bug f643 /
    ``superior-trifling-dunlin`` — believing otherwise is exactly what let that bug ship).
    Since :func:`offload_sync_tools` (bug ``dewy-rotatable-tarsier``) it runs on an anyio
    worker thread instead, because leaving those bodies on the loop stopped the server
    answering every other request for the length of the call. Either way the lock is what
    makes this safe, and it was always required: the gauge is READ and acted on from other
    threads — notably the SIGTERM drain path, which polls :attr:`value` while in-flight
    calls mutate it. :meth:`track` only counts a call whose tool name is in
    :data:`CERTIFIED_TOOLS`; any other name is a no-op context so instrumentation can
    be applied uniformly.

    Once :meth:`begin_draining` is called (on SIGTERM, bug 2f46) the gauge is CLOSED to NEW
    certified intake: :meth:`track` raises :class:`MCPRetiringError` for a certified tool
    instead of counting it, so a burst arriving mid-drain cannot push the gauge back above 0
    and re-pin the retiring port. Calls already in flight are unaffected and drain normally.
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


# ── Keep long tool bodies OFF the event loop ─────────────────────────────────
# THE DEFECT THIS FIXES (bug dewy-rotatable-tarsier). Every rebar MCP tool is a plain
# ``def``, and the SDK calls a sync tool body DIRECTLY inside the ASGI request coroutine
# (``fastmcp/utilities/func_metadata.py``: ``if fn_is_async: await fn(...) else: fn(...)``).
# On the stdio transport that was harmless — one client, one process. Behind the shared
# HTTP transport it is not: a tool call occupies the uvicorn event loop for its whole
# duration, so the server answers NOTHING else meanwhile — not ``initialize``, not
# ``tools/list``, not even the unauthenticated ``/health`` route.
#
# MEASURED on the deployed box: a ``CallToolRequest`` began at 21:03:00.590 and the process
# logged nothing at all for 3m56s; at 21:06:56.275 six ``/health`` responses and five 401s
# completed within 5 MILLISECONDS of each other — a backlog draining the instant the loop was
# released. From outside, that window is `http=000` after 70s and 401s taking 12s / 28s / 50s
# / 63s, against a 0.25s steady state. rebar's own budgets make this routine rather than
# exotic: AGENTS.md documents ``review_plan`` at 15-20 MINUTES and a completion-verifier close
# at 9-11 minutes, and one unfiltered ``list_tickets`` has been measured at 177 seconds.
#
# It reads as a STARTUP bug because a redeploy makes every agent reconnect at once, so the
# first client to ``initialize`` after a cutover is the one most likely to land behind another
# client's long tool call. The handshake itself is not slow: a cold authenticated
# ``initialize`` fired at the cutover instant measures 30 ms.
#
# THE FIX is the one this codebase already applied to the sibling service — ticket c2ba
# (``melting-resting-serpent``) moved the review-bot's ``emit_code_review_artifact`` off the
# event loop with ``asyncio.to_thread`` "so the drain bound can actually fire"
# (infra/compose/docker-compose.yml). Same defect, same remedy: run each sync tool body on a
# worker thread and mark the tool async so the SDK awaits it.
#
# ``anyio.to_thread.run_sync`` is used rather than ``asyncio.to_thread`` because the SDK's
# transport runs under anyio, and because it COPIES THE CALLER'S CONTEXTVARS into the worker
# thread (verified). That is load-bearing here and not incidental: ``run_http_with_grace``
# binds the box's op-cert signer as a ContextVar inside the serving thread, and the certified
# tools mint op-certs from it — a thread that did not inherit that context would sign under
# the wrong environment. It also carries anyio's default 40-slot thread limiter, which bounds
# how many tool bodies can run at once instead of letting an unbounded fan-out spawn threads.


def _thread_offloaded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """An async wrapper that runs ``fn`` on a worker thread, preserving its signature.

    ``functools.wraps`` matters: the SDK already built this tool's ``fn_metadata`` (and
    therefore its argument model and output schema) from the original callable at
    registration time, and ``Tool.run`` passes arguments by KEYWORD, so the wrapper must
    stay transparent to introspection and accept whatever the original accepted."""

    @functools.wraps(fn)
    async def _offloaded(*args: Any, **kwargs: Any) -> Any:
        import anyio.to_thread

        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return _offloaded


def offload_sync_tools(mcp: Any) -> int:
    """Run every SYNC tool body on a worker thread; return how many were moved.

    Applied to every tool, not just :data:`CERTIFIED_TOOLS`: the gauge only needs to see
    billable work, but the event loop is blocked by ANY slow body, and the 177-second
    ``list_tickets`` that motivated this is an ordinary read.

    MUST run AFTER :func:`instrument_certified_tools`, which installs a SYNC gauge wrapper
    and FAILS LOUD on a certified tool already marked async — reversing the order would
    leave the certified tools uninstrumented (or trip that guard). Composing this way also
    means the gauge is incremented on the worker thread rather than the event-loop thread;
    that is safe, and is exactly what :class:`InFlightGauge`'s lock has always been for."""

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


# The serving/shutdown runtime (run_mcp -> run_http_with_grace -> make_sigterm_handler ->
# drain_then_exit) lives in _mcp_serving to keep this module under the 800-LOC cap. Re-export
# it so `from rebar._mcp_health import run_mcp` (and the tests' monkeypatch paths) keep working.
# Imported at the END so _mcp_serving can import the gauge/handshake primitives defined above.
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
