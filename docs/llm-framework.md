# rebar LLM agent-operations framework (`rebar.llm`)

`rebar.llm` is a framework for running **tool-using LLM agents that emit structured
findings**, exposed — like the rest of rebar — over a Python library, the CLI, and
MCP. It ships one reference operation (`review_ticket`) and the seams to add more
(code review, spec-vs-epic scans, …) reliably.

It is **optional**: the rebar engine stays stdlib-only (`dependencies = []`), and
nothing here is imported until you actually run an operation. `import rebar` and
even `import rebar.llm` pull no heavy dependency.

## Why this shape (the research-grounded decision)

The design was chosen after a research spike + two independent Opus design reviews
(both *GO-WITH-CHANGES*; their must-fixes are folded in below).

- The agent **tool-use loop is a solved problem** — we do not reimplement one.
- We need the agent to have **filesystem access** (a repo) and **MCP servers** as
  tools, and we want to **configure Langflow and Langfuse** for use across
  environments (some of which can't run Langflow).
- Of the widely-used, actively-maintained agent runtimes, **only LangChain /
  LangGraph is *both* the framework Langflow is built on *and* natively traced by
  Langfuse** (every other framework — CrewAI, LlamaIndex, Pydantic AI, OpenAI
  Agents SDK, Google ADK, the Anthropic Claude Agent SDK — integrates with Langfuse
  only via OpenTelemetry and is unrelated to Langflow). So LangChain/LangGraph is
  the **default in-process substrate** — but kept strictly optional, behind the
  `nava-rebar[agents]` extra, so it is never required by core rebar.

```
 operation (review_ticket)                      reviewer registry
   │  assemble deterministic context              │ catalog.json  (id, dimension,
   │  (rebar reads, sorted, no timestamps)        │   selection rules — local, tested)
   │  resolve reviewer prompt ───────────────────▶│ prompt TEXT ◀── Langfuse prompt mgmt
   │                                              │   (packaged *.md fallback offline)
   ▼
 Runner (pluggable)                              findings contract
   ├── LangGraphRunner  (default, in-process)      review_result.schema.json
   │     create_agent + ToolStrategy structured      finding / citation / severity
   │     output; read-only line-numbered file        ($defs in common.schema.json)
   │     tools + MCP tools; Langfuse callback      ▲
   ├── LangflowRunner   (REST stub; other envs) ───┘ validated + citations resolved
   └── FakeRunner       (offline / tests)
```

## The pluggable runner

A `Runner` takes a `RunRequest` (resolved system prompt + task instructions +
config) and returns a **validated `review_result` dict**. This is the portability
seam:

| Runner | When | Notes |
|--------|------|-------|
| `LangGraphRunner` | default, in-process | `langchain.agents.create_agent` + `ToolStrategy` (robust in-loop structured output; the legacy `create_react_agent(response_format=…)` makes a context-losing post-loop call and is avoided). Tools: read-only, line-numbered repo file tools + MCP via `MultiServerMCPClient`. Tracing: Langfuse callback. Needs `nava-rebar[agents]` + `ANTHROPIC_API_KEY`. |
| `LangflowRunner` | other environments | Documented **stub**. The protocol seam is defined so a hosted Langflow deployment (`POST /api/v1/run/{flow_id}`, header `x-api-key`, body `{"input_value", "input_type":"chat", "output_type":"chat"}`) can be wired without touching the operation layer. When implementing it, note the response text is **deeply nested** (`data["outputs"][0]["outputs"][0]["outputs"]["message"]["message"]`, shape varies by output component) — extraction must walk it defensively. This environment can't run Langflow, so it raises a clear error until configured. |
| `FakeRunner` | offline / tests | Returns canned findings — the dependency-injection seam that makes the whole pipeline (and all three interfaces) testable with no model, network, or extra. |

Select with `REBAR_LLM_RUNNER` (`langgraph` default / `langflow` / `fake`), or pass
an explicit `runner=` to an operation.

## Model providers (not Anthropic-only)

The LangGraph runner builds its model with LangChain's `init_chat_model`, so it is
**provider-agnostic**. The provider is inferred from the model name (`claude-*` →
Anthropic, `gpt-*` → OpenAI, `gemini-*` → Google) or set explicitly with
`REBAR_LLM_MODEL_PROVIDER`:

```bash
REBAR_LLM_MODEL=gpt-4o REBAR_LLM_MODEL_PROVIDER=openai rebar review <id>
REBAR_LLM_MODEL=gemini-2.5-pro REBAR_LLM_MODEL_PROVIDER=google_genai rebar review <id>
# local OpenAI-compatible server (LMStudio / Ollama / vLLM):
REBAR_LLM_MODEL=local-model REBAR_LLM_MODEL_PROVIDER=openai \
  REBAR_LLM_BASE_URL=http://localhost:1234/v1 REBAR_LLM_API_KEY=not-needed rebar review <id>
```

The `[agents]` extra ships only `langchain-anthropic` (the default); other providers
need their integration package (`pip install langchain-openai` /
`langchain-google-genai`) — a missing one raises a clear error. We deliberately
**never send `temperature`** (claude-opus-4.x reject it; other providers use their
default). Structured output uses `ToolStrategy` precisely because it is
provider-*portable* (unlike provider-native strategies). One caveat: `ToolStrategy`
forces tool choice, which Anthropic rejects when **extended thinking** is enabled —
so thinking is left off on the model.

## Findings contract

Every operation returns a **`review_result`** (`src/rebar/schemas/review_result.schema.json`):

```json
{
  "findings": [
    {
      "severity": "high",            // critical | high | medium | low | info
      "dimension": "security",       // category/dimension (reviewer-defined)
      "detail": "…",                 // what + why
      "citations": [                 // file+line / url / freeform
        {"kind": "file", "path": "src/x.py", "line_start": 12, "line_end": 18},
        {"kind": "url", "url": "https://…"},
        {"kind": "source", "description": "ticket acceptance criteria"}
      ]
    }
  ],
  "target": {"kind": "ticket", "ticket_ids": ["…"]},
  "reviewers": ["ticket-quality"], "runner": "langgraph",
  "model": "claude-opus-4-8", "trace_id": null, "summary": "…"
}
```

The schema is the **single source of truth** (`finding`/`citation`/`severity` are
shared `$defs` in `common.schema.json`); the runner's Pydantic structured-output
model mirrors it (pinned by a test). Correctness guarantees:

- **No silent empty reviews.** If the agent returns no structured payload
  (LangChain #36349, a plain-text turn), the runner raises `StructuredOutputError`
  rather than returning zero findings — an empty review must never look "clean."
- **Citations are real.** Every `kind=file` citation is resolved against the actual
  repo; a missing file or out-of-range line is downgraded to a freeform `source`
  note. File tools print `<lineno>: <content>` so the model cites accurately.
- **Read-only, sandboxed.** The agent's file tools are read-only and confined to
  the repo, with `.git` / `.tickets-tracker` / `.bridge_state` denied. No
  write/edit/bash tools in a *review* op.

## Reviewer registry

Reviewer **identity + selection rules** live in a versioned, testable local catalog
(`src/rebar/llm/reviewers/catalog.json`): `id`, `dimension`, `applies_to` globs,
`default`. Reviewer **prompt text** comes from **Langfuse prompt management**
(`get_prompt(name, label="production", fallback=…)`), with a packaged `*.md`
fallback so the framework runs offline / when Langfuse is unconfigured.

`select_reviewers(changed_files)` is the deterministic rule layer (union of every
`default` reviewer and every reviewer whose `applies_to` globs match) — the basis
for the future code-review op's "deterministic reviewer-selection rules."

## Configuration (all env vars optional)

| Var | Default | Purpose |
|-----|---------|---------|
| `REBAR_LLM_RUNNER` | `langgraph` | in-process backend (`langgraph`/`langflow`/`fake`) |
| `REBAR_LLM_MODEL` | `claude-opus-4-8` | model id |
| `REBAR_LLM_MODEL_PROVIDER` | inferred | provider for `init_chat_model` (`anthropic`/`openai`/`google_genai`/…); inferred from the model name if unset |
| `REBAR_LLM_BASE_URL` | — | OpenAI-compatible endpoint (LMStudio/Ollama/vLLM) |
| `REBAR_LLM_API_KEY` | — | explicit model key (e.g. a dummy key for a local server) |
| `REBAR_LLM_MAX_TOKENS` | `8000` | per-response token ceiling |
| `REBAR_LLM_MAX_ITERS` | `25` | agent-loop recursion cap |
| `REBAR_LLM_TIMEOUT` | `600` | per-operation seconds |
| `REBAR_LLM_REPO_PATH` | repo root | repo the read-only file tools see |
| `REBAR_LLM_MCP_SERVERS` | `{}` | JSON of MCP servers (`langchain-mcp-adapters` shape) |
| `ANTHROPIC_API_KEY` | — | model credentials (required to run langgraph) |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | — | tracing + prompts (auto-enabled when both keys present) |
| `LANGFLOW_URL` / `LANGFLOW_API_KEY` | — | Langflow deployment (langflow runner) |
| `REBAR_MCP_ALLOW_LLM` | off | gate the MCP `review_ticket` tool (it makes a live, billable call) |

Langfuse is **no-op unless both keys are set** (gated before the handler is even
constructed — the reliable degradation pattern). The runner wraps each run in a
span and **flushes before returning** so short-lived CLI processes don't lose
traces (the v3 SDK buffers on a background thread). Prompt→trace linkage is
best-effort: Langfuse's first-class linkage attaches `langfuse_prompt` to a
LangChain `PromptTemplate`, but `create_agent` builds messages internally, so the
link may not register in every SDK version. Heavy deps are an optional extra; a
missing extra/credential raises a clear, actionable error.

## Using it

```bash
pip install 'nava-rebar[agents]'        # langchain/langgraph/langfuse/anthropic
export ANTHROPIC_API_KEY=...            # model credentials
rebar review --check                    # show backend/credential availability
rebar review <ticket-id> ticket-quality # JSON review_result on stdout
rebar review <epic-id> --graph -o text  # review an epic + its children, human output
```

```python
import rebar.llm
result = rebar.llm.review_ticket("abc123", "ticket-quality", graph=False)
for f in result["findings"]:
    print(f["severity"], f["dimension"], f["detail"])
```

MCP: the `review_ticket` tool is exposed but **disabled unless
`REBAR_MCP_ALLOW_LLM=1`** (it has cost/network side-effects). It returns a plain
dict (the `review_result` shape) and advertises no `outputSchema` by design.

## Adding an operation or reviewer

- **New reviewer:** add an entry to `reviewers/catalog.json` (+ a packaged `*.md`
  fallback) and create the same-named prompt in Langfuse. `applies_to` globs make
  it eligible for rule-based selection.
- **New operation:** assemble its deterministic context, resolve reviewer prompt(s)
  via `prompts.resolve_prompt`, build a `RunRequest`, and call
  `get_runner(config, override=…).run(req)`. Return a validated `review_result`.
  Add a CLI intercept (like `review`/`reconcile`) and an MCP tool if it should be
  on all three interfaces.

## How the motivating examples map

1. **LLM review of a ticket / ticket-graph** — the shipped `review_ticket` op.
2. **Code review over commits + tickets, deterministic reviewer selection** — a
   future op: `select_reviewers(changed_files)` (the rule layer) → run each reviewer
   as a pass → aggregate findings. The selection rules and finding contract are
   already here; multi-reviewer aggregation (cluster → consensus → rank by
   severity×agreement) is a documented future extension, intentionally not built
   for the single-reviewer milestone.
3. **Scan open epics related to a spec (batched API calls)** — a future op shaped as
   a batch evaluator rather than a single agent loop; it reuses the same findings
   contract and (optionally) a non-agent runner behind the same protocol.

## Deployment notes

- **Langflow** is a heavyweight service (≥2 GB RAM); for constrained hosts use its
  headless `--backend-only` mode or the lightweight `lfx` executor that runs flow
  JSON statelessly. Version-control flows as `flows/*.json`. Langflow wires Langfuse
  natively via `LANGFLOW_LANGFUSE_*` env vars.
- **Langfuse** cloud is low-friction; self-hosting needs Postgres + ClickHouse +
  Redis + S3. Tracing degrades to a no-op when unconfigured.
