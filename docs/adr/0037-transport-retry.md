# ADR 0037 — Transport-layer retry for LLM gate calls (SDK max_retries=0)

- **Status:** Accepted (authored by story `morbid-uncultured-arcticduck`; finalized by the integrator story `authorial-hated-blackbear`, epic jira-reb-687)
- **Date:** 2026-07-09

## Context

The three LLM gate operations (plan-review, code-review, completion-verify) run a
tool-using agent via pydantic-ai's synchronous `agent.run_sync()`. A transient provider
blip mid-run — `429`/`529`/`5xx`, a connect/read timeout, a dropped connection — must not
collapse the whole gate. But the naive recovery (restart the agent loop) re-executes
already-run, side-effecting tool calls (ticket comments) and multiplies token cost.

Two retry layers were available: the Anthropic SDK's built-in retries (`max_retries`,
default ~2), and a custom retry at the httpx transport. The SDK previously did ~2 retries
by inheritance, never deliberately configured.

## Decision

Own retries at the **httpx transport** (`pydantic_ai.retries.AsyncTenacityTransport`) and set
the SDK's `max_retries=0`, so retry lives at exactly one layer. The transport re-sends only
the single failed HTTP request/response pair BELOW the agent graph — completed tool results
are untouched, so no side-effecting tool is ever re-executed. `Retry-After` is honored
(capped at `llm_retry_max_wait_s`), else exponential backoff; the retriable set is
`{429,529,500,502,503,504}` + `httpx.TimeoutException`/`httpx.NetworkError`. Attempts are
bounded by `llm_retry_max_attempts` (`stop_after_attempt`, `reraise=True`). The retrying
client is built on EVERY Anthropic construction path (normal model-string AND
loopback-proxy-bypass), with a construction-time guard that fails fast if `max_retries != 0`
rather than silently regressing.

## Consequences

- A transient blip self-heals with no operator action and no duplicated tool side effects.
- Retry is config-controlled (`llm_retry_max_attempts` / `llm_retry_max_wait_s` on
  `LLMConfig`); setting attempts to `1` is the fail-fast back-out with no code revert.
- The buffered `run_sync` design (no streaming) means retries operate on whole request/
  response pairs; mid-stream partial-byte retry is out of scope.
- A `tenacity.RetryError` escaping on an edge path is unwrapped defensively by the story
  `civilized-immediate-mamba` classifier; on the normal exhaustion path `reraise=True`
  surfaces the original exception, which the runner's generic seam handles.
