"""rebar review-bot — the Gerrit webhook receiver package (epic d251 / story S2).

This package hosts the thin HTTP service that Gerrit posts review events to. Per
ADR-0007 it is a small FastAPI ASGI app that imports the rebar review kernel **as a
library** — it is deliberately NOT the stdio MCP server (`rebar-mcp`) exposed over
HTTP, because Gerrit speaks plain webhooks (a JSON POST), not the MCP JSON-RPC
transport, and an MCP HTTP endpoint would reject an ordinary webhook body.

S2 ships only the receiver SKELETON (health + a placeholder webhook endpoint). The
actual review/vote logic — calling the review kernel and posting a Gerrit review
score back — is story S4b.

Importing this package must NOT require fastapi: the fastapi import is confined to
``rebar.review_bot.app`` (so ``import rebar`` and ``import rebar.review_bot`` stay
dependency-free, and only ``import rebar.review_bot.app`` needs the ``reviewbot``
extra installed).
"""
