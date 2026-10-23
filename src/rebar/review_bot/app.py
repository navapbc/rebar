"""FastAPI receiver for Gerrit review webhooks.
The internal Gerrit route authenticates its origin header and network peer.
Public callers use a receiver token. POST /webhook acknowledges quickly,
then a worker clones, reviews, and votes. A backfill reconciler also runs.
The core rebar package remains importable without FastAPI."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hmac
import logging
import os
import socket
import threading
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

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
    shutdown_force_exit_grace_seconds,
)
from rebar.review_bot.gerrit_client import GerritClient, GerritError
from rebar.review_bot.startup import compose_startup_binding

if TYPE_CHECKING:
    pass

logger = logging.getLogger("rebar.review_bot")

#: Public webhook and rerun callers prefer X-Rebar-Token because
#: uvicorn logs query strings, not headers. Query tokens are redacted.
#: The deployed Gerrit plugin cannot send this header and uses the
#: internal origin and network checks.
TOKEN_HEADER = "X-Rebar-Token"
_WEBHOOK_AUTH_REJECTIONS_ATTR = "webhook_auth_rejections"
_WEBHOOK_AUTH_LAST_REJECTED_AT_ATTR = "webhook_auth_last_rejected_at"

#: Default listen port when run via the ``__main__`` convenience runner. The
#: deployment single-sources this from the ``.env`` (``REVIEW_BOT_PORT``) and passes
#: it through docker-compose, so this default only matters for a bare local run.
DEFAULT_PORT = 8000

#: Number of background workers draining the review queue. One is enough for a
#: single-box PoC (reviews serialize on the per-(change,rev) lock anyway).
WORKER_COUNT = 1

#: A cancelled to_thread coroutine can leave a non-daemon review thread
#: running. Arm a hard-exit grace sized to let bounded store writes
#: release locks before the container deadline. Named offload threads
#: identify this app's work without matching unrelated executor threads.
_OFFLOAD_THREAD_PREFIX = "reviewbot-offload"


def _install_offload_executor() -> None:
    """Install a named default executor so shutdown can identify this app's
    asyncio.to_thread review work. The loop tears it down on exit."""
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(thread_name_prefix=_OFFLOAD_THREAD_PREFIX)
    )


def _running_offload_threads() -> list[threading.Thread]:
    """Return active review-bot offload threads. An empty result needs no
    hard-exit timer on clean shutdown."""
    return [
        t
        for t in threading.enumerate()
        if t.is_alive() and not t.daemon and t.name.startswith(_OFFLOAD_THREAD_PREFIX)
    ]


def _force_process_exit(code: int = 0) -> None:  # pragma: no cover - exercised via the seam
    """Log surviving offloads, then exit without joining abandoned review
    threads. Kept as a seam so tests can observe the action."""
    survivors = [t.name for t in _running_offload_threads()]
    logger.critical(
        "review-bot: graceful shutdown exceeded its bound with orphaned offload thread(s) %s "
        "still running; forcing process exit (code=%s) so the container releases its port for "
        "the replacement (ADR 0067)",
        survivors,
        code,
    )
    os._exit(code)


def _arm_hard_shutdown(grace: float) -> threading.Timer:
    """Arm a daemon timer to force process exit after the grace period.
    Clean exit discards the timer. A stranded offload cannot hold the
    listener port indefinitely."""
    timer = threading.Timer(grace, _force_process_exit)
    timer.daemon = True
    timer.start()
    return timer


#: Share review timeout with backfill and shutdown drain settings with
#: the container budget. Config exposes both without importing FastAPI.


def _config() -> ReceiverConfig:
    """Process-wide receiver config (env/SSM-sourced). Resolved fresh per app build so
    a reload picks up rotated secrets."""
    return ReceiverConfig.from_env()


def _gerrit_auth_health(cfg: ReceiverConfig) -> tuple[bool, str]:
    if not cfg.gerrit_bot_token:
        return False, "gerrit_auth_missing_token"
    try:
        GerritClient(cfg).check_auth()
    except GerritError as exc:
        status = exc.status if exc.status is not None else "transport"
        return False, f"gerrit_auth_failed:{status}"
    return True, "ok"


def _request_token(request: Request) -> str:
    """Prefer X-Rebar-Token over the query parameter so public callers keep
    secrets out of access logs. The deployed Gerrit webhook plugin cannot
    send this custom header and uses separate internal origin authentication.
    Query-token requests remain accepted and redacted in access logs."""
    header_token = request.headers.get(TOKEN_HEADER, "").strip()
    if header_token:
        return header_token
    return request.query_params.get("token", "")


def _resolve_host_addresses(hostname: str) -> set[str]:
    try:
        return {
            str(sockaddr[0])
            for *_, sockaddr in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        }
    except socket.gaierror:
        return set()


def _configured_gerrit_service_addresses(cfg: ReceiverConfig) -> set[str]:
    host = urlparse(cfg.gerrit_base_url).hostname
    return _resolve_host_addresses(host) if host else set()


def _is_internal_gerrit_webhook(request: Request, cfg: ReceiverConfig) -> bool:
    """Return true for Gerrit's direct compose-network webhook delivery.

    The Gerrit webhooks plugin version deployed with Gerrit 3.14 does not read arbitrary
    ``header =`` values from ``webhooks.config``; it sends only its built-in ``X-Origin-Url``.
    The header is not a secret, so it is only accepted when the TCP peer resolves to the
    configured Gerrit service host. Public nginx requests always carry ``X-Forwarded-For``, so
    absence of that header is the final guard that keeps this fallback off the public edge.
    """
    origin = request.headers.get("X-Origin-Url", "").strip().rstrip("/")
    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    client_host = request.client.host if request.client is not None else ""
    expected_origins = {
        cfg.gerrit_base_url.rstrip("/"),
        cfg.gerrit_canonical_web_url.rstrip("/"),
    }
    return (
        bool(origin)
        and origin in expected_origins
        and not forwarded_for
        and client_host in _configured_gerrit_service_addresses(cfg)
    )


def _webhook_authorized(request: Request, cfg: ReceiverConfig) -> bool:
    token = _request_token(request)
    if cfg.webhook_token and hmac.compare_digest(token, cfg.webhook_token):
        return True
    return _is_internal_gerrit_webhook(request, cfg)


def _reset_webhook_auth_rejections(app: FastAPI) -> None:
    setattr(app.state, _WEBHOOK_AUTH_REJECTIONS_ATTR, 0)
    setattr(app.state, _WEBHOOK_AUTH_LAST_REJECTED_AT_ATTR, None)


def _record_webhook_auth_rejection(app: FastAPI) -> None:
    current = int(getattr(app.state, _WEBHOOK_AUTH_REJECTIONS_ATTR, 0))
    setattr(app.state, _WEBHOOK_AUTH_REJECTIONS_ATTR, current + 1)
    setattr(app.state, _WEBHOOK_AUTH_LAST_REJECTED_AT_ATTR, time.monotonic())


def _webhook_auth_rejection_health(app: FastAPI) -> dict[str, int]:
    last_rejected_at = getattr(app.state, _WEBHOOK_AUTH_LAST_REJECTED_AT_ATTR, None)
    age_seconds = (
        -1 if last_rejected_at is None else max(0, int(time.monotonic() - last_rejected_at))
    )
    return {
        "webhook_auth_rejections": int(getattr(app.state, _WEBHOOK_AUTH_REJECTIONS_ATTR, 0)),
        "webhook_auth_last_rejected_age_seconds": age_seconds,
    }


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the queue, the background review worker(s), the backfill reconciler, and
    the snapshot-cache janitor; stop them cleanly on shutdown."""
    cfg = app.state.config
    # Route every ``asyncio.to_thread`` review offload onto the bot's OWN, distinctly-named
    # executor so the shutdown force-exit gate (_running_offload_threads) can recognise an
    # orphaned offload precisely and never mistake an unrelated default-executor thread for it.
    _install_offload_executor()
    _reset_webhook_auth_rejections(app)
    app.state.queue = asyncio.Queue()
    tasks: list[asyncio.Task] = []
    for _ in range(WORKER_COUNT):
        tasks.append(asyncio.create_task(_worker(app.state.queue, cfg)))
    # Re-arm the reconciler's cooperative-stop flag (module state, so a process that already
    # ran a lifespan would otherwise start this one already shutting down).
    _reconcile.clear_stop()
    reconcile_task = asyncio.create_task(_reconcile.reconcile_loop(config=cfg))
    tasks.append(reconcile_task)
    app.state.tasks = tasks
    # Start snapshot cleanup off the hot path. Janitor failure must not
    # block review-bot startup.
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
        # Drain acknowledged queue work before cancellation. One deadline
        # covers the queue and reconciler so shutdown stays below the container
        # stop budget. Backfill recovers events left queued after that deadline.
        deadline = time.monotonic() + shutdown_drain_seconds()

        def _remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        # Tell the reconciler to wind down BEFORE either wait, so it starts no new review while
        # we drain (otherwise a fresh candidate could re-extend the wait, pass after pass).
        _reconcile.request_stop()
        queue: asyncio.Queue | None = getattr(app.state, "queue", None)
        if queue is not None:
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(queue.join(), timeout=_remaining())
        # After queue drain, wait only if the reconciler still has a review
        # in flight. Share the remaining deadline. asyncio.wait returns on
        # timeout without cancelling and then waiting indefinitely. Bounded
        # cancel below handles the remaining task.
        if _voter.in_flight_reviews() and not reconcile_task.done():
            await asyncio.wait({reconcile_task}, timeout=_remaining())
        for task in tasks:
            task.cancel()
        # Bound task cancellation and await. Offloaded OS work may continue,
        # but gather returns coroutine errors without extending shutdown.
        if tasks:
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=shutdown_cancel_seconds(),
                )
        # Clear the module-wide reconciler stop flag after shutdown.
        # CLI backfill and later app instances can otherwise skip every candidate.
        _reconcile.clear_stop()
        # Arm hard exit only for surviving named offload threads.
        # Give a bounded store write time to release its mkdir lock first.
        # The total deadline precedes Docker's stop kill.
        if _running_offload_threads():
            _arm_hard_shutdown(shutdown_force_exit_grace_seconds())


def _event_revision(event: Any) -> str | None:
    """The revision sha the QUEUED event describes, or ``None`` if the payload lacks one."""
    patch_set = event.get("patchSet") if isinstance(event, dict) else None
    revision = patch_set.get("revision") if isinstance(patch_set, dict) else None
    return str(revision) if revision else None


def _current_revision(event: dict, cfg: ReceiverConfig) -> str | None:
    """Return Gerrit's current revision or None on lookup or shape failure.
    The queued event still proceeds when currency cannot be checked."""
    try:
        change = event.get("change") if isinstance(event, dict) else None
        change_id = change.get("id") if isinstance(change, dict) else None
        if not change_id:
            return None
        # Imported inside the function to keep the module's lazy-import style (the client
        # pulls the REST stack; nothing here needs it until a review is actually dequeued).
        from rebar.review_bot.gerrit_client import GerritClient

        current = GerritClient(cfg).get_change_event(str(change_id))
        return _event_revision(current)
    except Exception:  # noqa: BLE001 — fail open: an unknown revision must not skip a review
        logger.warning("review-bot worker: current-revision lookup failed; reviewing anyway")
        return None


async def _superseded_by(event: Any, cfg: ReceiverConfig) -> str | None:
    """Return the newer Gerrit revision when a queued event is stale.
    Unknown or matching revisions proceed. Run the blocking lookup off the
    event loop."""
    queued = _event_revision(event)
    if queued is None:
        return None
    current = await asyncio.to_thread(_current_revision, event, cfg)
    if current is None or current == queued:
        return None
    return current


async def _worker(queue: asyncio.Queue, cfg: ReceiverConfig) -> None:
    """Drain queued events with one bounded review and vote per event.
    Log failures and timed-out work as VOTER_ERROR, then continue so one
    event cannot stall the queue."""
    review_timeout = review_timeout_seconds()
    while True:
        event = await queue.get()
        try:
            # Check contributor rerun eligibility through privileged REST calls,
            # then queue the current revision for the normal worker path.
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
            # Skip superseded queued revisions before costly review or stale votes.
            # Forced and manual reruns already use the current revision and bypass this
            # check. Unknown currency proceeds so a lookup error cannot lose work.
            superseded_by = (
                None
                if force or (isinstance(event, dict) and event.get("type") == "manual-rerun")
                else await _superseded_by(event, cfg)
            )
            if superseded_by is not None:
                _discard_superseded(event, superseded_by)
                continue
            await asyncio.wait_for(
                _voter.review_and_vote(event, config=cfg, force=force),
                timeout=review_timeout,
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            # Abandon a timed-out review without a vote and emit VOTER_ERROR.
            # Backfill can retry while the worker continues draining.
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


def _discard_superseded(event: Any, current_revision: str) -> None:
    """Emit a VOTER_ERROR marker for a stale event discarded before review."""
    change = event.get("change") if isinstance(event, dict) else None
    change_id = change.get("id") if isinstance(change, dict) else None
    queued_revision = _event_revision(event)
    _voter._voter_error(
        change_id=change_id,
        revision_id=queued_revision,
        error=(
            f"queued revision {queued_revision} was superseded by current revision "
            f"{current_revision} before the review started — discarded without reviewing "
            "(a newer patchset-created event covers the current revision)"
        ),
    )
    logger.info(
        "review-bot worker: discarding superseded event for change %s (queued revision %s, "
        "current revision %s)",
        change_id,
        queued_revision,
        current_revision,
    )


# Install structured rebar logging before app startup so INFO events
# reach stdout beside uvicorn logs.
configure_logging()

# Redact query tokens from uvicorn access logs at import time.
# The deployed Gerrit webhook plugin still uses query-token delivery.
install_access_log_redaction()

app = FastAPI(
    title="rebar review-bot",
    summary="Gerrit webhook receiver — reviews a patchset and casts the LLM-Review vote.",
    lifespan=lifespan,
)
app.state.config = _config()
app.state.startup_binding = compose_startup_binding(app.state.config)


@app.get("/health")
async def health() -> dict[str, str | int]:
    """Liveness probe + in-flight review count + queue depth. Returns 200 with
    ``{"status": "ok", "in_flight": N, "queue_depth": M}`` (no kernel call).

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

    ``queue_depth`` is the number of events waiting on the review queue (bug 9f63). Without
    it, an agent looking at a change still sitting at ``LLM-Review = --`` cannot distinguish
    "the event was never queued" from "queued, waiting behind N others" from "the bot is
    down" — all three present identically, and that ambiguity is what caused completed work to
    be abandoned. Read defensively off ``app.state`` (the queue only exists once the lifespan
    has run) and always reported as a plain ``int``, ``0`` when there is no queue, so the key
    is never absent. The ``status`` / ``in_flight`` keys and their expressions are untouched:
    ``infra/scripts/autodeploy.sh`` parses ``in_flight``.

    ``webhook_auth_rejections`` makes the inbound-auth failure mode observable: a rotated token
    can leave the process live and outbound Gerrit auth healthy while Gerrit-originated webhooks
    are rejected before they enqueue any work.
    """
    queue: asyncio.Queue | None = getattr(app.state, "queue", None)
    queue_depth = int(queue.qsize()) if queue is not None else 0
    auth_ok, auth_reason = await asyncio.to_thread(_gerrit_auth_health, app.state.config)
    webhook_auth = _webhook_auth_rejection_health(app)
    if auth_ok:
        return {
            "status": "ok",
            "in_flight": _voter.in_flight_reviews(),
            "queue_depth": queue_depth,
            "gerrit_auth": "ok",
            **webhook_auth,
        }
    return {
        "status": "degraded",
        "in_flight": _voter.in_flight_reviews(),
        "queue_depth": queue_depth,
        "gerrit_auth": "failed",
        "reason": auth_reason,
        **webhook_auth,
    }


@app.post("/webhook", status_code=202)
async def webhook(request: Request) -> JSONResponse:
    """Authenticate, ACK fast (202), and enqueue the event for background review.

    Auth (ADR-0014, ticket 66af): public callers must supply the configured ``WEBHOOK_TOKEN``
    (constant-time compare) in the ``X-Rebar-Token`` header (preferred — kept out of the
    access-logged request line) or the legacy ``?token=`` query param (accepted for
    backward-compat, its value scrubbed from the log by the redaction filter). The internal
    Gerrit→review-bot path has no public hop and is authenticated by the Gerrit webhooks
    plugin's built-in ``X-Origin-Url`` header because that plugin does not support arbitrary
    configured headers. Unauthorized requests are 401. The review itself is NOT awaited here —
    it is enqueued and a background worker casts the vote — because the review takes far longer
    than Gerrit's webhook socket timeout.
    """
    cfg: ReceiverConfig = request.app.state.config
    if not _webhook_authorized(request, cfg):
        _record_webhook_auth_rejection(request.app)
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
    revision, resets exactly that revision's ``LLM-Review`` vote to neutral, verifies
    that no account retains a nonzero vote, then best-effort re-arms the attempt budget
    and enqueues it with the force marker. The neutral Gerrit write is the durable
    review-owed marker: its patchset-bearing event lets reconciliation recover after
    process loss. Still fail-closed: a rerun can only request a FRESH review, never force
    a PASS.
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

    gc = GerritClient(cfg)
    try:
        event = await asyncio.to_thread(gc.get_change_event, str(change))
    except GerritError as exc:
        # Log the Gerrit error detail server-side; keep it OUT of the client
        # response body (py/stack-trace-exposure) — a generic status is enough.
        logger.warning("review-bot rerun: gerrit lookup failed for change %s: %s", change, exc)
        return JSONResponse(status_code=502, content={"status": "gerrit error"})
    if event is None:
        return JSONResponse(status_code=404, content={"status": "change not found"})

    change_id = str(event["change"]["id"])
    revision = str(event["patchSet"]["revision"])

    # Write the durable rerun marker before acknowledgement. Reset the
    # vote to neutral and fail closed if a nonzero vote survives.
    try:
        await asyncio.to_thread(gc.reset_llm_review_vote, change_id, revision)
    except GerritError as exc:
        logger.warning(
            "review-bot rerun: vote reset failed change=%s revision=%s: %s",
            change_id,
            revision,
            exc,
        )
        return JSONResponse(status_code=502, content={"status": "vote reset failed"})

    try:
        has_vote = await asyncio.to_thread(gc.has_llm_review_vote, change_id, revision)
    except GerritError as exc:
        logger.warning(
            "review-bot rerun: vote verification failed change=%s revision=%s: %s",
            change_id,
            revision,
            exc,
        )
        return JSONResponse(status_code=502, content={"status": "vote verification failed"})
    if has_vote:
        logger.warning(
            "review-bot rerun: vote reset refused change=%s revision=%s: nonzero vote remains",
            change_id,
            revision,
        )
        return JSONResponse(status_code=409, content={"status": "nonzero vote remains"})

    # Best-effort re-arm only after durable neutral state is proven. A local failure
    # cannot undo or reject the authoritative Gerrit marker already created above.
    try:
        from rebar.review_bot.dedup import DedupStore

        await asyncio.to_thread(
            DedupStore(cfg.dedup_db_path).reset_attempts,
            change_id,
            revision,
        )
    except Exception as exc:  # noqa: BLE001 — durable Gerrit marker already established
        logger.warning(
            "review-bot rerun: attempts reset failed change=%s revision=%s: %s",
            change_id,
            revision,
            exc,
        )

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
    raw = os.environ.get("REVIEW_BOT_PORT")  # read-via: server-bootstrap
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
