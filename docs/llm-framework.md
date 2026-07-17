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

- **`[agents]`** — the LLM agent runtime (`pydantic-ai-slim[anthropic]` +
  `json-repair`, `pydantic>=2`): agent workflow steps, `review_*`, the workflow
  agent runner.
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

> **Stack note (d6d1 cutover).** The original runtime was built on
> LangChain/LangGraph (with a deepagents experimental harness); that stack was
> **removed in the d6d1 cutover**. The runtime is now the provider-agnostic,
> in-process **pydantic-ai** runner — this doc describes that current state, so
> don't be confused by the LangChain/LangGraph references in git history. The
> cross-provider runtime was de-risked first by
> [`runtime_pydanticai_poc.py`](experiments/workflow-remediation-pocs/runtime_pydanticai_poc.py)
> (see the [de-risk POC index](experiments/workflow-remediation-pocs/README.md)).

- The agent **tool-use loop is a solved problem** — we do not reimplement one.
- We need the agent to have **filesystem access** (a repo) and **MCP servers** as
  tools, and we want **Langfuse** tracing usable across environments.
- The chosen substrate is the provider-agnostic, in-process **pydantic-ai**
  runtime (`PydanticAIRunner`): it resolves any provider from a `provider:model`
  string, speaks **MCP natively** (no adapter shim), and gives a reliable
  structured-output stack (NativeOutput/PromptedOutput + `json-repair` + bounded
  retry). Tracing is the optional `[tracing]` OpenTelemetry exporter to Langfuse's
  OTLP endpoint (Langfuse is an OTLP sink, not an SDK dependency). The whole
  runtime is kept strictly optional behind the `nava-rebar[agents]` extra, so it
  is never required by core rebar.

```
 operation (review_ticket)                      reviewer registry
   │  assemble deterministic context              │ index.json (DERIVED: id, dimension,
   │  (rebar reads, sorted, no timestamps)        │   selection rules — from front-matter)
   │  resolve reviewer prompt ───────────────────▶│ prompt TEXT (git-canonical:
   │                                              │   .rebar/prompts/<id>.md override
   ▼                                              │   ▸ packaged reviewers/*.md)
 Runner (pluggable)                              findings contract
   ├── PydanticAIRunner (the runtime, in-process)  review_result.schema.json
   │     provider from model string; native          finding / citation / severity
   │     pydantic-ai MCP toolsets; read-only          ($defs in common.schema.json)
   │     line-numbered file tools; structured      ▲
   │     output stack; OTel tracing       ─────────┘ validated + citations resolved
   └── FakeRunner       (offline / tests)
```

## The pluggable runner

A `Runner` takes a `RunRequest` (resolved system prompt + task instructions +
config) and returns a **validated `review_result` dict**. This is the portability
seam:

| Runner | When | Notes |
|--------|------|-------|
| `PydanticAIRunner` | **the runtime, in-process; the review runner** | Provider-agnostic: pydantic-ai resolves the provider from the model string (`provider:model`, e.g. `anthropic:claude-opus-4-8`). Tools: read-only, line-numbered repo file tools + a read-only rebar `show_ticket` tool + MCP via **native pydantic-ai MCP toolsets** (no adapter shim). Structured output via the reliability stack — `NativeOutput`/`PromptedOutput` + `json-repair` + bounded retry. Cost bounded by a `usage_limits` budget. Tracing via the optional `[tracing]` OpenTelemetry exporter. Needs `nava-rebar[agents]` + `ANTHROPIC_API_KEY` (or the relevant provider key). |
| `FakeRunner` | offline / tests | Returns canned findings — the dependency-injection seam that makes the whole pipeline (and all three interfaces) testable with no model, network, or extra. |

`RUNNERS = ("pydantic_ai", "fake")`. The runner is **derived** (EV-4): the
`pydantic_ai` runtime is always the runner — it is not a public env knob. `fake`
is test-only — pass an explicit `runner=`/`override=` to an operation (it is
library-arg-only, off the public env surface).

## Model providers (not Anthropic-only)

The pydantic-ai runner is **provider-agnostic**: pydantic-ai resolves the provider
from the model string in `provider:model` form (e.g. `anthropic:claude-opus-4-8`,
`openai:gpt-4o`, `google:gemini-2.5-pro`). The provider can also be inferred from a
bare model name or set explicitly with `REBAR_LLM_MODEL_PROVIDER`:

```bash
REBAR_LLM_MODEL=openai:gpt-4o rebar review <id>
REBAR_LLM_MODEL=google:gemini-2.5-pro rebar review <id>
# local OpenAI-compatible server (LMStudio / Ollama / vLLM):
REBAR_LLM_MODEL=local-model REBAR_LLM_MODEL_PROVIDER=openai \
  REBAR_LLM_BASE_URL=http://localhost:1234/v1 REBAR_LLM_API_KEY=not-needed rebar review <id>
```

The `[agents]` extra ships **`pydantic-ai-slim[anthropic]`** (Claude, the default)
out of the box; other providers need their pydantic-ai slim group
(`pip install 'pydantic-ai-slim[openai]'` for ChatGPT + OpenAI-compatible local
servers, `pydantic-ai-slim[google]` for Gemini) — a missing one raises a clear
error naming the package. We deliberately **never send `temperature`**
(claude-opus-4.x reject it; other providers use their default). Structured output
uses pydantic-ai's reliability stack (`NativeOutput`/`PromptedOutput` +
`json-repair` + bounded retry), which is provider-*portable*.

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
  "reviewers": ["ticket-quality"], "runner": "pydantic_ai",
  "model": "claude-opus-4-8", "trace_id": null, "summary": "…"
}
```

The schema is the **single source of truth** (`finding`/`citation`/`severity` are
shared `$defs` in `common.schema.json`); the runner's Pydantic structured-output
model mirrors it (pinned by a test). Correctness guarantees:

- **No silent empty reviews.** If the agent returns no structured payload (e.g. a
  plain-text turn), the runner raises `StructuredOutputError` rather than returning
  zero findings — an empty review must never look "clean."
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

Reviewers are now a **subset of prompts**, flagged by an explicit `category: review` in
the prompt front-matter (see [workflow-authoring-v2.md](workflow-authoring-v2.md)).
Reviewer **identity + selection rules** (`id`, `dimension`, `applies_to` globs,
`default`) are **derived** from that front-matter into a generated, committed index
(`src/rebar/llm/reviewers/index.json`; regenerate with `python -m rebar.llm.prompting.prompts
regenerate-index`, enforced by a CI drift gate) — there is no hand-edited catalog.
Reviewer **prompt text is git-canonical** — resolved from the repo, never
from Langfuse: a project override at `.rebar/prompts/<id>.md` wins if present,
otherwise the packaged `reviewers/*.md` shipped with the framework. Langfuse is
**never consulted for prompt text** (it is only an optional trace sink). The
resolved prompt's **content hash is recorded** for trace provenance, so a trace can
be tied back to the exact prompt text that produced it.

`select_reviewers(changed_files)` is the deterministic rule layer (union of every
`default` reviewer and every reviewer whose `applies_to` globs match) — the basis
for the future code-review op's "deterministic reviewer-selection rules."

## Configuration (all env vars optional)

| Var | Default | Purpose |
|-----|---------|---------|
| `REBAR_LLM_MODEL` | `claude-opus-4-8` | model id (or a `provider:model` string) |
| `REBAR_LLM_MODEL_PROVIDER` | inferred | pydantic-ai provider (`anthropic`/`openai`/`google`/…); inferred from the model string if unset |
| `REBAR_LLM_BASE_URL` | — | OpenAI-compatible endpoint (LMStudio/Ollama/vLLM) |
| `REBAR_LLM_API_KEY` | — | explicit model key (e.g. a dummy key for a local server) |
| `REBAR_LLM_MAX_TOKENS` | `8000` | per-response token ceiling |
| `REBAR_LLM_MAX_STEPS` | `50` | agent-loop step cap (~2 per tool call) |
| `REBAR_LLM_TIMEOUT` | `600` | per-call wall-clock seconds (wired to the model's request timeout — see note below) |
| `REBAR_LLM_REPO_PATH` | repo root | repo the read-only file tools see |
| `REBAR_LLM_MCP_SERVERS` | `{}` | JSON of MCP servers (pydantic-ai MCP server / toolset shape) |
| `ANTHROPIC_API_KEY` | — | model credentials (required to run the agent runtime) |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | — | OTLP trace sink only (auto-enabled when both keys present + the `[tracing]` extra); never used for prompt text |
| `REBAR_MCP_ALLOW_LLM` | off | gate the MCP `review_ticket` tool (it makes a live, billable call) |

> **`REBAR_LLM_TIMEOUT` wiring & default semantics.** The resolved `timeout_s` is passed
> into the model's request settings (the base `ModelSettings.timeout`, which maps to the
> underlying httpx/Anthropic client per-request timeout), so it actually bounds each LLM
> call rather than being an inert knob. The default (`600` s) equals the Anthropic SDK's
> own default, so leaving it unset never lowers the effective timeout below the SDK floor;
> an explicit operator value is honored verbatim (raise it for very large graph reviews,
> lower it to fail faster). This does not add retry/backoff — it is a single per-call
> wall-clock bound.

Tracing is the optional `[tracing]` **OpenTelemetry exporter** to Langfuse's OTLP
endpoint (Langfuse is an OTLP sink, not an SDK dependency) — wired in
`src/rebar/llm/tracing.py` (`setup_tracing`). It is **best-effort / no-op** without
the `[tracing]` extra or the `LANGFUSE_*` keys. The exporter **flushes before
returning** so short-lived CLI processes don't lose spans. Prompt→trace provenance
is by **content hash**: the resolved (git-canonical) prompt's hash is recorded on
the run so a span can be tied back to the exact prompt text. Heavy deps are an
optional extra; a missing extra/credential raises a clear, actionable error.

## Using it

```bash
pip install 'nava-rebar[agents]'        # pydantic-ai-slim[anthropic] + json-repair + pydantic
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
  export LANGFUSE_SECRET_KEY=sk-lf-1234567890   # gitleaks:allow — deterministic non-secret local throwaway (matches the docker-compose init default; NOT a real key)
  REBAR_RUN_EXTERNAL=1 ANTHROPIC_API_KEY=… pytest -m external tests/external/test_llm_trace.py
  docker compose -f docker-compose.langfuse.yml down -v          # tear down + wipe
  ```
  The UI is at `http://localhost:3000` (login `rebar-ci@rebar.local` /
  `rebar-ci-password`). The init keys are **non-secret throwaways for a local
  instance** — never reuse them or expose the instance to a network.

## Adding an operation or reviewer

- **New reviewer:** ship a packaged prompt (`reviewers/<id>.md`) whose front-matter
  carries `category: review` plus `dimension` / `applies_to` / `default`, then
  regenerate the derived index (`python -m rebar.llm.prompting.prompts regenerate-index`); a
  project can override it with `.rebar/prompts/<id>.md`. `applies_to` globs make it
  eligible for rule-based selection. (No hand-edited catalog — the index is derived.)
- **New operation:** assemble its deterministic context, resolve reviewer prompt(s)
  via `prompts.resolve_prompt`, build a `RunRequest`, and call
  `get_runner(config, override=…).run(req)`. Return a validated `review_result`.
  Add a CLI intercept (like `review`/`reconcile`) and an MCP tool if it should be
  on all three interfaces.

## How the motivating examples map

1. **LLM review of a ticket / ticket-graph** — the shipped `review_ticket` op.
2. **Code review over a change — the four-pass code-review GATE** — the `review_code` op
   (library `rebar.llm.review_code`, CLI `rebar review-code`, gated MCP `review_code`). As of
   epic b744 (WS4, ADR 0011) the throwaway single-pass route — deterministic reviewer selection
   → parallel reviewers → `aggregate_findings` — is RETIRED. `review_code` is now the gate-backed
   shim: OFF by default (`verify.enable_code_review` / `REBAR_VERIFY_ENABLE_CODE_REVIEW`), it
   returns an inert empty `review_result` when disabled, and when enabled runs the four-pass
   code-review gate (`gates/code-review.yaml`: a base reviewer + two-round overlay escalation →
   kernel Pass-2 verify / Pass-3 decide / Pass-4 coach, via `produce_code_review_verdict`) and
   TRANSLATES the typed `code_review_verdict` → a `review_result` (preserving the public surface).
   See `docs/review-kernel.md` (the code-review consumer section) + ADRs 0010/0011.
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

> Structured output uses pydantic-ai's reliability stack (`NativeOutput`/`PromptedOutput`
> + `json-repair` + bounded retry), which is provider-portable. Optional `None`s are dropped
> (`model_dump(exclude_none=True)`) so they don't surface as schema-invalid `null`s.

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
a ticket with no explicit criteria PASSes with a note.

**Child-closure trust (parents/epics) — a deterministic gate BEFORE the LLM.** Closing a parent
enumerates its **direct** children (via the `parent_id` hierarchy, `list_tickets(parent=…)`) and
splits them two ways, **deterministically and FIRST**, before any LLM call:

- **an unclosed child BLOCKS.** If any direct child is not in a closed state, `verify_completion`
  returns a **FAIL verdict immediately, without ever invoking the LLM** (the verdict's `runner` is
  `"deterministic"`). A parent cannot be complete over open work.
- **a closed-but-uncertified child WITHHOLDS CERTIFICATION, but does not block.** If a child is
  closed *without* a certified/valid completion signature (e.g. it was `--force-close`d), the parent
  may still **close** — judged on its **own** criteria — but the verdict carries **`certifiable:
  false`** and the close is **left unsigned** (see the close gate below). The parent is
  complete-enough-to-close but not certifiable, because a descendant's attestation is missing.

It does **not** recurse into grandchildren and does **not** re-verify a *certified* child's own
criteria — that child's certified signature **is** the trusted attestation that its criteria were
validated when it closed. The consequence (and the fix for the count-dependent false-negatives +
step-budget blowups of bug `a254`): the **LLM evaluator is reached once all children are closed**
(signed or not) and it judges only the parent's **own** substantive success criteria (the agent,
against the code) — never child closure. The cost of the child check is independent of child count;
it never re-walks the whole subtree (which is impractical and re-does work the children's own gates
already did). The **close gate** runs the verifier with `graph=False` for exactly this reason (the
standalone `rebar verify-completion <id> --graph` still inlines the subtree for a human review).

> **Why the verifier uses natural termination, not forced structured output (root cause).**
> Forcing a tool-using agent's output (forced `tool_choice`) makes it **not terminate
> naturally** — it keeps calling exploration tools instead of concluding, so on a code-heavy
> ticket it over-explores for hundreds of steps and trips the budget. (This was first measured
> on the now-removed LangChain `ToolStrategy` runtime, by A/B on the same model/prompt/ticket:
> **>250 tool calls (timeout) with forced output vs ~17 and a clean verdict without it** — a
> Claude-Code sonnet subagent on the same task: ~12 — and the finding carries over to the
> pydantic-ai runtime.) So the verifier runs in `mode="structured"` on the pydantic-ai
> reliability stack: the agent reasons with the read-only tools and produces the verdict via
> NATIVE/PROMPTED structured output (NOT a forced tool_choice that makes a tool-using verifier
> over-explore). This is the proven fix and the field consensus (forcing the loop is the
> documented anti-pattern; a high recursion limit means "you're paying for a loop, fix the
> loop").
>
> The verifier also **defaults to `claude-sonnet-4-6`** — a *decisive* model, not a
> maximally-thorough one: larger/reasoning models *over-explore more* on bounded agentic tasks
> (the documented "overthinking" effect), so escalating to a bigger model is the **wrong** lever
> here. An explicit non-default `REBAR_LLM_MODEL` still wins. The untrusted ticket/file content is delimited and
the prompt carries an instruction-hierarchy clause (prompt-injection mitigation, OWASP LLM01).

**The close gate** (`verify.require_completion_verification_for_close`, default off; **on for
this project**) wires this into `transition` **outside the write lock**, ordering
**verify → close → sign**. It verifies the committed `HEAD` of **whichever checkout the
`transition … closed` command runs from** — an immutable attested snapshot resolved offline,
NOT `origin/main` and NOT necessarily the worktree where the edits were made — so **run the
close from the worktree/branch that contains the code you want verified** (running it from the
main checkout verifies the main checkout's `HEAD`, not your worktree edits):

- on a non-force close it runs `verify_completion`; a **FAIL** verdict, or an **unavailable
  LLM** (missing `[agents]` extra / API key / any verifier error), **blocks** the close
  (fail-closed `CommandError`) with the findings + a `--force-close` hint;
- on **PASS** it signs the verdict onto the ticket *after* the close is confirmed (so a
  failed/raced close never leaves an orphan certified signature) via `rebar.signing.sign_manifest`
  — **unless the verdict is `certifiable: false`** (a closed-but-uncertified descendant), in which
  case the parent closes but is **left unsigned**;
- **`--force-close="<reason>"`** closes without verifying or signing. So a **closed-without-
  signature** ticket means "not certified" — either the gate was bypassed (`--force-close`) *or* a
  descendant is still uncertified; it no longer implies the ticket's **own** validation failed. The
  remedy for the descendant case is to re-close the uncertified child so it earns a signature.

> **AC-authoring rule — never demand child SIGNATURES in a container's acceptance criteria.**
> Because `certifiable: false` is a deliberate soft path (a parent closes *unsigned* when a
> descendant is legitimately force-closed pending operator attestation), an epic/story AC must
> assert the **outcome** ("all child stories closed"; "the work is landed") and **never** the
> gate's own output ("children closed **(signed)**" / any signature demand). A "signed" AC turns
> that soft PASS-but-unsigned path into a **hard FAIL**, blocking the parent's close and erasing
> its certification signal. This is the bf50/5f39 contagion that motivated **bug 02a3**: bf50's
> "all child stories closed (signed)" AC hard-FAILed a fully-landed 19-story epic when child 5f39
> was force-closed for operator attestation. Assert what was *delivered*; let the gate decide
> whether the close is signed. (See the ticket-template guidance in `plan-review-criteria-guide.md`.)

**Trust model.** The signature is only *secure/meaningful* when rebar runs as the **MCP
server**, whose environment signing key is canonical; a CLI/library install signs with a local
key that **CI** reads as `foreign_key` (intentionally not secure). So CI verifies a closed
ticket's attestation under the MCP server's key; local installs cannot mint a CI-trusted
attestation. The agent is read-only and never signs its own homework — a deterministic gate
acts on its verdict, and a successful prompt-injection can at worst flip the *advisory* verdict,
never forge the signature. The completion-verification close gate is the sole close-gate
attestation (it signs a PASS verdict *after* the close). An unreadable config fails this gate **off** (with a warning), so it never auto-enables
across repos that didn't opt in.

## See also — reuse reference + the plan-review gate

- **[reuse-surface.md](reuse-surface.md)** — the developer API reference for the
  reusable machinery a new capability builds on: the **signing** surface
  (`rebar.signing`), the **runner + workflow-executor** runtime, the
  **prompt/contract** model, and the **output-schema** seam. Exact signatures +
  return shapes + invariants, for both human and LLM authors.
- **[plan-review-gate.md](plan-review-gate.md)** — the plan-review gate
  (`rebar.llm.review_plan` / `rebar review-plan`; the claim gate): the *inverse* of
  the completion close gate, a worked consumer of all of the above.

## Deployment notes

- **Langfuse** cloud is low-friction; self-hosting needs Postgres + ClickHouse +
  Redis + S3. Tracing degrades to a no-op when unconfigured.
