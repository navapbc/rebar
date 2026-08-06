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
from rebar.review_bot.config import (
    ReceiverConfig,
    configure_logging,
    install_access_log_redaction,
    review_timeout_seconds,
    shutdown_cancel_seconds,
    shutdown_drain_seconds,
)

logger = logging.getLogger("rebar.review_bot")

#: HTTP header carrying the webhook/rerun secret (ticket 66af). The token is read from THIS
#: header in preference to the legacy ``?token=`` query param because uvicorn's access log
#: records the request LINE (method + path + query) but NOT headers — so a header-authenticated
#: request never writes the secret to stderr → journald. Do NOT reintroduce the query-string
#: form as a convenience: it is exactly what leaked the bot's Gerrit credential into the most-
#: grepped log on the box. ``config.install_access_log_redaction()`` is the backstop for any
#: caller still using the query form. Gerrit's ``webhooks`` plugin can send this via its
#: ``header`` config (see infra/gerrit/webhooks.config); the operator ``/rerun`` recipe uses it
#: directly.
TOKEN_HEADER = "X-Rebar-Token"

#: Default listen port when run via the ``__main__`` convenience runner. The
#: deployment single-sources this from the ``.env`` (``REVIEW_BOT_PORT``) and passes
#: it through docker-compose, so this default only matters for a bare local run.
DEFAULT_PORT = 8000

#: Number of background workers draining the review queue. One is enough for a
#: single-box PoC (reviews serialize on the per-(change,rev) lock anyway).
WORKER_COUNT = 1

#: The per-review wall-clock timeout now lives in ``config.review_timeout_seconds()`` so the
#: live worker (below) AND the backfill reconciler (``reconcile.reconcile_once``) bound a single
#: review with the SAME value — the reconciler previously had NO timeout, so one hung backfill
#: review could freeze all backfill (incident 2731 / bug 9d7c is the live-worker analogue).
#:
#: The shutdown drain window (``config.shutdown_drain_seconds()``) lives there for the same
#: reason, plus one more: the container's ``stop_grace_period`` is sized against it, and the
#: test asserting that relationship must read it without importing this fastapi-laden module.


def _config() -> ReceiverConfig:
    """Process-wide receiver config (env/SSM-sourced). Resolved fresh per app build so
    a reload picks up rotated secrets."""
    return ReceiverConfig.from_env()


def _request_token(request: Request) -> str:
    """Extract the auth secret, preferring the ``X-Rebar-Token`` header over the legacy
    ``?token=`` query param.

    The header is preferred because it keeps the secret OUT of the access-logged request line
    (ticket 66af). The query param is still accepted so a legacy caller (a Gerrit webhook whose
    ``webhooks.config`` has not yet been re-pushed to send the header) keeps authenticating; its
    value is protected in the log by ``config.install_access_log_redaction()``."""
    header_token = request.headers.get(TOKEN_HEADER, "").strip()
    if header_token:
        return header_token
    return request.query_params.get("token", "")


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
    except Exception:
        logger.exception("review-bot: snapshot janitor failed to start (non-fatal)")
    try:
        yield
    finally:
        if janitor_stop is not None:
            janitor_stop.set()
        # Graceful drain (deploy resilience): let the still-running worker finish the QUEUED
        # events before we cancel it, so a routine autodeploy restart does not abandon
        # acknowledged (202 "queued") webhooks (the in-memory queue is otherwise lost on every
        # restart). Bounded by ``config.shutdown_drain_seconds()``; anything still queued when
        # the window elapses is left for the backfill reconciler — fail-safe, never fail-lose.
        queue: asyncio.Queue | None = getattr(app.state, "queue", None)
        if queue is not None:
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(queue.join(), timeout=shutdown_drain_seconds())
        for task in tasks:
            task.cancel()
        # Bound the cancel + await (c2ba). A well-behaved task cancels promptly (its coroutine
        # unwinds at the next await — including the await inside an ``asyncio.to_thread`` offload,
        # which returns immediately without waiting for the orphaned OS thread). But a task slow
        # to honor cancellation — a shielded region, a synchronous ``finally`` — would make an
        # unbounded ``await task`` hang shutdown with no stateable upper bound. Await the join
        # under ``shutdown_cancel_seconds()`` and ABANDON whatever is still pending (the process
        # is exiting; any in-flight store write is bounded independently by event_append's
        # per-git timeout). ``gather(return_exceptions=True)`` collects each task's CancelledError
        # as a result rather than re-raising, so this never propagates out of the lifespan.
        if tasks:
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=shutdown_cancel_seconds(),
                )


async def _worker(queue: asyncio.Queue, cfg: ReceiverConfig) -> None:
    """Drain the review queue, running one review→vote per event under a bounded timeout.

    A per-event FAILURE is logged and a per-event HANG (a review exceeding
    ``REVIEW_TIMEOUT_SECONDS`` — a blocked clone/subprocess/LLM call) is timed out,
    abandoned, and recorded as a countable ``VOTER_ERROR`` timeout marker; either way the
    worker continues to the next event. It must never die AND never stall on one event and
    starve the queue (bug 9d7c — the single worker silently backing up behind a hung review
    when the disk filled mid-clone)."""
    review_timeout = review_timeout_seconds()
    while True:
        event = await queue.get()
        try:
            # A contributor `recheck-review` comment (ticket bb9b): decide eligibility
            # privileged-side and, on acceptance, requeue the change's CURRENT revision
            # as a forced review event (it then flows through the normal branch below on
            # its next dequeue). Runs off the event loop — it makes blocking REST calls.
            if isinstance(event, dict) and event.get("type") == "comment-added":
                from rebar.review_bot import retrigger as _retrigger

                follow_up = await asyncio.to_thread(
                    _retrigger.handle_comment_added, event, config=cfg
                )
                if follow_up is not None:
                    queue.put_nowait(follow_up)
                continue
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
        except Exception:
            logger.exception("review-bot worker: review_and_vote raised")
        finally:
            queue.task_done()


# Configure rebar's structured logging (the _emit() INFO events) so they reach stdout→journald.
# uvicorn configures only its own uvicorn.* loggers; without this the rebar.* loggers have no
# handler and every INFO line is silently dropped (the observability outage this fixes). Called
# at import, before the app is built, so startup logs are captured too.
configure_logging()

# Install the access-log token-redaction backstop (ticket 66af option c). Even though the
# secret now travels in the ``X-Rebar-Token`` header (out of the access-logged request line),
# any request line that still carries ``?token=`` — a legacy caller or a future regression — is
# scrubbed before it reaches stderr → journald. Idempotent; called at import for the same reason
# as configure_logging.
install_access_log_redaction()

app = FastAPI(
    title="rebar review-bot",
    summary="Gerrit webhook receiver — reviews a patchset and casts the LLM-Review vote.",
    lifespan=lifespan,
)
app.state.config = _config()


@app.get("/health")
async def health() -> dict[str, str | int]:
    """Liveness probe + in-flight review count. Returns 200 with
    ``{"status": "ok", "in_flight": N}`` (no kernel call).

    ``in_flight`` is the deploy loop's drain signal (bug 34cd): recreating this container
    mid-review KILLS the review, and does so invisibly — the process was asked to stop, so
    nothing fails, no ``VOTER_ERROR`` is emitted, and ``restarts`` stays 0. During a landing
    burst that can live-lock the ``LLM-Review`` gate with every health signal green.
    ``infra/scripts/autodeploy.sh`` reads this field and DEFERS the recreation while it is
    non-zero, so the count is a load-bearing contract, not a debugging nicety — see the
    ``in_flight`` assertions in ``tests/scripts/test_autodeploy_review_drain.py``.

    Carried on the EXISTING liveness route rather than a new one on purpose: the deploy
    loop already polls ``HEALTH_URL`` for its post-deploy readiness gate, so the drain
    needs no second endpoint, no second URL to configure, and no new exposed surface. The
    ``status`` key is unchanged, so every existing caller keeps working.
    """
    return {"status": "ok", "in_flight": _voter.in_flight_reviews()}


@app.post("/webhook", status_code=202)
async def webhook(request: Request) -> JSONResponse:
    """Authenticate, ACK fast (202), and enqueue the event for background review.

    Auth (ADR-0014, ticket 66af): the secret must equal the configured ``WEBHOOK_TOKEN``
    (constant-time compare), supplied in the ``X-Rebar-Token`` header (preferred — kept out of
    the access-logged request line) or the legacy ``?token=`` query param (accepted for
    backward-compat, its value scrubbed from the log by the redaction filter). A
    missing/empty/wrong token is 401. The
    review itself is NOT awaited here — it is enqueued and a background worker casts the
    vote — because the review takes far longer than Gerrit's webhook socket timeout.
    """
    cfg: ReceiverConfig = request.app.state.config
    token = _request_token(request)
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

    Auth: same secret as ``/webhook`` (constant-time), supplied in the ``X-Rebar-Token``
    header (preferred — keeps it out of the access log) or the legacy ``?token=`` query param.
    Body/query supplies
    ``change`` (a Gerrit change id/number). The receiver looks up the change's CURRENT
    revision, enqueues it with the force marker, and ACKs 202; the worker re-reviews it
    bypassing the dedup + existing-vote short-circuits — so a stuck fail-closed ``-1``
    (e.g. from a transient LLM outage) is re-reviewed without amending. Still fail-closed:
    a rerun can only request a FRESH review, never force a PASS.
    """
    cfg: ReceiverConfig = request.app.state.config
    token = _request_token(request)
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
        # Log the Gerrit error detail server-side; keep it OUT of the client
        # response body (py/stack-trace-exposure) — a generic status is enough.
        logger.warning("review-bot rerun: gerrit lookup failed for change %s: %s", change, exc)
        return JSONResponse(status_code=502, content={"status": "gerrit error"})
    if event is None:
        return JSONResponse(status_code=404, content={"status": "change not found"})

    # Re-arm the 0347 automatic-retry budget (harmonized with the contributor
    # `recheck-review` path): a force re-review on a stale exhausted budget would
    # otherwise immediately re-escalate. Best-effort — a reset failure must not
    # block the operator's rerun.
    try:
        from rebar.review_bot.dedup import DedupStore

        await asyncio.to_thread(
            DedupStore(cfg.dedup_db_path).reset_attempts,
            str(event["change"]["id"]),
            str(event["patchSet"]["revision"]),
        )
    except Exception:  # noqa: BLE001 — best-effort budget re-arm
        logger.warning("review-bot rerun: attempts-budget reset failed (continuing)")

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
