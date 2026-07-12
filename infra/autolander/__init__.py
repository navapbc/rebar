"""Serial auto-lander (epic f1fa) — THIS project's Gerrit+GHA landing infrastructure.

Standalone project tooling: stdlib-only, does NOT import `rebar` core (keeps rebar
platform-agnostic; landing/merge-queueing is platform-specific + maintainer-owned).
Run in-container as `python -m autolander.loop` (with `infra/` on the path).
"""
