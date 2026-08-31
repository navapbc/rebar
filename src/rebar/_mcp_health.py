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
  accepts a connection (:func:`install_startup_handshake`, bug
  vaccinated-flavorous-solenodon).
* The grace window is a MODULE constant (:data:`DEFAULT_SHUTDOWN_GRACE_SECONDS`), the
  ``review_bot/config.py`` budget precedent, deliberately NOT a rebar config key so it
  does not ripple into ``MCP_ENV_VARS`` / ``server.json`` / the env-var docs generators.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from rebar._mcp_opcert_health import (
    _OPCERT_STATUS_ATTR,
)

CERTIFIED_TOOLS = frozenset({"review_plan", "verify_completion", "review_code", "scan_spec"})
"""The certified, long-running LLM tools (``register_llm_tools`` in ``_mcp_llm.py``).
Only these move the in-flight gauge; ``sign_review`` is excluded (it runs no LLM)."""

DEFAULT_SHUTDOWN_GRACE_SECONDS = 1200
"""Upper bound (seconds) a retiring process waits for the gauge to drain before it
exits. compose ``stop_grace_period`` must be >= this so Docker never SIGKILLs mid-op."""

DEFAULT_UVICORN_GRACEFUL_SECONDS = 10
"""Short backstop (seconds) for uvicorn's OWN ``timeout_graceful_shutdown``, deliberately
DECOUPLED from :data:`DEFAULT_SHUTDOWN_GRACE_SECONDS` (bug 2f46). uvicorn's graceful
shutdown waits this long for still-open connections after :attr:`should_exit` is set.
Binding it to the 1200s certified-op grace made a retiring Streamable-HTTP container wait
the FULL 1200s for idle held-open client streams even at 0 in-flight ops — pinning a
blue-green port ~20 min and exhausting the two-port pool (``mcp_retire_cap`` /
``deploy_errors``). The certified-op drain is enforced by the in-flight gauge poll (which
runs BEFORE ``should_exit`` is set), never by this timeout, so keeping it short only sweeps
IDLE held-open streams fast and never truncates a real in-flight op."""

_GAUGE_ATTR = "_rebar_in_flight_gauge"

DEFAULT_HANDSHAKE_BUDGET_SECONDS = 10.0
"""Upper bound (seconds) on the startup MCP handshake. A module constant for the same
reason as :data:`DEFAULT_SHUTDOWN_GRACE_SECONDS` — a config key would ripple into
``MCP_ENV_VARS`` / ``server.json`` / the env-var docs generators."""

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
    """The Host real traffic carries, from the deployment's DECLARED public resource URL —
    ``mcp.auth_resource_server_url`` / ``REBAR_MCP_AUTH_RESOURCE_SERVER_URL``, which the box sets
    to ``https://rebar.solutions.navateam.com/mcp``. The port is appended only when it is not the
    scheme default, matching the Host header a proxy actually sends. ``""`` when unset or
    unparseable."""

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


def select_probe_host(
    security: Any,
    *,
    resource_server_url: Any = None,
    bind_host: str = "",
    bind_port: int = 0,
) -> str:
    """The Host header the startup handshake presents to this server's own guard.

    Precedence, all from this server's OWN settings:

    1. the host of the DECLARED public resource URL (:func:`_declared_public_host`). This is
       declared INDEPENDENTLY of the allowlist, which is what makes the probe non-vacuous: a
       container whose ``REBAR_MCP_HTTP_ALLOWED_HOSTS`` admits some OTHER hostname answers
       ``/health`` 200 while 421-ing every real request, and this is the case that catches it;
    2. else the first ``allowed_hosts`` entry that is a LITERAL host (no ``*``);
    3. else the first ``host:*`` port-wildcard entry as ``host:<bind port>`` — the only wildcard
       form ``TransportSecurityMiddleware._validate_host`` matches;
    4. else the bind address, ``0.0.0.0``/``::`` normalized to ``127.0.0.1``. An allowlist that
       admits nothing usable — empty, or only unmatchable forms such as ``*.example.com`` — lands
       here and is refused by this server's own guard.

    LIMIT, stated rather than hidden: with no declared public resource URL there is nothing
    independent to check the allowlist against, so on such a deployment a pass proves the request
    path works, not that the right hosts are admitted."""

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
    if host in ("", "0.0.0.0", "::", "[::]"):  # a wildcard bind is probed over loopback
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
    """Drive one real ``initialize`` through ``session_manager`` and return its HTTP status.

    Calls ``handle_request`` — the SAME entry point the ``/mcp`` route serves through
    (``StreamableHTTPASGIApp(self._session_manager)`` in the SDK's ``streamable_http_app``)
    — so a 200 cannot be produced unless the DNS-rebinding transport-security guard
    admitted the Host, the transport parsed the body, the session manager started a server
    task, and the MCP server answered the JSON-RPC request. That is what makes a later
    ``/health`` 200 mean "this container has already served an MCP request".

    ``receive`` yields the body ONCE and then ``http.disconnect``: without the disconnect
    the SSE response stream never terminates and the drive hangs (observed)."""

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
    """Run ``drive`` under a bound and REPORT its outcome — never raise, never hang.

    Returns ``{ok, status, host, elapsed_ms, error}``. A non-200 status, a drive that
    raises, and a drive that exceeds ``budget_seconds`` are all reported as ``ok: False``
    so boot continues and uvicorn still serves; a health probe that can abort startup
    would be worse than one that reports a degraded field (same posture as
    :func:`store_status`).

    The timeout scope and the clock are INJECTED — the seam :func:`drain_then_exit`
    already uses — so the bound is provable in a unit test with a fake clock instead of
    by sleeping."""

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
        # A removed, still-set, load-bearing input must fail the server HARD rather than be
        # folded into this record's error field. RemovedInputError subclasses BaseException so
        # it already sails past the handler below; the re-raise is redundant today and exists
        # so the intent survives a future widening of that handler.
        raise
    except Exception as exc:  # noqa: BLE001 - see docstring: the handshake never raises
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
    """The Starlette app inside ``app``, unwrapping ASGI middleware (``ProxyAuthMiddleware``
    wraps the Streamable-HTTP app when the proxy auth strategy is active). ``None`` when
    there is none — a fake/stub server in a test, which must not break boot."""

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
    """Wrap ``app``'s ASGI lifespan so the handshake runs BEFORE uvicorn serves.

    Starlette's ``Router.lifespan`` sends ``lifespan.startup.complete`` only after the
    lifespan context has been entered, and uvicorn accepts no connection until it receives
    that message — so recording the handshake inside the wrapper, after the session
    manager's own lifespan is running and before the ``yield``, is what makes a later
    ``/health`` 200 prove that this container has already served an MCP request. A
    background task or a lazily-evaluated ``/health`` field would not.

    Best-effort by construction: any failure to install is logged and boot continues."""

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
        # Same contract: a retired, still-set input must abort boot loudly rather than be
        # swallowed by the never-break-boot handler below. Redundant today (BaseException
        # subclass); kept so a future widening cannot silently reintroduce the swallow.
        raise
    except Exception:  # installation never breaks boot
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

__all__ = ["drain_then_exit", "make_sigterm_handler", "run_http_with_grace", "run_mcp"]
