"""Trusted FastAPI shell for asynchronous op-cert gate jobs.

``POST /opcert/jobs`` validates and queues a bounded single-worker job. ``GET``
returns its record, so long gates never hold requests open. API Gateway supplies
SigV4 authentication, and ``X-Opcert-Guard`` provides defense in depth before
enqueueing. Importing this module intentionally requires the ``reviewbot`` extra.

Run with ``uvicorn rebar.opcert_service.app:app --host 0.0.0.0 --port 8080``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from rebar.opcert_service import jobs
from rebar.opcert_service.config import (
    DEFAULT_SHUTDOWN_CANCEL_SECONDS,
    OpcertServiceConfig,
)
from rebar.opcert_service.keyprov import compose_signer

logger = logging.getLogger("rebar.opcert_service")

#: One background worker: jobs are signed serially (one signing key, one workspace at a time).
WORKER_COUNT = 1


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Compose the ONE startup op-cert signer (fail-closed BEFORE serving), then start the
    in-process job queue + the single background worker; stop them and clean up on shutdown."""
    # AC1 (story 6f14): validate + compose the signer BEFORE any queue/worker/network/workspace
    # creation. An invalid key source raises OpcertKeyError here, so the app never begins serving.
    app.state.signer = compose_signer(app.state.config)
    app.state.queue = asyncio.Queue()
    app.state.jobs = {}
    # Use an app-owned executor. Loop teardown joins its default executor without
    # a bound, while cancellation cannot stop an active OS thread. Ownership lets
    # shutdown abandon a wedged job instead of waiting up to the job timeout.
    app.state.executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=WORKER_COUNT, thread_name_prefix="opcert-job"
    )
    tasks = [asyncio.create_task(_worker(app)) for _ in range(WORKER_COUNT)]
    app.state.tasks = tasks
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        # Give cancelled workers a bounded grace period. Collect ``CancelledError``
        # values so lifespan shutdown never propagates them.
        if tasks:
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_shutdown_cancel_seconds(app),
                )
        # ABANDON whatever is still running. ``wait=False`` is the whole point: a thread cannot be
        # force-cancelled, so waiting here would reintroduce the unbounded join this fix removes.
        # ``cancel_futures=True`` drops work that never started.
        app.state.executor.shutdown(wait=False, cancel_futures=True)
        # Remove ONLY rebar's process-owned key copy (dir + file); never the deployment source.
        signer = getattr(app.state, "signer", None)
        if signer is not None:
            signer.cleanup()


def _shutdown_cancel_seconds(app: FastAPI) -> float:
    """The lifespan's bounded cancel window, defaulting if the app carries no config."""
    cfg = getattr(app.state, "config", None)
    return float(getattr(cfg, "shutdown_cancel_seconds", DEFAULT_SHUTDOWN_CANCEL_SECONDS))


async def _offload(app: FastAPI, rec: dict) -> dict:
    """Run one job body on the app-owned executor (the seam tests replace).

    Deliberately NOT ``asyncio.to_thread``: see the executor comment in :func:`lifespan`.
    """
    loop = asyncio.get_running_loop()
    cfg: OpcertServiceConfig = app.state.config
    return await loop.run_in_executor(
        app.state.executor,
        functools.partial(
            jobs.run_job,
            ticket_id=rec["ticket_id"],
            kind=rec["kind"],
            cfg=cfg,
            signer=app.state.signer,
        ),
    )


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
            async with asyncio.timeout(cfg.job_timeout_seconds):
                fields = await _offload(app, rec)
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
        except Exception as exc:
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
