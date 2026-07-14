"""FastAPI ASGI app — the trusted op-cert gate service (story ee0b).

WHAT THIS IS. A thin HTTP shell over the FastAPI-free job core (:mod:`rebar.opcert_service.jobs`),
mirroring ``rebar.review_bot.app``'s software pattern: an ACK-fast async job API backed by an
in-process ``asyncio.Queue`` drained by a SINGLE background worker under a bounded per-run timeout.
Gate runs take 30s-minutes, so no synchronous request holds the socket: ``POST /opcert/jobs``
validates + enqueues + returns 202 ``{job_id}``; ``GET /opcert/jobs/{job_id}`` returns the record.

AUTHN. Endpoint SigV4 is terminated at API Gateway (the deploy story); the app additionally
requires a shared-secret header ``X-Opcert-Guard`` == ``REBAR_OPCERT_GUARD`` as defense in depth
(same posture as ``review_bot``'s ``?token=`` check; a header rather than a query param because API
Gateway injects static request headers on the integration). A missing/mismatched guard is 403
BEFORE any work is enqueued.

IMPORTABILITY CONTRACT. ``fastapi`` is imported at module top here on purpose — so
``import rebar.opcert_service.app`` requires the ``reviewbot`` extra, while ``import rebar`` (and
``import rebar.opcert_service``) does NOT pull FastAPI.

RUN. ``uvicorn rebar.opcert_service.app:app --host 0.0.0.0 --port 8080``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from rebar.opcert_service import jobs
from rebar.opcert_service.config import OpcertServiceConfig
from rebar.opcert_service.ssm import boto3_ssm_fetcher

logger = logging.getLogger("rebar.opcert_service")

#: One background worker: jobs are signed serially (one signing key, one workspace at a time).
WORKER_COUNT = 1


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the in-process job queue + the single background worker; stop them on shutdown."""
    app.state.queue = asyncio.Queue()
    app.state.jobs = {}
    tasks = [asyncio.create_task(_worker(app)) for _ in range(WORKER_COUNT)]
    app.state.tasks = tasks
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _worker(app: FastAPI) -> None:
    """Drain the queue, running one gate job per id in a worker thread under the per-run timeout.

    A hung/over-time job is ABANDONED (recorded ``error``/``timeout``) and the worker keeps
    draining — no single job can wedge the queue (the review-bot pattern)."""
    queue: asyncio.Queue = app.state.queue
    cfg: OpcertServiceConfig = app.state.config
    while True:
        job_id = await queue.get()
        rec = app.state.jobs.get(job_id)
        try:
            if rec is None:
                continue
            rec["status"] = "running"
            fields = await asyncio.wait_for(
                asyncio.to_thread(
                    jobs.run_job,
                    ticket_id=rec["ticket_id"],
                    kind=rec["kind"],
                    cfg=cfg,
                    ssm_fetcher=app.state.ssm_fetcher,
                ),
                timeout=cfg.job_timeout_seconds,
            )
            rec.update(fields)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            if rec is not None:
                rec.update(
                    status="error",
                    verdict=None,
                    envelope=None,
                    error={
                        "class": jobs.ERR_TIMEOUT,
                        "message": f"job exceeded the {cfg.job_timeout_seconds}s per-run timeout",
                    },
                )
        except Exception as exc:  # noqa: BLE001 — a bad job must not kill the worker
            logger.exception("opcert-service worker: run_job raised")
            if rec is not None:
                rec.update(
                    status="error",
                    verdict=None,
                    envelope=None,
                    error={"class": jobs.ERR_INTERNAL, "message": str(exc)},
                )
        finally:
            queue.task_done()


app = FastAPI(
    title="rebar op-cert gate",
    summary="Trusted-environment gate service: fetch authoritative state, run a gate, "
    "return a signed op-cert.",
    lifespan=lifespan,
)
app.state.config = OpcertServiceConfig.from_env()
#: The SSM key-fetch seam (tests replace this with a fake — no boto3/AWS/network).
app.state.ssm_fetcher = boto3_ssm_fetcher


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


def _guard_ok(request: Request) -> bool:
    cfg: OpcertServiceConfig = request.app.state.config
    guard = cfg.guard
    if not guard:  # unconfigured guard fails closed (defense in depth, review-bot posture)
        return False
    header = request.headers.get("X-Opcert-Guard", "")
    return hmac.compare_digest(header, guard)


@app.post("/opcert/jobs", status_code=202)
async def submit_job(request: Request) -> JSONResponse:
    """Validate ``{ticket_id, kind}``, enqueue, and ACK 202 ``{job_id}``.

    Guard-checked (403) BEFORE any work. ANY client field beyond ``ticket_id``/``kind`` (a
    ``material_fingerprint``, ``commit``, ``env_id``, …) is IGNORED — the server derives every
    authoritative value itself.
    """
    if not _guard_ok(request):
        return JSONResponse(status_code=403, content={"status": "forbidden"})

    try:
        body: Any = await request.json()
    except Exception:  # noqa: BLE001 — a non-JSON/empty body is a bad request
        return JSONResponse(
            status_code=400, content={"status": "invalid", "detail": "expected JSON"}
        )
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"status": "invalid", "detail": "expected object"}
        )

    ticket_id = body.get("ticket_id")
    kind = body.get("kind")
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        return JSONResponse(
            status_code=400, content={"status": "invalid", "detail": "ticket_id required"}
        )
    if kind not in jobs.VALID_KINDS:
        return JSONResponse(
            status_code=400,
            content={
                "status": "invalid",
                "detail": f"kind must be one of {list(jobs.VALID_KINDS)}",
            },
        )

    job_id = uuid.uuid4().hex
    request.app.state.jobs[job_id] = jobs.new_record(job_id, ticket_id.strip(), kind)
    request.app.state.queue.put_nowait(job_id)
    return JSONResponse(status_code=202, content={"job_id": job_id})


@app.get("/opcert/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> JSONResponse:
    """Return the job record, or 404 if unknown."""
    rec = request.app.state.jobs.get(job_id)
    if rec is None:
        return JSONResponse(status_code=404, content={"status": "not found"})
    return JSONResponse(status_code=200, content=rec)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=app.state.config.port)
