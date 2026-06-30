"""FastAPI ASGI app — the rebar review-bot webhook receiver (epic d251 / story S2).

WHAT THIS IS (ADR-0007). A thin HTTP service that Gerrit posts review events to.
It imports the rebar review kernel **as a library** rather than re-exposing the
stdio MCP server (`rebar-mcp`) over HTTP: Gerrit emits a plain webhook (a JSON
POST), not MCP JSON-RPC, so an MCP HTTP transport would reject the body. nginx
routes ``/review/`` to this app (stripping the prefix), so externally the receiver
lives at ``https://<host>/review/`` and these routes are reached as ``/health`` and
``/webhook`` after the prefix strip.

SCOPE OF S2. Skeleton only — a liveness probe and a placeholder webhook sink. The
review/vote logic (invoke the review kernel, post a Gerrit review score back) is
story S4b; ``POST /webhook`` here merely accepts + logs the payload.

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

import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("rebar.review_bot")

#: Default listen port when run via the ``__main__`` convenience runner. The
#: deployment single-sources this from the ``.env`` (``REVIEW_BOT_PORT``) and passes
#: it through docker-compose, so this default only matters for a bare local run.
DEFAULT_PORT = 8000

app = FastAPI(
    title="rebar review-bot",
    summary="Gerrit webhook receiver (skeleton — review/vote logic lands in S4b).",
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Returns 200 with ``{"status": "ok"}`` (no kernel call)."""
    return {"status": "ok"}


@app.post("/webhook", status_code=202)
async def webhook(request: Request) -> JSONResponse:
    """Accept a Gerrit webhook payload, log it, and acknowledge (202).

    PLACEHOLDER (S2). Any JSON body is accepted and logged; no review is run and no
    Gerrit vote is posted yet — that is story S4b. A non-JSON / empty body is still
    acknowledged (we log that we could not parse it) so Gerrit's delivery does not
    see a 4xx and start retrying against a skeleton that intentionally does nothing.
    """
    payload: Any
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — tolerate any non-JSON/empty body on the skeleton sink
        payload = None
        logger.info("review-bot webhook: received non-JSON or empty body")
    else:
        logger.info("review-bot webhook: received payload %r", payload)
    return JSONResponse(status_code=202, content={"status": "accepted"})


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
