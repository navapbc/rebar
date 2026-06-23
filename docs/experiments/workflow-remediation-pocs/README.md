# Workflow-engine-v2 de-risk POCs

The three throwaway proof-of-concepts that de-risked the riskiest parts of the
workflow-engine-v2 epic (`hump-seam-spice`) **before** committing to the design. They are
**reference artifacts, not shipped deliverables** — each proved one hard assumption, and the
production implementation (cited below) supersedes it.

- **[`engine_interpreter_poc.py`](engine_interpreter_poc.py)** — the *deepest* risk: that a
  thin interpreter over the event log can resume **exactly-once across every crash point**.
  Proved replay correctness (and found+fixed one replay bug). Shipped as
  `src/rebar/llm/workflow/interpreter.py` (frame-key `(run_id, step_id, iteration)` markers);
  see also `tests/unit/workflow/test_interpreter_v2.py`.
- **[`runtime_pydanticai_poc.py`](runtime_pydanticai_poc.py)** — that a provider-agnostic
  **Pydantic AI** runtime works across providers, including the Claude thinking +
  structured-output (the forced-tool 400) corner. De-risked the LangGraph→PydanticAI cutover
  (story d6d1). Shipped as the `PydanticAIRunner` in `src/rebar/llm/runner.py`; the
  structured-output reliability stack is companioned by
  [`structured-output-research.md`](structured-output-research.md) and `src/rebar/llm/structured.py`.
- **[`visual_bpmn_roundtrip_poc.mjs`](visual_bpmn_roundtrip_poc.mjs)** — that an IR↔BPMN
  projection round-trips losslessly (ids / multi-instance / agent metadata survive a real
  bpmn-io parse via a registered moddle descriptor). Shipped as
  `src/rebar/llm/workflow/bpmn.py` + the editor; see [docs/workflow-editor.md](../../workflow-editor.md).
