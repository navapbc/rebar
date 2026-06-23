# ADR 0001: `execution_mode` is fixed on the prompt

- **Status:** Accepted
- **Context:** Workflow authoring v2 (epic *Workflow authoring v2: contract-bearing
  prompt/step model*), story 4b2f.

## Context

A prompt runs in one of two ways:

- **`agentic`** — a tool-using loop (the pydantic_ai path with filesystem + rebar
  tools), bounded by a step budget; how the review/verifier prompts run today.
- **`single_turn`** — exactly one model call with no tools, returning structured output
  validated against the prompt's `outputs` contract.

The runner needs to know which protocol to use for a given prompt. Two shapes were
considered:

1. **Fixed on the prompt.** `execution_mode` is a closed `{single_turn, agentic}` enum
   in the prompt's front-matter; the prompt declares how it is meant to run.
2. **Swappable per call (a "strategy").** The execution strategy is chosen at the call
   site / step (à la DSPy modules, BAML, or a pydantic_ai strategy object swapped per
   invocation), independent of the prompt.

## Decision

**`execution_mode` is fixed on the prompt** (option 1). A prompt's text is written *for*
a way of running — a single-turn extraction prompt and a tool-using investigation prompt
are not interchangeable bodies. Binding the mode to the prompt:

- keeps the prompt's contract **complete and self-describing**: the typed palette and
  the inspector can show how a prompt runs without consulting the call site;
- lets the runner **dispatch from the resolved prompt alone** — no per-call protocol
  negotiation, no invented strategy plumbing;
- makes `single_turn`'s requirement (a declared `outputs` contract) checkable at author
  time, not discovered at run time.

`execution_mode` is deliberately **distinct from the step-level `mode`**
(`findings`/`structured`/`text`, which shapes output): one is *how the model is driven*
(prompt-level), the other is *how the result is finalized* (step-level). They are
orthogonal.

## Rejected alternative: swappable per-call strategy

A swappable strategy (DSPy/BAML/pydantic_ai-style) is more flexible but buys complexity
this system doesn't need:

- it splits a prompt's identity from its execution, so the same prompt body could be run
  in a mode it wasn't written for — exactly the foot-gun the contract model exists to
  prevent;
- it pushes a protocol decision to every call site, defeating "the resolved prompt is
  enough to dispatch";
- the flexibility is hypothetical here: rebar's prompts each have one correct way to run.

If a future need arises for one prompt body under multiple strategies, that is a new
decision to be recorded as its own ADR — it is not the default.

## Consequences

- The prompt front-matter `execution_mode` enum is closed and defaults to `agentic`
  (back-compat); story afe6's migration stamps it explicitly on every existing prompt.
- The runner branches on `execution_mode`: `single_turn` builds an agent with no
  tools/toolsets (one structured call); `agentic` is the unchanged tool-using path.
- A `single_turn` prompt **must** declare an `outputs` contract (the structured target);
  the dispatch raises a clear error otherwise.
