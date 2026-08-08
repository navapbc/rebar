# ADR 0065 — The Burr adoption tripwire for the workflow executor

- **Status:** Accepted (epic `b5bc`; ticket `ad16`)
- **Date:** 2026-08-08

## Context

The v3 workflow engine's executor (`llm/workflow/executor.py`) and the v2 worklist interpreter
(`llm/workflow/interpreter.py`) run a validated workflow's steps as a **single in-process,
synchronous, linear pass** in `graphlib.static_order`. Scripted steps dispatch through a
registry; agentic steps through an injected runner; both are seams, so the executor owns control
flow only.

There is a standing temptation, as workflows grow, to reach for a workflow-orchestration
framework (Burr) or to grow a bespoke scheduler here — asyncio, threads, processes, or a retry
library. Doing that silently would turn a thin, cheap, easily-audited pass into an opaque engine.
To prevent that drift, the run state is already modeled as an immutable, copy-on-write `RunState`
(a Burr-style `State`) so that adopting Burr **later** is a swap, not a rewrite, and a **tripwire
test** — `tests/unit/workflow/test_executor_tripwire.py` — reads the executor + interpreter source
and **fails if either imports** `asyncio` / `concurrent` / `concurrent.futures` / `threading` /
`multiprocessing` / `tenacity` / `backoff` / `retrying` / `retry`. The tripwire forces a
**deliberate decision** (adopt Burr, or stay thin) rather than letting the engine accrete
concurrency by accident.

## Decision

Keep the executor + interpreter a **thin linear synchronous pass**, kept honest by the tripwire.
Adopt the Burr framework (do not grow a hand-rolled scheduler) **only when at least one** of the
following adoption triggers becomes TRUE — until then the hand-rolled executor is correct and
cheaper, and the tripwire stays armed:

1. **Durable cross-process PAUSE/RESUME.** Steps need durable pause/resume across processes
   (human-in-the-loop holds that outlive the run), beyond our crash-recovery replay.
2. **Non-linear control flow lands.** Data-dependent branching / looping / fan-out that the static
   DAG cannot express.
3. **Parallel step execution becomes a hard requirement.** Concurrent independent steps, making a
   single linear pass the bottleneck.
4. **Burr's telemetry/UI is wanted as a product surface** rather than our own event log.

None hold today, so the tripwire stays armed. The one narrow, deliberate relaxation is the
bounded-concurrent `map` fan-out (`llm/workflow/map_fanout.py`, story `8d8e`): it is the SOLE
workflow module permitted to import a concurrency library, it is excluded from the tripwire by
name, and it carries its own recorded rationale (order-independent iterations, serialized commits
through `rc.lock`, bounded `max_concurrency`) proven by `test_map_fanout.py`.

## Consequences

- The executor's module docstring keeps a **compressed** form of the trigger list — the literal
  token `burr` plus a four-item `1.`/`2.`/`3.`/`4.` numbered list — because
  `test_executor_tripwire.py::test_executor_documents_the_burr_adoption_path` asserts (tolerant of
  rewording) that the adoption criteria travel with the code. That compressed list cites this ADR
  as its fuller home; the full trigger rationale lives here.
- Adding any banned import to the executor/interpreter fails the tripwire; the correct response is
  to evaluate the four triggers above and either adopt Burr or keep the pass thin — not to silence
  the test.
- Widening the map-fan-out relaxation, or introducing a second concurrency site, reopens this
  decision and must be argued against the triggers here.

## Alternatives rejected

- **Grow a bespoke scheduler in the executor** (asyncio/threads/retry) as needs arise. Rejected:
  it recreates a workflow framework badly and without the audit surface; the tripwire exists
  precisely to stop this.
- **Adopt Burr now, pre-emptively.** Rejected: none of the four triggers hold, so it would add a
  framework dependency and opacity for no present benefit; the `RunState` shape already makes a
  later swap cheap.
