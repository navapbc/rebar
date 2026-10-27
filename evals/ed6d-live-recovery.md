# ed6d — live Bedrock recovery proof (session experiment)

Ticket: `ed6d-5448-4ff6-4b8a` — *Preserve partial evidence on per-criterion
evidence-run exhaustion.*

This is the durable, independently re-runnable session experiment for ed6d's
"Live Bedrock proof before ship" acceptance criterion:

> A real gate-style recovery over multiple criteria where at least one evidence
> run exhausts completes with a verdict instead of failing whole (session
> experiment, not in unit suite).

## What it exercises

The driver [`ed6d_live_recovery.py`](./ed6d_live_recovery.py) invokes the real
`CompletionAgentStep._recover` (`src/rebar/llm/workflow/completion_recovery.py`) —
the exact code path ed6d changes — over a synthetic three-criterion ticket:

- The **primary-exhausted precondition** is injected directly into `_recover`
  (an `LLMBudgetExhaustedError`, the aggregate stop the recovery contract begins
  from).
- The **first criterion's** evidence run is forced to raise
  `LLMBudgetExhaustedError`.
- The **remaining two** evidence runs and the **tool-free finalizer** execute
  **live against Amazon Bedrock** (`bedrock:us.anthropic.claude-sonnet-4-6`).

Before ed6d, the first criterion's exhaustion propagated out of the per-criterion
loop and failed the whole recovery. After ed6d, the exhausted criterion becomes a
content-free placeholder entry and recovery proceeds to the finalizer, returning a
structured `completion_verdict`.

## How to run

```sh
AWS_REGION=us-east-1 \
REBAR_LLM_STANDARD_PROVIDER=bedrock \
REBAR_LLM_STANDARD_MODEL=us.anthropic.claude-sonnet-4-6 \
python docs/experiments/ed6d_live_recovery.py
```

## Recorded run (verbatim stdout)

AWS region `us-east-1`, model `bedrock:us.anthropic.claude-sonnet-4-6`:

```
=== ed6d LIVE recovery result ===
evidence runs total: 3 (1 forced-exhausted, live: 2 )
finalizer live calls: 1
verdict keys: ['_usage', 'criteria', 'findings', 'model', 'provider_provenance', 'runner', 'summary', 'trace_id', 'verdict']
verdict: FAIL
verdict criteria count: 3
STEP STATUS: succeeded
```

## Interpretation

`STEP STATUS: succeeded` with a `completion_verdict` carrying all three criteria
proves recovery **completed with a verdict** despite one evidence run exhausting —
it did not raise `CompletionRecoveryError` and fail whole. The verdict is `FAIL`
(not an error) because the exhausted criterion carries the content-free
"could-not-gather-within-bounds" placeholder, which cannot be read as affirmative
evidence — exactly the intended, honest outcome.

### Independent production corroboration

The live completion-verifier closes of both `8eb3` (the context-ceiling sibling)
and this ticket independently exercised the same path end-to-end: each close's
**primary** agentic verification hit a real runaway tool-call loop that the c827
loop-breaker aborted (`8eb3`: requests=97, tool_calls=112, max_consecutive_repeat=12;
`ed6d`: requests≈87, tool_calls≈90), after which this bounded-recovery path took
over and produced a structured verdict against Bedrock. The `8eb3` close recovered
to a PASS and closed the ticket.
