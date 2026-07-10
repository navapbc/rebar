# ADR 0038 — Activity-based liveness under run_sync; async idle-watchdog deferred

- **Status:** Accepted (authored by story `chief-contained-hoopoe`; finalized by the integrator story `authorial-hated-blackbear`, epic jira-reb-687)
- **Date:** 2026-07-09

## Context

LLM gate ops run a tool-using agent via pydantic-ai's **synchronous** `agent.run_sync()`. A
single hard total-runtime timeout would truncate healthy multi-minute / large-context
(1M-token) runs, whose time-to-first-token can legitimately be minutes of silence. Liveness
must be gauged by ACTIVITY, not total elapsed time.

The community async pattern — a stream-event idle-watchdog that resets on each streamed event
(LangGraph `idle_timeout`, the Claude Agent SDK stall watchdog) — requires async
`agent.run()` / `run_stream_events` and an event loop. The runner uses `run_sync` exclusively
and has no such loop.

## Decision

Achieve activity-based liveness with `run_sync`-compatible mechanisms, and DEFER the async
idle-watchdog:

- **Per-request read timeout** (`httpx.Timeout(read=cfg.timeout_s, …)` on the shared client)
  bounds a hung MODEL at the HTTP layer — the real, common failure.
- **Per-tool timeout** (`Agent(tool_timeout=cfg.llm_tool_timeout_s)`) bounds a hung ASYNC/MCP
  tool. It is a no-op for SYNC in-process tools (async cancellation cannot interrupt a
  blocking call); those are fast and bounded by the derived step caps.
- **Step caps** (`request_limit`/`tool_calls_limit`, derived from `max_iterations`) bound
  runaway/oscillating loops.
- **No total-runtime timeout** anywhere in the gate path.

The sub-event stream-idle-watchdog is NOT built: it needs the agentic runner migrated to
async `agent.run()` / `run_stream_events`, which conflicts with the exclusive `run_sync`
design.

## Consequences

- Healthy long / large-context runs are never truncated; a hung model, a hung async tool, and
  a runaway loop are each bounded.
- Sync in-process tools are not individually timeout-bounded (documented caveat); if that ever
  matters, a follow-on can make them async or thread-cancellable.
- **Async-migration trigger:** if/when the agentic runner moves to async `agent.run()`, add
  the stream-event idle-watchdog (reset-on-event), wrapping the event iterator to avoid
  pydantic-ai #4796 (a failing tool + handler AssertionError masking the real error).
