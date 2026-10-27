# f6fc live memo+nudge capture (Amazon Bedrock)

**Ticket:** `f6fc-27fe-d3af-4f87` — *Memoize and nudge duplicate read-only tool calls at
the shared agent-call seam.*

This is the durable artifact for f6fc's live-Bedrock acceptance criterion:

> Live Bedrock capture recorded on this ticket: a run demonstrating either a nudge-broken
> loop (duplicate signature followed by a different next call) or a nudge→actuator
> escalation, with usage-log figures, backed by a durable in-repo artifact under
> `docs/experiments/`.

The runnable driver is [`f6fc_live_memo_nudge.py`](./f6fc_live_memo_nudge.py) beside this
file. It exercises the REAL shared agent-call seam (`src/rebar/llm/agent_call.py`, the
module f6fc changes) through a live agentic `PydanticAIRunner` run against Amazon Bedrock
(`bedrock:us.anthropic.claude-sonnet-4-6`).

## What it proves

The four read-only filesystem tools are wrapped with an execution counter that sits
**below** the memo layer, so:

- **guard-observed `tool_calls`** (`result["_usage"]`, the run-shape reducer) counts every
  tool call the model emitted, including duplicates;
- **real executions** (the script's own counter) counts only calls that reached the real
  tool.

A positive delta is direct proof the memo served the duplicate from cache — the model
received the cached file body plus a static nudge instead of re-executing the tool, then
produced its final answer (a **nudge-broken loop**).

The live model emits both instructed reads in a single turn (a parallel batch), which
pydantic-ai executes concurrently. The memo's per-signature `asyncio.Lock` serialises the
check-execute-store section so the batch still executes the wrapped tool exactly once —
satisfying AC #1's "the wrapped tool function runs exactly once for N identical calls".

## Verbatim run

```
$ AWS_REGION=us-east-1 \
  REBAR_LLM_STANDARD_PROVIDER=bedrock \
  REBAR_LLM_STANDARD_MODEL=us.anthropic.claude-sonnet-4-6 \
  REBAR_GATE_ALLOW_UNGATED=1 \
  python docs/experiments/f6fc_live_memo_nudge.py

=== f6fc LIVE memo+nudge experiment ===
model: bedrock:us.anthropic.claude-sonnet-4-6
real read_file executions (below memo): 1
all real tool executions (below memo): {'read_file': 1}
OUTCOME: nudge-broken loop (model answered instead of looping)
  final answer: The function `hello()` in `sample.py` returns the string `'world'`.
  tool_calls (guard-observed): 2
  tool_calls_distinct: 1
  top_repeated_tool_calls: [{'signature': 'read_file:d752e6df', 'count': 2}]
PROOF: 1 duplicate call(s) served from the memo cache with a static nudge — the wrapped tool did NOT re-execute them.
```

### Reading the figures

- `tool_calls (guard-observed): 2` — the model emitted the `read_file` call twice; the
  runaway guard (outermost) observed both signatures.
- `tool_calls_distinct: 1` and `top_repeated_tool_calls: [... count: 2]` — the two calls
  are the SAME signature: a genuine duplicate.
- `real read_file executions (below memo): 1` — the wrapped tool executed only **once**.
- The `2 − 1 = 1` delta is the duplicate the memo served from cache (with the level-1
  nudge appended), so the model stopped repeating and produced its final answer.

## Why this is a session experiment, not a unit test

The deterministic contract is pinned by the unit oracle
[`tests/unit/test_agent_call_memo_nudge.py`](../../tests/unit/test_agent_call_memo_nudge.py)
(memoization, allowlist, nudge escalation + byte-stability, error passthrough, guard
interaction, and the concurrent-batch race this live run first surfaced). This file
records that the same seam behaves correctly against a real frontier model on live
Bedrock, which a `FunctionModel` unit double cannot attest.

### A defect this live run surfaced

The first live run executed the wrapped tool twice for the two identical calls. Two
root causes, both fixed and now covered by the oracle:

1. The memo cache was first stored on the toolset **instance**; pydantic-ai copies a
   toolset per run step while preparing the request, so the cache did not survive — the
   fix moves cache/counts/locks into the enclosing closure, exactly as the runaway guard
   keeps its `signatures` window.
2. pydantic-ai executes a single turn's tool calls **concurrently**, so both coroutines
   checked the cache before either populated it — the fix adds a per-signature
   `asyncio.Lock` around the check-execute-store section
   (`test_parallel_batch_of_identical_calls_executes_wrapped_tool_once`).
