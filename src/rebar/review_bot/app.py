"""FastAPI ASGI app — the rebar review-bot webhook receiver (epic d251 / S4b).

WHAT THIS IS (ADR-0007). A thin HTTP service that Gerrit posts review events to.
It imports the rebar review kernel **as a library** rather than re-exposing the
stdio MCP server (`rebar-mcp`) over HTTP: Gerrit emits a plain webhook (a JSON
POST), not MCP JSON-RPC, so an MCP HTTP transport would reject the body. nginx
routes ``/review/`` to this app (stripping the prefix), so externally the receiver
lives at ``https://<host>/review/`` and these routes are reached as ``/health`` and
``/webhook`` after the prefix strip.

S4b BEHAVIOR (the proven pipe). ``POST /webhook`` (1) validates the inbound
``?token=`` secret (ADR-0014 — the ``webhooks`` plugin has no HMAC, so the URL token
+ network ACL are the inbound auth), (2) parses the JSON body, and (3) **ACKs fast**:
it enqueues the event and returns 202 immediately. A background worker consumes the
queue and runs ``voter.review_and_vote`` (clone → review → cast the ``LLM-Review``
vote). An LLM review takes 30s–minutes and would blow Gerrit's ~5s webhook socket
timeout if processed inline (→ timeout + re-delivery). On startup the lifespan also
launches the ``reconcile_loop`` backfill poller.

IMPORTABILITY CONTRACT. ``fastapi`` is imported at module top here on purpose — it
is fine that ``import rebar.review_bot.app`` requires the ``reviewbot`` extra. What
must stay true is that ``import rebar`` (and ``import rebar.review_bot``) does NOT
pull fastapi; that holds because nothing in the core package imports this module.

RUN. ``uvicorn rebar.review_bot.app:app --host 0.0.0.0 --port 8000`` (the
docker-compose / Dockerfile entrypoint). The ``REVIEW_BOT_PORT`` env var (default
8000) is read only by the ``__main__`` convenience runner below; under uvicorn the
port is passed on the uvicorn command line / by compose.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from rebar.review_bot import reconcile as _reconcile
from rebar.review_bot import voter as _voter
from rebar.review_bot.config import ReceiverConfig

logger = logging.getLogger("rebar.review_bot")

#: Default listen port when run via the ``__main__`` convenience runner. The
#: deployment single-sources this from the ``.env`` (``REVIEW_BOT_PORT``) and passes
#: it through docker-compose, so this default only matters for a bare local run.
DEFAULT_PORT = 8000

#: Number of background workers draining the review queue. One is enough for a
#: single-box PoC (reviews serialize on the per-(change,rev) lock anyway).
WORKER_COUNT = 1

#: Wall-clock cap (seconds) on a SINGLE review before the worker abandons it and moves
#: to the next event. The single background worker drains the queue serially, so a review
#: that hangs indefinitely (a clone/subprocess/LLM call blocked forever — as happened when
#: the root disk filled mid-clone, incident 2731) would otherwise wedge the worker on one
#: ``await`` and every subsequent change would silently back up behind it. The per-event
#: try/except only guards against EXCEPTIONS, not a HANG — hence this bounded timeout.
#: Reviews take seconds–minutes, so the default is deliberately generous (20 min); override
#: with the ``REVIEW_TIMEOUT_SECONDS`` env var.
DEFAULT_REVIEW_TIMEOUT_SECONDS = 1200


def _review_timeout_seconds() -> float:
    """Per-review wall-clock timeout from ``REVIEW_TIMEOUT_SECONDS`` (default
    ``DEFAULT_REVIEW_TIMEOUT_SECONDS``). A missing / unparseable / non-positive value falls
    back to the default (a 0 or negative timeout would abandon every review immediately)."""
    raw = os.environ.get("REVIEW_TIMEOUT_SECONDS")
    if not raw:
        return float(DEFAULT_REVIEW_TIMEOUT_SECONDS)
    try:
        val = float(raw.strip())
    except ValueError:
        logger.warning(
            "REVIEW_TIMEOUT_SECONDS=%r is not a number; using %d",
            raw,
            DEFAULT_REVIEW_TIMEOUT_SECONDS,
        )
        return float(DEFAULT_REVIEW_TIMEOUT_SECONDS)
    if val <= 0:
        logger.warning(
            "REVIEW_TIMEOUT_SECONDS=%r is not positive; using %d",
            raw,
            DEFAULT_REVIEW_TIMEOUT_SECONDS,
        )
        return float(DEFAULT_REVIEW_TIMEOUT_SECONDS)
    return val


def _config() -> ReceiverConfig:
    """Process-wide receiver config (env/SSM-sourced). Resolved fresh per app build so
    a reload picks up rotated secrets."""
    return ReceiverConfig.from_env()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the queue, the background review worker(s), the backfill reconciler, and
    the snapshot-cache janitor; stop them cleanly on shutdown."""
    cfg = app.state.config
    app.state.queue = asyncio.Queue()
    tasks: list[asyncio.Task] = []
    for _ in range(WORKER_COUNT):
        tasks.append(asyncio.create_task(_worker(app.state.queue, cfg)))
    tasks.append(asyncio.create_task(_reconcile.reconcile_loop(config=cfg)))
    app.state.tasks = tasks
    # Snapshot-cache janitor (incident 2731 / bug e7f4): every review clones into the
    # content-addressed snapshot store on the ROOT disk, and without the janitor that
    # store grows unboundedly (694M observed) — the reclamation code existed but no
    # production process ever started it. Daemon thread, off the hot path; must never
    # block or fail startup (the gate matters more than the janitor).
    janitor_stop = None
    try:
        from rebar._snapshot import start_background_janitor

        _janitor_thread, janitor_stop = start_background_janitor()
    except Exception:  # noqa: BLE001 — a janitor failure must not take down the receiver
        logger.exception("review-bot: snapshot janitor failed to start (non-fatal)")
    try:
        yield
    finally:
        if janitor_stop is not None:
            janitor_stop.set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _worker(queue: asyncio.Queue, cfg: ReceiverConfig) -> None:
    """Drain the review queue, running one review→vote per event under a bounded timeout.

    A per-event FAILURE is logged and a per-event HANG (a review exceeding
    ``REVIEW_TIMEOUT_SECONDS`` — a blocked clone/subprocess/LLM call) is timed out,
    abandoned, and recorded as a countable ``VOTER_ERROR`` timeout marker; either way the
    worker continues to the next event. It must never die AND never stall on one event and
    starve the queue (bug 9d7c — the single worker silently backing up behind a hung review
    when the disk filled mid-clone)."""
    review_timeout = _review_timeout_seconds()
    while True:
        event = await queue.get()
        try:
            # A manual /rerun enqueues the event with a _rebar_force marker so the
            # voter bypasses the dedup + existing-vote short-circuits and re-reviews.
            force = bool(event.pop("_rebar_force", False)) if isinstance(event, dict) else False
            await asyncio.wait_for(
                _voter.review_and_vote(event, config=cfg, force=force),
                timeout=review_timeout,
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            # A hung review — wait_for has already cancelled the inner coroutine. Abandon it
            # (fail-closed: the change simply goes un-voted this pass and is picked up by the
            # backfill reconciler) and keep draining. Emit the greppable VOTER_ERROR marker so
            # the timeout is counted on rebar/host:voter_errors like any other fail-closed event.
            change = event.get("change") if isinstance(event, dict) else None
            change_id = change.get("id") if isinstance(change, dict) else None
            _voter._voter_error(
                change_id=change_id,
                error=(
                    f"review timed out after {review_timeout}s — abandoned to keep the queue "
                    "draining (hung clone/subprocess/LLM); the backfill reconciler will retry"
                ),
            )
            logger.error(
                "review-bot worker: review timed out after %ss — abandoning change %s and "
                "continuing (REVIEW_TIMEOUT_SECONDS)",
                review_timeout,
                change_id,
            )
        except Exception:  # noqa: BLE001 — a single bad event must not kill the worker
            logger.exception("review-bot worker: review_and_vote raised")
        finally:
            queue.task_done()


app = FastAPI(
    title="rebar review-bot",
    summary="Gerrit webhook receiver — reviews a patchset and casts the LLM-Review vote.",
    lifespan=lifespan,
)
app.state.config = _config()


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Returns 200 with ``{"status": "ok"}`` (no kernel call)."""
    return {"status": "ok"}


@app.post("/webhook", status_code=202)
async def webhook(request: Request) -> JSONResponse:
    """Authenticate, ACK fast (202), and enqueue the event for background review.

    Auth (ADR-0014): the ``?token=`` query value must equal the configured
    ``WEBHOOK_TOKEN`` (constant-time compare); a missing/empty/wrong token is 401. The
    review itself is NOT awaited here — it is enqueued and a background worker casts the
    vote — because the review takes far longer than Gerrit's webhook socket timeout.
    """
    cfg: ReceiverConfig = request.app.state.config
    token = request.query_params.get("token", "")
    if not cfg.webhook_token or not hmac.compare_digest(token, cfg.webhook_token):
        logger.warning("review-bot webhook: rejected (missing/invalid token)")
        return JSONResponse(status_code=401, content={"status": "unauthorized"})

    payload: Any
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — tolerate a non-JSON/empty body; ACK so Gerrit doesn't retry
        logger.info("review-bot webhook: received non-JSON or empty body")
        return JSONResponse(status_code=202, content={"status": "accepted", "queued": False})

    queue: asyncio.Queue | None = getattr(request.app.state, "queue", None)
    if queue is not None and isinstance(payload, dict):
        queue.put_nowait(payload)
        queued = True
    else:
        queued = False
    return JSONResponse(status_code=202, content={"status": "accepted", "queued": queued})


@app.post("/rerun", status_code=202)
async def rerun(request: Request) -> JSONResponse:
    """Manually FORCE a fresh review of a change (operability — recover a stuck vote).

    Auth: same ``?token=`` secret as ``/webhook`` (constant-time). Body/query supplies
    ``change`` (a Gerrit change id/number). The receiver looks up the change's CURRENT
    revision, enqueues it with the force marker, and ACKs 202; the worker re-reviews it
    bypassing the dedup + existing-vote short-circuits — so a stuck fail-closed ``-1``
    (e.g. from a transient LLM outage) is re-reviewed without amending. Still fail-closed:
    a rerun can only request a FRESH review, never force a PASS.
    """
    cfg: ReceiverConfig = request.app.state.config
    token = request.query_params.get("token", "")
    if not cfg.webhook_token or not hmac.compare_digest(token, cfg.webhook_token):
        return JSONResponse(status_code=401, content={"status": "unauthorized"})

    change = request.query_params.get("change")
    if not change:
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                change = body.get("change") or body.get("change_id")
    if not change:
        return JSONResponse(status_code=400, content={"status": "missing 'change'"})

    # Look up the change's current revision (off the event loop) to shape an event.
    from rebar.review_bot.gerrit_client import GerritClient, GerritError

    try:
        event = await asyncio.to_thread(GerritClient(cfg).get_change_event, str(change))
    except GerritError as exc:
        return JSONResponse(status_code=502, content={"status": "gerrit error", "detail": str(exc)})
    if event is None:
        return JSONResponse(status_code=404, content={"status": "change not found"})

    event["_rebar_force"] = True
    queue: asyncio.Queue | None = getattr(request.app.state, "queue", None)
    queued = False
    if queue is not None:
        queue.put_nowait(event)
        queued = True
    logger.info("review-bot rerun: queued force re-review of change %s", change)
    return JSONResponse(
        status_code=202, content={"status": "accepted", "queued": queued, "force": True}
    )


def _port() -> int:
    """Resolve the listen port from ``REVIEW_BOT_PORT`` (default ``DEFAULT_PORT``)."""
    raw = os.environ.get("REVIEW_BOT_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        logger.warning("REVIEW_BOT_PORT=%r is not an int; using %d", raw, DEFAULT_PORT)
        return DEFAULT_PORT


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=_port())
