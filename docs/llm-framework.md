# rebar LLM agent-operations framework (`rebar.llm`)

`rebar.llm` is a framework for running **tool-using LLM agents that emit structured
findings**, exposed — like the rest of rebar — over a Python library, the CLI, and
MCP. It ships one reference operation (`review_ticket`) and the seams to add more
(code review, spec-vs-epic scans, …) reliably.

It is **optional**: rebar's core runtime is tiny — its only dependency is `pyyaml`
(the workflow DSL loader) — and **none of the LLM stack is imported until you
actually run an operation**. `import rebar` and even `import rebar.llm` pull no
heavy dependency; the LLM features live entirely behind the `nava-rebar[agents]`
extra, and CI enforces that the core never grows it (see "Optionality is a hard,
validated contract" below).

### Optionality is a hard, validated contract

Optionality holds across **every interface × every operation**, and when the
`agents` extra is absent each surface **degrades cleanly** — never an
`ImportError` traceback, never a silent success:

- **Library** — each operation (`review_ticket`, `review_code`,
  `scan_epics_for_spec`) raises a typed `LLMError` (the `LLMConfigError` subclass)
  whose message points at the extra.
- **CLI** — `rebar review` / `review-code` / `scan-spec` print `Error: …` and exit
  non-zero (`rebar review --check` is an import-free preflight that reports
  availability and always exits 0).
- **MCP** — `review_ticket` / `review_code` / `scan_spec` are **gated off** unless
  `REBAR_MCP_ALLOW_LLM=1`; even when the gate is opened with the extra absent they
  surface the typed error as a tool error, so a default client can never trigger a
  billable call.

Every runner exposes a cheap, offline `preflight()` (import-only, no model/network
call) that the operations invoke **before** their batch loop. This is what makes a
zero-work workload (e.g. a spec-scan over a store with no epics, or a code review
that selects no reviewers) still fail loudly on a missing extra instead of
returning an empty-but-successful result.

The whole matrix is locked down by `tests/interfaces/store/test_llm_optionality.py`
(import-cleanliness per interface + degradation per interface×operation + an
exhaustiveness guard that discovers operations from the public surface), all
runnable offline.

### Extras taxonomy (epic a88f / WS-J)

The optional surface is three extras, each lazy-imported behind
`rebar._optional.guard_import(..., extra=…)` (which raises a clear error naming the
exact `pip install nava-rebar[<extra>]`), and CI-enforced lean by
`.github/workflows/optionality.yml` (an AST import-linter + a clean-core-wheel job
that asserts the heavy stack is *not* importable + per-extra and union jobs):

- **`[agents]`** — the LLM agent runtime (langchain/langgraph + a provider SDK):
  agent workflow steps, `review_*`, the workflow agent runner.
- **`[eval]`** — prompt evaluation (Inspect AI + promptfoo interop).
- **`[tracing]`** — the OTLP trace sink. **Write-only by rule:** OpenTelemetry is a
  *sink*, never read back into a rebar decision (the oracle-discipline rule). The
  `rebar llm setup` wizard configures its endpoint (`--otlp-endpoint` /
  `$OTEL_EXPORTER_OTLP_ENDPOINT`).

Only `pyyaml` is a hard runtime dependency (the workflow DSL loader); everything
else is one of these extras. A scripted-only workflow runs with no extra at all.

## Why this shape (the research-grounded decision)

The design was chosen after a research spike + two independent Opus design reviews
(both *GO-WITH-CHANGES*; their must-fixes are folded in below).

- The agent **tool-use loop is a solved problem** — we do not reimplement one.
- We need the agent to have **filesystem access** (a repo) and **MCP servers** as
  tools, and we want **Langfuse** tracing usable across environments.
- Of the widely-used, actively-maintained agent runtimes, **LangChain / LangGraph
  is natively traced by Langfuse** (every other framework — CrewAI, LlamaIndex,
  Pydantic AI, OpenAI Agents SDK, Google ADK, the Anthropic Claude Agent SDK —
  integrates with Langfuse only via OpenTelemetry). So LangChain/LangGraph is
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
   ├── DeepAgentsRunner (experimental opt-in) ─────┘ validated + citations resolved
   └── FakeRunner       (offline / tests)
```

## The pluggable runner

A `Runner` takes a `RunRequest` (resolved system prompt + task instructions +
config) and returns a **validated `review_result` dict**. This is the portability
seam:

| Runner | When | Notes |
|--------|------|-------|
| `LangGraphRunner` | **default, in-process; the review runner** | `langchain.agents.create_agent` + `ToolStrategy` (robust in-loop structured output; the legacy `create_react_agent(response_format=…)` makes a context-losing post-loop call and is avoided). Tools: read-only, line-numbered repo file tools + MCP via `MultiServerMCPClient`. Tracing: Langfuse callback. Needs `nava-rebar[agents]` + `ANTHROPIC_API_KEY`. |
| `DeepAgentsRunner` | **experimental opt-in** (`REBAR_LLM_EXPERIMENTAL_HARNESS=deepagents`) | Runs on LangChain's [deepagents](https://github.com/langchain-ai/deepagents) harness (planning, subagents, large-result eviction) via `create_deep_agent`, with deepagents' native filesystem over a repo-rooted `FilesystemBackend` made **read-only** by a write-denying `FilesystemPermission`, plus our findings schema (so it still returns a `review_result`). **The review default stays `langgraph`** with our own citation-disciplined tools — this runner is the seam for future deepagents-based task types. Caveat: the rebar state-dir deny-list is enforced on citation *output* here, not on reads (use `langgraph` for read-side deny-listing). |
| `FakeRunner` | offline / tests | Returns canned findings — the dependency-injection seam that makes the whole pipeline (and all three interfaces) testable with no model, network, or extra. |

The runner is **derived** (EV-4): the experimental deepagents harness via
`REBAR_LLM_EXPERIMENTAL_HARNESS=deepagents`; otherwise the `langgraph` default.
`fake` is test-only — pass an explicit `runner=`/`override=` to an operation
(it is off the public env surface).

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

The `[agents]` extra ships both **`langchain-anthropic` (Claude, the default)** and
**`langchain-openai` (ChatGPT + OpenAI-compatible local servers)** out of the box;
other providers need their integration package (`pip install langchain-google-genai`
for Gemini) — a missing one raises a clear error naming the package. We deliberately
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
  the repo, with `.git` / `.tickets-tracker` / `.bridge_state` denied (by realpath,
  so symlinks can't escape the root). No write/edit/bash tools in a *review* op.
  **Deployment caveat:** the deny-list covers internal state, *not* secrets. An
  explicitly named in-repo file is readable even if `.gitignore`'d — discovery
  hides `.gitignore`'d paths, but `read_file` will still return a named `.env` /
  `*.pem` / credentials file (and could quote it in a citation). Run reviews
  against repos that don't contain live secrets, or scrub them first; don't point
  the agent at a working tree holding production credentials.
- **Built for large files/projects** (the patterns SWE-agent / deepagents / Claude
  Code converge on, where windowing is a *correctness* lever, not just cost):
  `read_file` is **windowed** — it returns a capped number of lines and tells the
  model the next `line_start` to page with, and clips overlong (minified/generated)
  lines. `list_directory` / `search_files` **hide vendored/generated and
  `.gitignore`'d paths** (via `git ls-files` + a noise list) and cap their output
  with a "narrow your query" hint — so an explicitly named file is still readable,
  but discovery doesn't drown the agent in `node_modules`/build output.
- **Tool-awareness steering.** The operation's instructions name the tools, tell
  the agent to *use them rather than guess*, how to page large files, and to ground
  every finding in real tool output (cite `path:line`, never invent) — the
  prompt-level reliability technique used by Claude Code / SWE-agent.

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
| `REBAR_LLM_EXPERIMENTAL_HARNESS` | _(unset)_ | set to `deepagents` to opt into the experimental harness; otherwise the runner is the langgraph default. `fake` is library-arg-only. |
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
rebar review-code --base main --head HEAD    # multi-reviewer code review of a git range
rebar review-code --diff-file change.diff -o text   # review a diff file, human output
rebar scan-spec --spec-file spec.md --batch-size 5   # scan open epics against a spec
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

## External-integration suite (live validation)

Tests that hit third-party services live in **`tests/external/`** and are marked
**`external`**. They make real, billable calls, so they are excluded from the
default run (`-m "not integration and not external"`) **and** are inert unless
`REBAR_RUN_EXTERNAL=1` is also set (a second guard against accidental billable
calls — both the env opt-in and credentials are required). The live `rebar.llm`
runner validation (b2e5) and the Langfuse trace round-trip (9bd5) are the current
members. Two ways to run them:

- **CI (recommended):** add an `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`)
  repository secret and dispatch the **`external-integration`** workflow from the
  Actions tab (`.github/workflows/external-integration.yml`). It has two jobs:
  `external` (the live model tests) and `langfuse-trace`. Both **fail** if no key
  secret is set.
- **Locally:** `REBAR_RUN_EXTERNAL=1 ANTHROPIC_API_KEY=… pytest -m external tests/external`
  (needs the `agents` extra). Tests skip when a key/extra is absent.

### Self-hosting Langfuse for the trace round-trip

The `langfuse-trace` job validates that a review run emits a trace **fetchable
back through the Langfuse API** — so it needs a live Langfuse. Per
[research grounded in Langfuse's own SDK CI](https://github.com/langfuse/langfuse-python/blob/main/.github/workflows/ci.yml),
we run an **ephemeral self-hosted stack**, not a persistent server:

- **`docker-compose.langfuse.yml`** — the v3 stack (web + worker + Postgres +
  ClickHouse + Redis + MinIO) pinned to a server version, with **headless
  initialization** baking in a deterministic org/project/user and the keys
  `pk-lf-1234567890` / `sk-lf-1234567890`. So no UI step and **no Langfuse secret**
  is needed in CI — only the model key is a real secret.
- **CI** brings the stack up, waits for `/api/public/health` **and** an
  `auth_check()` (the server reports healthy before headless-init + ClickHouse/MinIO
  migrations finish — budget ~2-3 min), runs `tests/external/test_llm_trace.py`,
  then tears it down. The test polls `GET /api/public/traces/{id}` with a read-retry
  loop because ingestion is async (a trace is queryable a few seconds *after*
  `flush()`).
- **Locally**, the same stack:
  ```bash
  docker compose -f docker-compose.langfuse.yml up -d            # ~2-3 min to Ready
  export LANGFUSE_HOST=http://localhost:3000
  export LANGFUSE_PUBLIC_KEY=pk-lf-1234567890
  export LANGFUSE_SECRET_KEY=sk-lf-1234567890
  REBAR_RUN_EXTERNAL=1 ANTHROPIC_API_KEY=… pytest -m external tests/external/test_llm_trace.py
  docker compose -f docker-compose.langfuse.yml down -v          # tear down + wipe
  ```
  The UI is at `http://localhost:3000` (login `rebar-ci@rebar.local` /
  `rebar-ci-password`). The init keys are **non-secret throwaways for a local
  instance** — never reuse them or expose the instance to a network.

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
2. **Code review over a change with deterministic reviewer selection** — the
   shipped `review_code` op (library `rebar.llm.review_code`, CLI `rebar
   review-code`, gated MCP `review_code`). Diff-centric but agentic: reads the git
   range (or a supplied `diff_text`), selects reviewers deterministically
   (`select_code_reviewers` — `code-quality` always, plus catalog reviewers whose
   `applies_to` globs match the changed files), runs each as its own pass, and
   **aggregates** results (`aggregate.aggregate_findings`: cluster → consensus →
   rank by severity×agreement; each finding carries `agreement` + `reviewers`). A
   lightweight repo "orientation" seeds the changed-file layout (a full
   tree-sitter/PageRank repo-map à la Aider is a future enhancement).
3. **Scan open epics against a spec (batched)** — the shipped `scan_epics_for_spec`
   op (library, CLI `rebar scan-spec`, gated MCP `scan_spec`). A batch evaluator:
   it pulls the store's open epics, evaluates them against the spec in batches
   (one runner pass each, bounded cost) for coverage gaps / conflicts / overlaps,
   and concatenates + ranks the findings — reusing the same findings contract.

## Pluggable output contracts (each operation declares its own shape)

The runner no longer hardcodes the findings model: the **structured-output contract** is
selected per operation by `RunRequest.output_schema` via a small registry
(`rebar.llm.contracts.response_model_for` → a Pydantic model builder; default = findings).
This is what lets a new operation emit a shape other than `review_result` — and it is keyed
by a serializable **name** (not a live type) precisely because `output_schema` is also the
string threaded from workflow DSL steps. A schema-pin test keeps each contract's Pydantic
model in lock-step with its JSON Schema. Add a contract = register a builder +
ship a same-named schema (parallel to "adding a reviewer").

> Structured output uses LangChain `ToolStrategy` (provider-portable). Because that is
> free-generation + code-validation, optional `None`s are dropped (`model_dump(exclude_none=
> True)`) so they don't surface as schema-invalid `null`s. (Migrating to raw-schema/
> AutoStrategy→ProviderStrategy — provider-native, decode-time enforcement — is a tracked
> framework-wide follow-up.)

## Completion verification + the close gate (`verify_completion`)

The shipped `verify_completion` op (library `rebar.llm.verify_completion`, CLI `rebar
verify-completion`, gated MCP `verify_completion`) is the first consumer of the pluggable
contract. The **completion-verifier** reviewer (adapted from the DSO completion-verifier)
answers one question — *"did we build/fix what the ticket requires?"* — verifying every
completion requirement (acceptance/success/close criteria, definitions of done; for **bugs**,
that the bug is resolved) against the implementation. It is read-only: line-numbered repo file
tools + a read-only rebar `show_ticket` tool (passed via `RunRequest.extra_tools`), and emits a
**`completion_verdict`** (`{verdict: PASS|FAIL, findings[]}`) where each FAIL finding cites the
failing `criterion`, an explanation, and a source-code citation. The agent emits the verdict;
the op then deterministically normalizes it and enforces FAIL⇔findings (`_reconcile`) and
resolves citations. Findings are **failures-only** (a completion check, not a code review);
a ticket with no explicit criteria PASSes with a note. Because verification is far more
tool-heavy than a single review, the op raises the agent step budget to a floor (an explicit
higher `REBAR_LLM_MAX_STEPS` still wins). The untrusted ticket/file content is delimited and
the prompt carries an instruction-hierarchy clause (prompt-injection mitigation, OWASP LLM01).

**The close gate** (`verify.require_completion_verification_for_close`, default off; **on for
this project**) wires this into `transition` **outside the write lock**, ordering
**verify → close → sign**:

- on a non-force close it runs `verify_completion`; a **FAIL** verdict, or an **unavailable
  LLM** (missing `[agents]` extra / API key / any verifier error), **blocks** the close
  (fail-closed `CommandError`) with the findings + a `--force-close` hint;
- on **PASS** it signs the verdict onto the ticket *after* the close is confirmed (so a
  failed/raced close never leaves an orphan certified signature) via `rebar.signing.sign_manifest`;
- **`--force-close="<reason>"`** closes without verifying or signing — a **closed-without-
  signature** ticket is the durable signal that validation did not pass / was bypassed.

**Trust model.** The signature is only *secure/meaningful* when rebar runs as the **MCP
server**, whose environment signing key is canonical; a CLI/library install signs with a local
key that **CI** reads as `foreign_key` (intentionally not secure). So CI verifies a closed
ticket's attestation under the MCP server's key; local installs cannot mint a CI-trusted
attestation. The agent is read-only and never signs its own homework — a deterministic gate
acts on its verdict, and a successful prompt-injection can at worst flip the *advisory* verdict,
never forge the signature. The gate is an **alternative** to the signature gate
(`require_signature_for_close`), not composed with it (the completion gate signs *after* the
close; the signature gate requires a signature *before* it — enabling both deadlocks a non-force
close). An unreadable config fails this gate **off** (with a warning), so it never auto-enables
across repos that didn't opt in.

## Deployment notes

- **Langfuse** cloud is low-friction; self-hosting needs Postgres + ClickHouse +
  Redis + S3. Tracing degrades to a no-op when unconfigured.
