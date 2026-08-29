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
import functools
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
    and skips tools already marked async — reversing the order would leave the certified
    tools uninstrumented. Composing this way also means the gauge is incremented on the
    worker thread rather than the event-loop thread; that is safe, and is exactly what
    :class:`InFlightGauge`'s lock has always been for."""

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


_OPCERT_STATUS_ATTR = "_rebar_opcert_status"


def opcert_signing_status(binding: Any, repo_root: str | None = None) -> dict[str, Any]:
    """Whether the box's bound startup op-cert signer's public key matches the pinned
    trusted-environment key for its principal — ``{bound, expected, env_id?, matched?}``.

    This is the ONE same-environment failure the derived-key verify path cannot catch (bug 879b):
    a private key that signs valid op-certs but whose PUBLIC half is not the published/pinned key
    that *required-environment* verify checks against. Everything else about a signable key is
    already validated at composition (``compose_startup_opcert_binding``) or re-derivable from the
    private key on demand, so a "can we derive a pub?" probe would be redundant — the pinned-key
    match is the only non-redundant signal.

    ``expected`` gates strictness exactly like :func:`store_status`: it is True only when this
    deployment OPTED INTO pinning by shipping ``.rebar/trusted_environments.yaml``. Required-
    environment binding is advisory/deferred today (ADR 0104 decision 3), so a deployment that has
    not configured pinning is never marked degraded. NEVER raises: a malformed config or any
    resolution fault is reported as ``expected: False`` rather than aborting the probe."""
    from rebar._deprecations import RemovedInputError
    from rebar._opcert_signing import _read_opcert_pub
    from rebar.attest.trusted_env import load_trusted_environments, trusted_env_keyring

    if binding is None:
        return {"bound": False, "expected": False}
    env_id = getattr(binding, "principal", None)
    key_path = getattr(binding, "key_path", None)
    status: dict[str, Any] = {"bound": True, "expected": False, "env_id": env_id}
    try:
        status["expected"] = load_trusted_environments(repo_root) is not None
        if not status["expected"]:
            return status
        signer_pub = _read_opcert_pub(key_path) if key_path else None
        keyring = trusted_env_keyring(env_id, repo_root) if env_id else None
        pinned = [
            k.get("public_key") for k in (keyring or []) if k.get("revoked_at_log_position") is None
        ]
        status["matched"] = signer_pub is not None and signer_pub in pinned
    except RemovedInputError:
        # A removed, still-set, load-bearing input must fail hard rather than be reported as a
        # merely-degraded signer (mirrors store_status / run_startup_store_sweep).
        raise
    except Exception as exc:  # noqa: BLE001 - the probe never raises (see docstring)
        status["expected"] = False
        status["error"] = str(exc)
    return status


def opcert_signer_degraded(status: dict[str, Any]) -> bool:
    """True only when a deployment that opted into pinning has a bound signer whose public key is
    NOT the pinned trusted-environment key. Non-blocking: a degraded signer still serves — those
    are valid signatures that only fail the advisory environment-binding check (ADR 0104 dec. 3)."""
    return bool(status.get("bound") and status.get("expected") and not status.get("matched"))


def run_startup_opcert_check(binding: Any, repo_root: str | None = None) -> None:
    """Boot-time SERVE-DEGRADED surface for bug 879b: log a WARNING (naming the principal) when
    the bound startup signer's public key is not the pinned trusted-environment key. NEVER raises
    and NEVER aborts boot — the op-cert-signer sibling of :func:`run_startup_store_sweep`."""
    from rebar._deprecations import RemovedInputError

    try:
        status = opcert_signing_status(binding, repo_root)
        if opcert_signer_degraded(status):
            logging.getLogger("rebar").warning(
                "startup: bound op-cert signer's public key is NOT the pinned trusted-environment "
                "key for principal %s — its op-certs are valid signatures but FAIL a required-"
                "environment (pinned-key) verify. Serving DEGRADED (this binding check is advisory "
                "today; boot continues). See bug 879b-9bf0-86fd-4a6b.",
                status.get("env_id"),
            )
    except RemovedInputError:
        # A removed, still-set, load-bearing input must fail MCP startup hard rather than be
        # swallowed into a silent boot (mirrors run_startup_store_sweep).
        raise
    except Exception:  # a health check must never abort boot
        logging.getLogger("rebar").debug("startup op-cert check skipped", exc_info=True)


def register_health_route(mcp: Any, gauge: InFlightGauge) -> None:
    """Register ``GET /health`` returning ``{"in_flight", "store", "opcert"}``.

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
                "opcert": getattr(mcp, _OPCERT_STATUS_ATTR, {"bound": False, "expected": False}),
            }
        )


def wire_health(mcp: Any, gauge: InFlightGauge | None = None) -> InFlightGauge:
    """Instrument the certified tools, move sync tool bodies off the event loop, register
    ``/health``, and stash the gauge on the server so :func:`run_mcp` can drain it on
    SIGTERM. Returns the gauge.

    ORDER IS LOAD-BEARING. :func:`instrument_certified_tools` installs a SYNC wrapper and
    skips tools already marked async, so offloading first would leave the certified tools
    uninstrumented and the SIGTERM drain blind to in-flight billable work."""

    gauge = gauge or InFlightGauge()
    instrument_certified_tools(mcp, gauge)
    offload_sync_tools(mcp)
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
