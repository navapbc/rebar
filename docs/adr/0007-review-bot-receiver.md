# ADR 0007: The review-bot is a thin FastAPI webhook receiver, not MCP-over-HTTP

- **Status:** Accepted
- **Context:** Epic *stand up AWS-hosted Gerrit + rebar review-bot (PoC)* (`d251`),
  story *S2 — app config / deploy* (this story owns the docker-compose stack, the
  Gerrit config, the nginx + TLS front, and the review-bot receiver skeleton).

## Context

Gerrit notifies external systems by POSTing **webhooks** — an ordinary HTTP request
with a JSON body describing the event (e.g. a patchset created). We need a service
on the box that receives those POSTs and, eventually (story S4b), runs the rebar
review kernel and posts a review score back to Gerrit.

rebar already exposes an agent surface over the **MCP server** (`rebar-mcp`). The
tempting shortcut is to "just point Gerrit at the MCP server." That does not work:

- MCP is a **JSON-RPC transport** with its own framing, handshake, and method
  envelope. A Gerrit webhook is a bare event JSON, not an MCP `tools/call` request.
  An MCP HTTP endpoint would **reject the webhook** as a malformed request.
- The MCP server is built as a **stdio** server (the `rebar-mcp` console script);
  exposing it over HTTP is a different transport entirely, and even then the
  request shape mismatch above remains.

## Decision

Run a **thin FastAPI ASGI app** (`src/rebar/review_bot/app.py`) as the webhook
receiver. It **imports the rebar review kernel as a library** (`import rebar` /
`rebar.llm`) rather than speaking to the MCP server. The receiver:

- exposes `GET /health` (liveness) and `POST /webhook` (accept Gerrit's event JSON);
- is packaged behind a new optional extra `reviewbot = ["fastapi",
  "uvicorn[standard]"]`, imported lazily so `import rebar` stays dependency-free
  (only `import rebar.review_bot.app` needs the extra);
- runs as its own container (`Dockerfile.reviewbot`, `pip install .[agents,reviewbot]`)
  in the docker-compose stack alongside Gerrit;
- sits behind the host nginx at the `/review/` path — nginx strips the prefix, so
  externally `https://<host>/review/health` reaches the app as `/health`.

S2 ships the **skeleton only** (health + a placeholder webhook that logs and
returns 202). The review/vote logic is **story S4b**.

## Consequences

- The receiver and the MCP server stay **decoupled** — the bot talks to rebar as a
  Python library, so it picks up the review kernel directly with no JSON-RPC hop and
  no transport-shape mismatch with Gerrit.
- `import rebar` remains lean: fastapi/uvicorn are confined to the `reviewbot`
  extra and to the `review_bot.app` module, so a plain library/CLI install pays
  nothing for them.
- nginx owns the public routing seam (`/review/` → receiver, everything else →
  Gerrit), so the receiver's internal port is an implementation detail
  (single-sourced as `REVIEW_BOT_PORT`).
- The deferred S4b work is isolated to one module: wiring `POST /webhook` to the
  kernel + a Gerrit REST call to vote, with no change to the transport decision here.
