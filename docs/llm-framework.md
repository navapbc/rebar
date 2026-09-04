# rebar LLM agent-operations framework (`rebar.llm`)

`rebar.llm` is a framework for running **tool-using LLM agents that emit structured
findings**, exposed — like the rest of rebar — over a Python library, the CLI, and
MCP. It ships the plan-review, code-review and spec-scan operations, and the seams to add more
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

- **Library** — each operation (`review_plan`, `review_code`, `scan_epics_for_spec`)
  raises a typed `LLMError` (the `LLMConfigError` subclass) whose message points at
  the extra.
- **CLI** — `rebar review-plan` / `review-code` / `scan-spec` print `Error: …` and exit
  non-zero (`rebar review-plan --check` is an import-free preflight that reports
  availability and always exits 0).
- **MCP** — `review_plan` / `review_code` / `scan_spec` are **gated off** unless
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
`.github/workflows/optionality.yml` — two jobs in the reusable it delegates to:
a **clean-core-wheel** job that installs a built wheel with no extras into a fresh
venv and asserts the heavy stack is *not* importable, and an **optional-extras**
job that installs each extra into its own venv (plus one union venv, which is what
catches a joint `ResolutionImpossible`). The AST import-linter half is no longer a
dedicated job: `tests/unit/test_core_optionality.py` and `tests/unit/test_optional.py`
run in the default suite on every matrix cell, and a guard in
`tests/unit/test_ci_workflow_parity.py` fails the build if either stops being
collected there:

- **`[agents]`** — the LLM agent runtime (`pydantic-ai-slim[anthropic]` +
  `json-repair`, `pydantic>=2`): agent workflow steps, `review_*`, the workflow
  agent runner.
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
 operation (review_plan)                        reviewer registry
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

Models are named in `provider:model` form (e.g. `anthropic:claude-opus-4-8`,
`bedrock:us.anthropic.claude-sonnet-4-6`, `openai-responses:gpt-4o`). The provider can also be
inferred from a bare model name or set explicitly with `REBAR_LLM_MODEL_PROVIDER`.

**rebar owns provider resolution; pydantic-ai is the fallback.** `ProviderSession.provider_factory`
(`src/rebar/llm/providers.py`) is passed to pydantic-ai's `infer_model(provider_factory=...)` hook
and answers in three ordered steps:

1. rebar has its own builder for the name (`anthropic`, `bedrock`, plus `openai` **only when
   `REBAR_LLM_BASE_URL` is set**) → rebar builds the provider;
2. otherwise pydantic-ai recognizes the name → delegate to its `infer_provider`;
3. neither → a typed `LLMConfigError`. An unrecognized provider is a **configuration error**, not
   an outage, so it never surfaces as `LLMUnavailableError`.

A string counts as provider-qualified only when its prefix is a member of
`llm/config.KNOWN_PROVIDER_NAMES` — never by prefix *shape*. This is what keeps a canonical
Bedrock id such as `anthropic.claude-haiku-4-5-20251001-v1:0` (whose colon belongs to the version
suffix) from being mistaken for a `provider:model` split. An explicitly configured provider that
is not a member is rejected outright, naming the valid set.

### Hosted OpenAI defaults to the Responses API

The hosted OpenAI family is named with two provider prefixes that select the **wire protocol**:

| Prefix | Wire protocol | When it is used |
|---|---|---|
| `openai-responses:` | OpenAI **Responses** API (`/v1/responses`) | the **default** for hosted OpenAI — a bare `openai:` qualifier, an inferred OpenAI model (`gpt-4o`), or `REBAR_LLM_MODEL_PROVIDER=openai`, all with **no** custom `base_url` |
| `openai-chat:` | OpenAI **Chat Completions** API (`/v1/chat/completions`) | only when a custom OpenAI-compatible `base_url`/slot `endpoint` is set |

So `model = "gpt-4o"`, `model = "openai:gpt-4o"`, and `model_provider = "openai"` now all resolve
to `openai-responses:gpt-4o`. The two prefixes are capability-equivalent (same `ModelProfile`, same
`native_structured_output`); only the request/response wire shape differs.

**Custom endpoints stay on Chat Completions.** rebar's own OpenAI builder registers only under
`openai`/`openai-chat`, and vendor support for `/v1/responses` behind an arbitrary OpenAI-compatible
`base_url` (LMStudio / Ollama / vLLM / a LiteLLM proxy) is not guaranteed. Whenever a top-level
`REBAR_LLM_BASE_URL`/`base_url` or a per-slot/per-fallback `endpoint` is configured, the hosted
default flip is suppressed and the OpenAI family stays on `openai-chat:` — no deprecation notice is
emitted for that endpoint-forced path.

**Hosted `openai-chat:` was removed before v1.0.0.** Explicitly selecting hosted
`openai-chat:` (with no custom `base_url`) now resolves to `openai-responses:` rather than
logging a deprecation warning or forcing Chat Completions. To exercise Chat Completions, point
at a custom endpoint; hosted OpenAI is Responses-only.

### Provider support tiers

Every provider resolves into one of two tiers, and the tier is **stamped into the gate verdict's
`provider_provenance` record** (see `docs/event-schema.md` §"Provider provenance"):

| Tier | Which providers | What it means |
|---|---|---|
| `first_class` | `anthropic`, `bedrock` | native rebar builder, capabilities derived from the real `ModelProfile`, covered by the unit suite and the provider-parity harness |
| `best_effort` | anything reached via `REBAR_LLM_BASE_URL` (LMStudio / Ollama / vLLM / a LiteLLM proxy), and the five `gateway/*` provider names | an intermediary may have rewritten the request; **rebar cannot vouch for what reached the model** |

Two independent triggers select `best_effort`: a non-empty `base_url`, **or** a `gateway/*`
provider name (those carry no `base_url`, because the gateway URL is resolved inside pydantic-ai).
Everything else — including an unregistered provider name — records `first_class`.

**rebar signs verdicts on every tier.** Refusing to sign an off-tier verdict would break local
development for anyone without a first-class provider key, so a contributor running Ollama can
still exercise every gate; the verdict simply says honestly that rebar cannot vouch for the route.
Three limits an attestation consumer must know: the tier is derived from **configuration-time**
facts and so does not prove where traffic went; it lives in the sidecar payload and is **not part
of the signed bytes**; and `first_class` does **not** currently imply retry/timeout parity —
`REBAR_LLM_RETRY_MAX_ATTEMPTS` and `REBAR_LLM_TIMEOUT` reach the Anthropic path only. The
decision record, including the rejected alternatives, is
[`docs/adr/0059-llm-provider-seam-and-support-tiers.md`](adr/0059-llm-provider-seam-and-support-tiers.md).

### AWS Bedrock

Bedrock is reached **natively**, on the **ambient AWS credential chain** — instance role,
`AWS_PROFILE`, or boto3's default chain. rebar manages no Bedrock credential and has no field for
one, so there is no `REBAR_LLM_*` key to set.

```bash
pip install 'nava-rebar[agents,bedrock]'     # adds pydantic-ai-slim[bedrock] (boto3)
export REBAR_LLM_BEDROCK_REGION=us-east-1    # see the region note below
export REBAR_LLM_FRONTIER_MODEL=bedrock:us.anthropic.claude-opus-4-8
export REBAR_LLM_STANDARD_MODEL=bedrock:us.anthropic.claude-sonnet-4-6
export REBAR_LLM_TRIVIAL_MODEL=bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0
```

Three things bite in practice:

- **Only INFERENCE-PROFILE ids are invokable.** A bare on-demand id such as
  `anthropic.claude-sonnet-4-6` returns
  `ValidationException: Invocation of model ID … with on-demand throughput isn't supported.` Use
  the `us.` / `global.` prefixed form, and take every id **verbatim** from
  `aws bedrock list-inference-profiles --region <region>` — the version suffixes are not uniform
  across models, and `list-foundation-models` does not list profiles at all.
- **A region must resolve.** rebar resolves it as `REBAR_LLM_BEDROCK_REGION` (rebar's own
  knob; the value and its source are recorded in the verdict's provider provenance) >
  `AWS_DEFAULT_REGION` > `AWS_REGION` > the active profile's config, and passes the result
  explicitly to boto3 — so exporting plain `AWS_REGION` works even though botocore alone does
  not consult it (measured on boto3/botocore 1.43.62). Instance-metadata
  reachability supplies **credentials, never a region** — credential and region discovery are
  independent, so a working instance role does not remove this step.

`ANTHROPIC_API_KEY` is **not** required on the Bedrock path, nor for a local OpenAI-compatible
server.

Models are chosen **per model class** (`trivial` / `standard` / `frontier`), so a cheap
model can serve the decisive checks while a frontier model does the open-ended work:

```toml
[tool.rebar.llm.model_classes]
frontier = { model = "openai-responses:gpt-4o" }
standard = { model = "google:gemini-2.5-pro" }
# A per-class `endpoint` points that class's model at a local OpenAI-compatible server; rebar
# routes it through its own builder (no key required) and records `tier=best_effort`.
trivial  = { model = "mlx-community/Qwen3-8B", provider = "openai", endpoint = "http://127.0.0.1:1234/v1" }
```

Equivalently, set a single top-level `base_url` to send **every** class's primary model to
one local server (it also flips the signed tier to `best_effort`):

```toml
[tool.rebar.llm]
base_url = "http://127.0.0.1:1234/v1"
```

The same slots are settable from the environment, one variable per class and field:

```bash
# `frontier` drives the code-review Pass-1 finder (`gates/code-review.yaml`):
REBAR_LLM_FRONTIER_MODEL=openai-responses:gpt-4o rebar review-code
# `standard` drives plan-review, the completion verifier, the code-review verify passes
# and the overlap judge:
REBAR_LLM_STANDARD_MODEL=google:gemini-2.5-pro rebar review-plan <id>
# `trivial` drives ticket enrichment and the epic bug screen. Point it at a local
# OpenAI-compatible server (LMStudio / Ollama / vLLM) with the per-class endpoint:
REBAR_LLM_TRIVIAL_MODEL=mlx-community/Qwen3-8B REBAR_LLM_TRIVIAL_PROVIDER=openai \
  REBAR_LLM_TRIVIAL_ENDPOINT=http://127.0.0.1:1234/v1
# ...or send EVERY class's primary to one local server with the top-level base URL:
REBAR_LLM_BASE_URL=http://127.0.0.1:1234/v1
# (no dummy REBAR_LLM_API_KEY needed — the builder supplies one; such a run records tier=best_effort)
```

A class slot only takes effect for an operation that **declares** that class — the gate
workflows do so with a step-level `model: frontier` / `model: standard`. An operation that
declares none resolves the **top-level** model instead
(`[tool.rebar.llm].model`, else `DEFAULT_MODEL`), so a
per-class variable does not change it. MEASURED: with `REBAR_LLM_FRONTIER_MODEL=openai-responses:gpt-4o`
set and this repo's own `[tool.rebar.llm].model` pinned to Bedrock,
a classless op still ran — and stamped its provenance — as
`bedrock:us.anthropic.claude-opus-4-8`, while `resolve_model(cfg, step="frontier")` returned
`openai-responses:gpt-4o`. Set the top-level `model` (or the matching `REBAR_LLM_<CLASS>_MODEL` for the
class the operation declares) to move a classless operation.

> **`REBAR_LLM_MODEL` was REMOVED** (pre-1.0 breaking pass #3). Setting it now fails loud
> with a migration error rather than being ignored. Use the class slots above, the
> `REBAR_LLM_<CLASS>_MODEL` variables, or the top-level `[tool.rebar.llm].model` key.

### A validated local-model run

Verified end to end against a local OpenAI-compatible server (an MLX model served at
`http://127.0.0.1:1234/v1`, no API key). Install the openai provider group, point the
`trivial` class at the server, and run any operation that declares it — here ticket
enrichment, the highest-volume `trivial` site:

```bash
pip install 'pydantic-ai-slim[openai]'   # the OpenAI-compatible provider group

# Either recipe works — a per-class endpoint, or a top-level base_url:
REBAR_LLM_TRIVIAL_MODEL=<your-local-model> REBAR_LLM_TRIVIAL_PROVIDER=openai \
  REBAR_LLM_TRIVIAL_ENDPOINT=http://127.0.0.1:1234/v1 \
  python -c "import json, rebar.llm.enrich as e; \
print(json.dumps(e.enrich(text='Title: retries lack jitter; add exponential backoff')['digest']))"
```

The call reaches the local server through rebar's builder (no `OPENAI_API_KEY`, no dummy
`REBAR_LLM_API_KEY`), returns a schema-valid `ticket_digest`, and records `tier=best_effort` in
the signed provenance because a local/opaque endpoint was used. Observed abridged output:

```json
{
  "problem_keywords": ["retry", "jitter", "exponential backoff"],
  "component_or_area": "client retry handler",
  "propositions": [
    "Client retries lack jitter",
    "Acceptance requires adding exponential backoff with jitter to retry logic"
  ]
}
```

> A missing provider group fails loudly and by name (`pip install 'pydantic-ai-slim[openai]'`)
> rather than with a bare `ModuleNotFoundError` — the framework's clean-degradation contract.

The `[agents]` extra ships **`pydantic-ai-slim[anthropic,retries]`** (Claude, the default)
out of the box. Bedrock has rebar's own **`[bedrock]`** extra (see above); other providers need
their pydantic-ai slim group
(`pip install 'pydantic-ai-slim[openai]'` for ChatGPT + OpenAI-compatible local
servers, `pydantic-ai-slim[google]` for Gemini) — a missing one raises a clear
error naming the package.

`temperature` is **unset by default**, so the provider's own default applies; set
`REBAR_LLM_TEMPERATURE` to send one. Whether a model accepts it is a resolved **capability**
(`supports_temperature`), recorded in the signed provenance record, because some models reject the
parameter outright — on Bedrock fatally, since only pydantic-ai's Anthropic path drops unsupported
sampling settings for you. Structured output
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
| `REBAR_LLM_MODEL_PROVIDER` | inferred | pydantic-ai provider (`anthropic`/`openai`/`google`/…); inferred from the model string if unset |
| `REBAR_LLM_{FRONTIER,STANDARD,TRIVIAL}_MODEL` | — | the model-class slots (see above); these are the interface |
| `REBAR_LLM_{FRONTIER,STANDARD,TRIVIAL}_PROVIDER` | inferred | per-class provider override; rejected if not in `KNOWN_PROVIDER_NAMES` |
| `REBAR_LLM_{FRONTIER,STANDARD,TRIVIAL}_ENDPOINT` | — | per-class OpenAI-compatible endpoint |
| `REBAR_LLM_BEDROCK_REGION` | — | AWS region for the Bedrock path; first in rebar's own chain `REBAR_LLM_BEDROCK_REGION` > `AWS_DEFAULT_REGION` > `AWS_REGION` > profile (see §"AWS Bedrock") |
| `REBAR_LLM_BASE_URL` | — | OpenAI-compatible endpoint (LMStudio/Ollama/vLLM). **Load-bearing twice over:** it is what registers the `openai` provider builder at all, and it flips the signed verdict tier to `best_effort` |
| `REBAR_LLM_API_KEY` | — | explicit model key (e.g. for a local server). Only valid **with** `REBAR_LLM_BASE_URL` — alone it is a hard `LLMConfigError`. A dummy value is no longer needed; the builder supplies `"not-needed"` itself |
| `REBAR_LLM_CONFIG_FILE` | — | path to a provider-overlay TOML; **outranks the project's `rebar.toml`**, so it is the one-line switch between provider arms |
| `REBAR_LLM_TEMPERATURE` | unset | sampling temperature; unset sends none |
| `REBAR_LLM_MAX_TOKENS` | `16000` | per-response token ceiling |
| `REBAR_LLM_MAX_STEPS` | `250` | agent-loop step cap (~2 per tool call) |
| `REBAR_LLM_TIMEOUT` | `600` | per-call wall-clock seconds (wired to the model's request timeout — see note below). On Bedrock: read + connect timeout on the botocore client |
| `REBAR_LLM_RETRY_MAX_ATTEMPTS` | `4` | transport retry attempts (total, incl. the first). On Bedrock: botocore `retries={"max_attempts": N, "mode": "adaptive"}` |
| `REBAR_LLM_RETRY_MAX_WAIT_S` | `60` | cap on the honored `Retry-After` backoff. **Anthropic path only** |
| `REBAR_LLM_TOOL_TIMEOUT_S` | — | per-tool-call wall-clock bound |
| `REBAR_LLM_ALLOW_LOCAL_PROXY` | off | permit an inherited loopback `ANTHROPIC_BASE_URL` instead of bypassing it |
| `REBAR_LLM_REPO_PATH` | repo root | repo the read-only file tools see |
| `REBAR_LLM_MCP_SERVERS` | `{}` | JSON of MCP servers (pydantic-ai MCP server / toolset shape) |
| `REBAR_LLM_HEADERS` | `{}` | JSON of request headers for gate LLM calls; attached only with `REBAR_LLM_BASE_URL` set (see §"Gateway observability") |
| `ANTHROPIC_API_KEY` | — | model credentials for the **direct-Anthropic** path only; not required for Bedrock or a local server |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | — | OTLP trace sink only (auto-enabled when both keys present + the `[tracing]` extra); never used for prompt text |
| `REBAR_MCP_ALLOW_LLM` | off | gate the billable MCP LLM tools (they make live, billable calls) |

> **`REBAR_LLM_TIMEOUT` wiring & default semantics.** The resolved `timeout_s` is passed
> into the model's request settings (the base `ModelSettings.timeout`, which maps to the
> underlying httpx/Anthropic client per-request timeout), so it actually bounds each LLM
> call rather than being an inert knob. The default (`600` s) equals the Anthropic SDK's
> own default, so leaving it unset never lowers the effective timeout below the SDK floor;
> an explicit operator value is honored verbatim (raise it for very large graph reviews,
> lower it to fail faster). It is a single per-call wall-clock bound and does not itself
> add retry/backoff — but **retry is on by default on the direct-Anthropic path**, as a
> tenacity transport wrapping the httpx client (`REBAR_LLM_RETRY_MAX_ATTEMPTS`, default 4;
> `REBAR_LLM_RETRY_MAX_WAIT_S`, default 60), with the Anthropic SDK's own retries disabled so
> the two do not compound. **On Bedrock** the same two knobs reach the botocore client Config
> (bug 61d8): `REBAR_LLM_RETRY_MAX_ATTEMPTS` maps to `retries={"max_attempts": N, "mode":
> "adaptive"}` (botocore counts total attempts, matching tenacity's `stop_after_attempt(N)`)
> and the timeout maps to both read and connect timeout; only `REBAR_LLM_RETRY_MAX_WAIT_S`
> has no botocore equivalent and stays Anthropic-only.

Tracing is the optional `[tracing]` **OpenTelemetry exporter** to Langfuse's OTLP
endpoint (Langfuse is an OTLP sink, not an SDK dependency) — wired in
`src/rebar/llm/tracing.py` (`setup_tracing`). It is **best-effort / no-op** without
the `[tracing]` extra or the `LANGFUSE_*` keys. The exporter **flushes before
returning** so short-lived CLI processes don't lose spans. Prompt→trace provenance
is by **content hash**: the resolved (git-canonical) prompt's hash is recorded on
the run so a span can be tied back to the exact prompt text. Heavy deps are an
optional extra; a missing extra/credential raises a clear, actionable error.

### The opportunistic enrichment drain runs in three phases

`enrich_drain.drain()` (spawned per ordinary store write by `maybe_drain`, or run inline via
`rebar enrich --drain`) follows the SKIP LOCKED job-queue shape — reserve-short,
process-unlocked, finalize-short, lease recovery — so its advisory drain lock is **never held
across an LLM call** (bug 6148-5d81-8e80-41e8: the earlier lock-across-LLM shape held it for
minutes and made the drain single-flight):

1. **Collect** (under the drain lock, seconds): a concurrency guard (at most 3 distinct
   live-lease drainers may spend LLM $ at once — a 4th skips with
   `{"skipped": "concurrency-cap"}`), the stale-digest self-heal re-enqueues, the pending
   scan, then optimistic per-ticket claims up to the batch cap with a content-hash snapshot
   each. `REBAR_LLM_OVERLAP_DRAIN_BATCH` (default 20) is the claim-window size, clamped to a
   lease-derived bound (`lease_ttl_s // 40` = 22 at the default 15-minute lease) so a run
   cannot outlive its claims.
2. **Enrich** (no lock): the LLM calls. Other drainers keep collecting and processing.
3. **Finalize** (short writes): each snapshot is revalidated — a ticket edited mid-enrichment
   gets no stale digest (the entry is re-enqueued with soak 0, reported as `stale_skipped`);
   otherwise the digest is emitted and the entry marked done. Failures keep their
   dispositions (transient → lease-expiry retry; permanent input rejection → tombstone).

Queue event schema, store write-lock semantics, and the `REBAR_LLM_OVERLAP_DRAIN`
off|async|always door are unchanged; correctness rests on the optimistic claim + lease, not
on the drain lock.

## Using it

```bash
pip install 'nava-rebar[agents]'        # pydantic-ai-slim[anthropic,retries] + json-repair + pydantic
export ANTHROPIC_API_KEY=...            # direct-Anthropic credentials (Bedrock uses the AWS chain)
rebar review-plan --check               # show backend/credential availability
rebar review-plan <ticket-id>           # the plan-review gate; JSON plan_review_verdict on stdout
rebar review-code --base main --head HEAD    # multi-reviewer code review of a git range
rebar review-code --diff-file change.diff -o text   # review a diff file, human output
rebar scan-spec --spec-file spec.md --batch-size 5   # scan open epics against a spec
```

```python
import rebar.llm
result = rebar.llm.review_code(base="main", head="HEAD")
for f in result["findings"]:
    label = "BLOCKING" if f.get("decision") == "block" else f.get("severity", "advisory")
    print(label, f["dimension"], f["detail"])
```

MCP: the `review_code` tool is exposed but **disabled unless
`REBAR_MCP_ALLOW_LLM=1`** (it has cost/network side-effects). It returns a plain
dict (the `review_result` shape) and advertises no `outputSchema` by design.

## Usage log (token spend + optional cost accounting)

`rebar.llm.usage_log` is the opt-in per-call spend sink for the billable CI jobs: when
`REBAR_USAGE_LOG` names a path, the runner appends one JSON object per LLM call (JSONL)
with the op label, the four token fields + request count, the provider-qualified
**model** actually invoked, the inferred **provider**, and a UTC ISO-8601 **timestamp**
(stamped inside `record()`). `python -m rebar.llm.usage_log summarize <path>` folds the
file into a Markdown table (per-op breakdown + totals) for `$GITHUB_STEP_SUMMARY`.

A call made inside a workflow step also carries **`step`** (the step id) and, when that step
declared a model CLASS rather than a literal id, **`model_class`** (`trivial`/`standard`/
`frontier`). Both are omitted — not written as null — when they do not apply, so a row with no
`step` means the call was made outside any step (a spec scan, an enrich pass). They exist because
`op` alone cannot attribute a call: it is the PROMPT name, several steps may share one prompt, and
without the declared class a reader cannot tell "resolved to opus *because* `frontier`" from
"resolved to opus *because* it fell through to `cfg.model`" — the distinction that makes a
per-pass model claim checkable.

**Run shape** (bug aec1). Every row — success and failure alike — also carries the shape of the
agent loop that produced it, reduced from the run's accumulated pydantic-ai messages by the ONE
reducer both outcomes share (`run_shape`, the outcome-neutral alias of `run_shape`):
`tool_calls`, `tool_calls_distinct`, `max_consecutive_repeat`, `top_repeated_tool_calls`
(signature + count, arguments **hashed** so the privacy contract is unchanged), plus the
`request_limit`/`tool_calls_limit` the call ran under and the provider's `finish_reason`. These
exist because the token counters cannot tell a **LOOP** from genuine **BREADTH**: a run that burned
its whole budget looks identical either way. The **distinct-vs-total ratio** separates them —
`tool_calls=125, tool_calls_distinct=1` is an agent spinning on one call; `tool_calls=40,
tool_calls_distinct=38` is an agent exploring — and it reads straight off the durable row, so the
diagnosis costs nothing and needs no re-run of a billable call. Each is written **only when
present**: absent means "not measured", never `0` (which would falsely assert "used no tools").
Rows also carry **`duration_s`** (wall clock for the call — a run can be cheap and still be a
twenty-minute stall) and **`ticket`** (the first element of the request target's `ticket_ids`
list, which makes spend comparable per unit of work rather than only per op — absent when that
list is empty or missing).

**The default gate sink.** `REBAR_USAGE_LOG` still wins outright when set, but with it unset a run
inside a **gate session** (`review-plan`, `verify-completion`, …) now appends to
`<repo root>/.rebar/usage.jsonl` when that `.rebar` directory already exists. Before this, the env
var was the only source, so a normal operator gate run — the billable, agentic, loop-prone run most
worth measuring — recorded nothing and its spend vanished with the process. Read it back with
`python -m rebar.llm.usage_log summarize .rebar/usage.jsonl`, which prints the token/cost table
**and** a `Run shape (loop vs breadth)` section — writing the counts without displaying them
would leave the operator exactly as unable to answer the question, so the retrieval command
renders the discriminator:

```
| op            | calls | tool_calls | distinct | ratio | max_repeat | request_limit | tool_calls_limit |
| plan-reviewer |    12 |        129 |      129 | 1.000 |          1 |           125 |              250 |
| verify        |     1 |        125 |        1 | 0.008 |        125 |           125 |              250 |
```

Both rows are real: the first is a healthy plan-review (every tool call different — breadth),
the second an agent that span on one call until the budget tripped (a loop). An op that made no
tool calls prints `—`, never `0.000`, because no measurement is not a perfect loop. The
gate-session condition is
load-bearing: outside a gate session with the env var unset, `record()` still writes **nothing at
all**, so library use and `make test` never drop JSONL into a checkout; and the directory is never
created, because its presence is what identifies the checkout as a rebar store.

**Lifecycle of `.rebar/usage.jsonl`: append-only, unrotated, and safe to delete at any time.** It is
git-ignored local telemetry owned by the checkout, never read back by rebar itself — only by
`summarize` and by you — so truncating or removing it loses history and breaks nothing. Nothing
prunes it, so it grows with every gate run; delete it when you have finished a measurement, or
point `REBAR_USAGE_LOG` at a per-session path when you want a clean window. (The same
no-rotation caveat already applies to the long-lived review-bot sink configured in
`infra/compose/docker-compose.yml`.) Each row is written with a single append of one complete
line, so concurrent gate runs interleave rows without corrupting one; and the write is
best-effort — a failure is logged and swallowed, never raised into the gate run it measures.

**Est. cost is an optional add-in**: install the `pricing` extra
(`pip install 'nava-rebar[pricing]'` → [genai-prices](https://github.com/pydantic/genai-prices),
pydantic's offline price data with cache read/write tiers and historical prices by
timestamp). With it installed, `summarize` prices each row from that row's own
model/provider/timestamp (multi-model ops sum correctly row by row) and adds an
"est. cost" column plus a per-model rollup table. Pricing never breaks the summary and
never guesses: rows genai-prices cannot price (unknown model → its typed `LookupError`,
rows from the pre-metadata format, or any pricing crash — logged at WARNING) are
excluded and the summary notes "excludes N unpriced calls", **naming each model id that
failed to resolve** so an unknown model (`my-local-model`) is distinguishable from a row
dropped because its id was mis-formatted (bug 2ca9, where a stored `provider:model` id was
passed through verbatim as genai-prices' `model_ref` and silently dropped every Anthropic
and OpenAI row — the qualifier is now removed by registry membership before lookup).
Without the extra, token
totals still print and the cost line reads `unavailable (install rebar[pricing])`.

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
  Add a CLI intercept (like `review-plan`/`reconcile`) and an MCP tool if it should
  be on all three interfaces.

## How the motivating examples map

1. **LLM review of a ticket's plan** — the shipped `review_plan` gate op.
2. **Code review over a change — the four-pass code-review GATE** — the `review_code` op
   (library `rebar.llm.review_code`, CLI `rebar review-code`, gated MCP `review_code`). As of
   epic b744 (WS4, ADR 0011) the throwaway single-pass route — deterministic reviewer selection
   → parallel reviewers → `aggregate_findings` — is RETIRED. `review_code` is now the gate-backed
   shim: an explicit call always runs the four-pass code-review gate (`gates/code-review.yaml`:
   a base reviewer + two-round overlay escalation → kernel Pass-2 verify / Pass-3 decide /
   Pass-4 coach, via `produce_code_review_verdict`) and TRANSLATES the typed
   `code_review_verdict` → a `review_result` (preserving the public surface). The
   `verify.enable_code_review` key (`REBAR_VERIFY_ENABLE_CODE_REVIEW`) gates only automated
   dispatch callers that leave `enabled=None` — never the explicit surface (bug
   5b32-37c4-f99a-4315).
   See `docs/review-kernel.md` (the code-review consumer section) + ADRs 0010/0011.
3. **Scan open epics against a spec (batched)** — the shipped `scan_epics_for_spec`
   op (library, CLI `rebar scan-spec`, gated MCP `scan_spec`). A batch evaluator:
   it pulls the store's open epics, evaluates them against the spec in batches
   (one runner pass each, bounded cost) for coverage gaps / conflicts / overlaps,
   and concatenates + ranks the findings — reusing the same findings contract.

## Code-review project criteria

Repository-owned code-review criteria use the same four-pass gate as the
built-in reviewers, while retaining their repository-effective identity and
routing.

### Runtime contract

#### Round A/B fan-in

Active LLM-backed project criteria are validated and injected exactly once
into the `round_a` finder batch. They are not injected into `round_b`; that
bounded pass remains limited to overlays selected by the Round A escalation
result. A criterion that declares `applies_to` globs in its routing
entry is injected only when one of the review's changed files matches; an empty
or absent `applies_to` means ungated, so it is injected on every review (the
built-in-overlay meaning of an empty list is the opposite — see ADR 0074). Each emitted finding keeps its reviewer prompt provenance and appends
the logical criterion id in the `project.<name>` namespace to its criteria
tags.

#### Effective routing in Pass 3

Pass 3 resolves the finding's logical project id through the repository's
effective routing map. That routing supplies the threshold and posture used
for deterministic blocking, advisory, and nit-suppression decisions, so a
project criterion is decided by its repository configuration rather than by
the physical prompt id.

#### criteria eval resolution

`criteria eval project.<name>` checks the effective plan-review and
code-review registries. An id active in exactly the code-review registry is
calibrated through the code-review prompt and eval-spec arm. An id in neither
registry is unknown; an id active in both is ambiguous and is rejected before
calibration.

### Project dogfood: review-phase-boundaries

`project.review-phase-boundaries` is a project-owned advisory code-review criterion.
Its routing, Pass-1 prompt, and eval corpus live in `.rebar/criteria_routing.json`,
`.rebar/prompts/`, and `.rebar/evals/`, respectively, so the project can dogfood a
repository-specific invariant without changing the shared gate. The finder protects the
boundary that Pass 1 discovers grounded candidates only; Pass 2 independently verifies
atomic validity and impact only; Pass 3 makes deterministic decisions with no LLM, new
evidence, or coaching; and Pass 4 offers non-prescriptive coaching only. It does not make
blocking decisions: its advisory posture leaves Pass 3's normal deterministic routing in
control. The balanced `RP-F1`–`RP-F6` fire and `RP-N1`–`RP-N6` pass corpus covers cross-phase
instructions, descriptive docs, tests/evals/negative examples, grandfathered suggested-fix
context, correct ownership, and ambiguous ownership that must abstain.

#### Configuration error

An activated project criterion with a missing or invalid prompt fails with a
located configuration error instead of being silently skipped. A missing eval
spec likewise reports both the logical criterion id and the expected
code-review eval-spec path.

### Project dogfood: concurrency trigger tiers

The concurrency code-review overlay ships with a **two-layer trigger** design that rebar
itself dogfoods, and that any project can copy.

- **Committed high-precision tier.** `src/rebar/llm/code_review/registry.py` carries a
  literal-substring token list (`_CONCURRENCY_TOKENS`: `Mutex`, `RwLock`, `synchronized`,
  `volatile`, `@GuardedBy`, `pthread_`, `sync.WaitGroup`, `FOR UPDATE`, `SKIP LOCKED`, …) chosen to
  fire only on explicit synchronization primitives across common stacks. It deliberately
  **excludes** noisy vocabulary (`.lock`, `retry`, `Popen`, bare `fork`, async/await) that
  produces false positives, so it stays stack-agnostic and low-noise for every project.
- **Project noisy tier.** A project layers repository-specific vocabulary into the
  `code_review.concurrency` entry of its `.rebar/criteria_routing.json`, using the additive
  `trigger_tokens` (literal substrings unioned into `content_triggered_overlays`) and
  `applies_to` (globs unioned into `glob_triggered_overlays`) keys. Project tokens are unioned
  with — never replace — the committed tier. Because `concurrency` is a **built-in** overlay id,
  the entry must carry **only** those two additive keys: `effective_routing` merges an
  un-prefixed built-in entry per-key over the committed routing (a re-tune), so a stray
  `block_threshold`/`default_posture`/`blocking_enabled` would silently override the committed
  overlay's calibration rather than extend its triggers.

**The fire-rate tradeoff (measured on this repo).** rebar is concurrency-heavy (multi-process
writers over a git-backed store, file locks, detached children, CI push contention), so it is
both the ideal dogfood target and a worst case for over-fire. Measured over the last 300
non-merge `origin/main` commits — by replaying each commit's diff through
`content_triggered_overlays` (committed-only vs. with the noisy tier), a reproducible scan —
the committed high-precision tier fires on **13.3%** of diffs (40/300); adding the noisy tier
(`.lock`, `retry`, `Popen`, `fork`, …) fires on a further **22.0%** (66/300), for 35.3%
combined. That extra noise is **load-bearing here**: three of the four traced corpus races
trigger only through noisy-tier vocabulary (`index.lock` via the `.lock` token, `retry`,
`Popen`), which is exactly why this repo opts into it — in the project overlay, not the
committed list, so other projects are not saddled with rebar's noise.

**This repo's entry (the worked example).** rebar's own `.rebar/criteria_routing.json` carries
a `code_review.concurrency` entry whose `trigger_tokens` add `.lock`, `retry`, `Popen`,
`setsid`, `fork`, `stage_and_commit`, `push_tickets_branch`, `refs/reconciler/`, and whose
`applies_to` adds `**/_store/**`, `**/lock*`, `**/enrich_drain*`, `.github/workflows/**`. A diff
touching `index.lock` — caught by the `.lock` token, which is absent from the committed
high-precision list — fires the overlay under this repo's effective routing but not under
committed-only routing, the same dogfood-by-example pattern as `project.portability` and
`project.review-phase-boundaries` above.

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

### The structured retry layers and their accounting (one bounded operation)

A structured call is **one bounded operation**, not a hand-rolled scheduler. `_pai_structured`
(`rebar.llm.structured_run`) issues **exactly one outer `Agent.run_sync`** per output mode, and
`runner.py` dispatches every `mode="structured"` request to it through the single
`get_runner(...).run(...)` facade — there is no bespoke fresh-`Agent`-per-attempt loop. Three
distinct retry layers stack underneath it, each with its own accounting, and they must not be
conflated:

1. **Transport retry** (the httpx/botocore layer) — resends a *failed HTTP request*
   (`REBAR_LLM_RETRY_MAX_ATTEMPTS`, default 4). It re-establishes the same turn; it does not add
   a model request to the usage tally.
2. **Output repair** (the in-`Agent` bounded retry, `retries={"output": N}`) — when a *completed*
   response fails schema validation or is transient-error/oversize, the Agent re-prompts **inside
   the same `run_sync`**. This adds a model request to the usage tally but **never** a second
   `run_sync`. The allowance N is single-sourced by `output_retry_allowance(req)` =
   `min(OUTPUT_RETRIES, max(0, structured_retry_limit))`; the same N seeds **both** the Agent
   `retries={"output": N}` **and** the matching `UsageLimits` request addend, so the request
   budget and the retry allowance can never drift. **`structured_retry_limit=0` ⇒ allowance 0 ⇒
   single-shot**: zero output-repair retries — the abstain/fail-safe `overlap/judge.py` and the
   `contracts.py` batch depend on. Raising a starved allowance is a **plan-reviewed follow-up**,
   never an ad-hoc bump.
3. **The native→prompted downgrade** (bug 895c) — the *only* sanctioned **second outer
   `run_sync`**. When the provider rejects native structured output at grammar-compile time
   (`translate_schema_complexity_rejection` matches a grammar-rejection phrase), the operation
   falls back from `_run_native_output` to `_run_prompted_output` once. So the outer-run count is
   **exactly one** on a well-formed first response and **exactly two** (native then prompted) only
   on the 895c downgrade — never a third.

**Complete-or-omit history.** A failed intermediate response is projected onto the retry wire
**only when it fits**; an over-budget failed response is **omitted whole**, never truncated, so
the model never sees a half-message it might complete-by-continuation.

**Trace, not usage, for candidates.** Provider *candidates* (fallbacks that were attempted and
discarded) are recorded in the run **trace** for provenance, but their tokens are **not** folded
into the aggregate successful-run usage tally — usage accounts the winning run, the trace
accounts the attempts.

**A mixed-provider fallback chain caches on its PRIMARY (serving) target.** A chain's
capability record is the **conservative intersection** over its candidates
(`runner_support._intersect_capabilities` is the authority) for every field that can hard-fail
cross-provider — any candidate may answer, so `supports_temperature`, native-output routing,
thinking, and web provenance hold only if every candidate has them. `prompt_cache_style` is the
ONE exception (bug `96f3-af59-ba26-4159`): it follows the **primary** candidate. The cache
directive is a set of provider-scoped `ModelSettings` keys (`bedrock_cache_*` vs
`anthropic_cache_*`); `merge_model_settings` (pydantic-ai, which `FallbackModel` applies per
served candidate) is a plain dict union with no validation, and rebar's own run-level assembly
(`structured_run.build_model_settings`, seeded from `cache_settings_for` at `runner.py:556`) is
likewise an unvalidated dict — each provider model then reads ONLY its own keys via `.get(...)`,
so a directive built for the primary is a silent miss — not an error — on a fallback that
actually serves. A Bedrock-primary +
Anthropic-fallback chain (the shipped `frontier`/`standard` shape) therefore caches on the
Bedrock target that serves ~99.6% of calls, and the rare Anthropic fallback runs
uncached-but-correct. (This REVERSES the earlier whole-chain collapse-to-none, whose premise —
that a foreign cache key "would error on the candidate that does not share it" — was disproven at
runtime; see the `_intersect_capabilities` docstring, the `rebar.toml` `[llm.model_classes]`
note, and ADR 0059 §7.) A single-provider chain still reports its own style; only a chain whose
PRIMARY cannot cache reports `prompt_cache_style = "none"` with `cache_read_tokens = 0`.

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
  closed *without* a certified/valid completion signature (e.g. it was `--force`d), the parent
  may still **close** — judged on its **own** criteria — but the verdict carries **`certifiable:
  false`** and the close is **left unsigned** (see the close gate below). The parent is
  complete-enough-to-close but not certifiable, because a descendant's attestation is missing.

It does **not** recurse into grandchildren and does **not** re-verify a *certified* child's own
criteria — that child's certified signature **is** the trusted attestation that its criteria were
validated when it closed. The consequence (and the fix for the count-dependent false-negatives +
step-budget blowups of bug `a254`): the **LLM evaluator is reached once all children are closed**
(signed or not) and it judges only the parent's **own** substantive acceptance criteria (the agent,
against the code) — never child closure. The cost of the child check is independent of child count;
it never re-walks the whole subtree (which is impractical and re-does work the children's own gates
already did). The **close gate** runs the verifier with `graph=False` for exactly this reason (the
standalone `rebar verify-completion <id> --graph` still inlines the subtree for a human review).

**The epic-close bug screen (epics only, ticket 4b54).** The precheck additionally guards an
epic's close against OUT-OF-HIERARCHY bugs in the epic's own deliverable: a deterministic
`caused_by` floor (an open/in_progress bug with a `caused_by` link into the subtree blocks
exactly like an unclosed child, no LLM), then a deterministic candidate filter
(open/in_progress bugs created after the epic's first claim OR linked to the subtree, ceiling
32), then one single-turn TRIVIAL-class call per candidate — the packaged `epic_bug_screen`
prompt emitting the registered `epic_bug_screen_verdict` schema (forced choice A/B/C + one-line
citation; out-of-vocabulary output normalizes to the non-surfacing `C`). A-verdicts are
forwarded (≤8 compact rows) inside the verifier's fenced context; the verifier adjudicates
each via `show_ticket` under its "Unresolved bug candidates" directive (block only an
undispositioned defect-in-deliverable). The screen degrades open on any failure and records
its per-bug tally + unevaluated-overflow count on the completion sidecar
(`epic_bug_screen_v1`). Full protocol: `docs/plan-review-gate.md` §"The epic-close bug
screen". Module: `rebar.llm.epic_bug_screen`; filter + floor: `rebar.llm.completion`.

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
> here. An explicit non-default `[tool.rebar.llm].model` still wins. The untrusted ticket/file content is delimited and
the prompt carries an instruction-hierarchy clause (prompt-injection mitigation, OWASP LLM01).

**Bounded completion recovery.** The ordinary verifier keeps the one-call path
above. If that aggregate call is provider-truncated (`max_tokens`/`length`),
rebar does not retry the same growing history or merely recommend a larger
response cap. It isolates each explicit checklist criterion in a fresh
evidence-gathering history, removes repository tools after 16 agent run steps,
and caps each evidence call at 40 iterations and 4,096 output tokens. Recovery
appends the bug verifier's deterministic “actually resolved” core criterion;
non-bug tickets without explicit checklist criteria cannot be exhaustively
enumerated and fail closed before recovery calls. Recovery
accepts at most 32 criteria, 4,000 characters per criterion, and 32,000 criterion
characters in total. Ticket context is capped at **100,000 characters**, and that cap
must never be smaller than the 32,000-character criteria budget: criteria are extracted
from the description and the description is embedded in the context, so a smaller
context cap would refuse criteria sets the criteria bounds had just accepted (the
32,000-vs-24,000 incoherence fixed in `d59e`). 100,000 covers the largest real tickets
observed — 41,595 and 34,282 characters — with roughly 2.4× headroom, while still
bounding the per-criterion re-send, since recovery sends the whole context once per
criterion (worst case: 32 criteria × this cap).

**Nothing is ever elided.** An over-cap context is refused, not trimmed. Trimming was
implemented and then withdrawn as a signed-false-PASS vector: on an epic the gate
assembles one block per ticket (`assemble_context(graph=True)`), each with its own
`#### Comments` heading, so "drop the oldest comment history" silently deleted whole
child tickets — including their unmet acceptance criteria — while reporting only that
comments had been removed. Elision is unsafe in both directions: dropping evidence that
a criterion *is* met causes a false FAIL, and dropping evidence that it is *not* causes
a false PASS. A refusal is a visible false-block; a bad elision is an invisible signed
false PASS, and this gate signs its verdict. Each compact
evidence record is limited to 12,000 characters, all evidence is limited to
96,000 characters, and the complete finalizer input is limited to 132,000
characters. These deterministic bounds are checked before the corresponding
billable recovery call. One fresh,
tool-free structured turn then merges the compact evidence into the public
`completion_verdict`. Rebar deterministically requires exact coverage of every
expected criterion before accepting it; incomplete evidence, another
truncation, or incomplete finalizer coverage remains a typed fail-closed error
and can never become PASS. A `gate_error_v1` sidecar records the recovery stage,
criterion progress, executable bounds, and request/tool/token/trace metadata
when available. Inspect that diagnostic first: increasing
`REBAR_LLM_MAX_TOKENS` alone does not repair repeated tool-history growth.

**The close gate** (`verify.require_completion_verification_for_close`, default off; **on for
this project**) wires this into `transition` **outside the write lock**, ordering
**verify → close → sign**. It verifies the committed `HEAD` of **whichever checkout the
`transition … closed` command runs from** — an immutable attested snapshot resolved offline,
NOT `origin/main` and NOT necessarily the worktree where the edits were made — so **run the
close from the worktree/branch that contains the code you want verified** (running it from the
main checkout verifies the main checkout's `HEAD`, not your worktree edits):

- on a non-force close it runs `verify_completion`; a **FAIL** verdict, or an **unavailable
  LLM** (missing `[agents]` extra / API key / any verifier error), **blocks** the close
  (fail-closed `CommandError`) with the findings + a `--force` hint;
- on **PASS** it signs the verdict onto the ticket *after* the close is confirmed (so a
  failed/raced close never leaves an orphan certified signature) via `rebar.signing.sign_manifest`
  — **unless the verdict is `certifiable: false`** (a closed-but-uncertified descendant), in which
  case the parent closes but is **left unsigned**;
- **`--force="<reason>"`** closes without verifying or signing. So a **closed-without-
  signature** ticket means "not certified" — either the gate was bypassed (`--force`) *or* a
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

**Trust model.** Each environment signs its own verdicts with its **own auto-generated
Ed25519 key** and the signature is an asymmetric operation certificate (a DSSE envelope
carrying an SSHSIG signature over its PAE bytes), attributed to the **signing environment**
and **verifiable by anyone** against that environment's public key — no shared secret is
needed to verify. In the low-security default an op-cert is a **verifiable process record,
not a control**. A high-security project **pins a required trusted environment's public key
in `.rebar/trusted_environments.yaml`** (on the Gerrit-gated `main` branch) and the **merge
gate** (`rebar verify-opcert`) enforces that the required environment's completion-verifier
certificate exists and verifies over the merged log — so a developer who wants to pass cannot
forge a verdict from an unpinned key. The agent is read-only and never signs its own homework — a
deterministic gate acts on its verdict, and a successful prompt-injection can at worst flip the
*advisory* verdict, never forge the signature. The completion-verification close gate is the sole
close-gate attestation (it signs a PASS verdict *after* the close). An unreadable config is an
**error** — gate resolution raises `ConfigError` and the close fails loudly (operator ruling
39f8-ae7c) — so a broken config can neither auto-enable the gate nor silently skip it.

## See also — reuse reference + the plan-review gate

- **[reuse-surface.md](reuse-surface.md)** — the developer API reference for the
  reusable machinery a new capability builds on: the **signing** surface
  (`rebar.signing`), the **runner + workflow-executor** runtime, the
  **prompt/contract** model, and the **output-schema** seam. Exact signatures +
  return shapes + invariants, for both human and LLM authors.
- **[plan-review-gate.md](plan-review-gate.md)** — the plan-review gate
  (`rebar.llm.review_plan` / `rebar review-plan`; the claim gate): the *inverse* of
  the completion close gate, a worked consumer of all of the above.

### Library contract: `review_plan` signs on PASS by default

`rebar.llm.review_plan(...)` takes **`sign: bool = True`**. On a non-blocking `PASS` it
persists a plan-review attestation, because that attestation — not the returned findings — is
what the claim gate consumes; a library caller that wanted a review and got no signature would
have paid the full model cost for a discarded artifact. Unsigned execution is the explicit
exception:

```python
import rebar.llm
rebar.llm.review_plan(tid)               # PASS -> signs (the default)
rebar.llm.review_plan(tid, sign=False)   # pure read: run the review, sign nothing
```

The signing primitive (`rebar.llm.plan_review.attest.sign_plan_review`) **refuses** every
non-certifiable verdict regardless of caller — a `BLOCK`, an `INDETERMINATE`, or a degraded
run (one carrying a `coverage.resolution_class`) raises `SigningError` rather than minting an
attestation. So `sign=True` is a request, not a guarantee: read `verdict["signature"]["signed"]`
(the boolean of record) rather than testing the `signature` object for presence — it is always
present.

A `PASS` whose signature failed to *persist* is recoverable without paying for the review
again: `rebar.llm.resign_plan_review(tid)` (CLI `rebar sign-review <id>`) re-signs from the
recorded `REVIEW_RESULT` sidecar with **no LLM call**, and refuses if the plan changed since
the review or the recorded verdict was not a signable `PASS`.

## Gateway observability (`llm.headers`)

When gate traffic runs through an OpenAI-compatible gateway (`llm.base_url`) that reports to an
observability backend, `llm.headers` labels it. rebar ships **no gateway vendor's header name**
in code or defaults — the examples below are documentation, not a supported-vendor list, and a
test asserts no vendor header literal exists under `src/rebar/llm/`. Point the keys at whatever
your gateway reads.

**LiteLLM proxy:**

```toml
[tool.rebar.llm.headers]
"x-litellm-trace-id"   = "${run:trace_id}"
"x-litellm-session-id" = "${run:ticket_id}"
```

**Helicone:**

```toml
[tool.rebar.llm.headers]
"Helicone-Session-Id"         = "${run:ticket_id}"
"Helicone-Property-Rebar-Op"  = "${run:operation}"
"Helicone-Auth"               = "${env:HELICONE_KEY}"
```

An internal gateway works the same way — the grammar, not the names, is what rebar provides.
Full key reference (layers, precedence, the closed `${run:…}` vocabulary, the `$$` escape, the
rejected header names) is in [config.md](config.md#llm-framework-llm--optional-agents-extra-toolrebarllm).

**Underscores are stripped by nginx.** Header names containing `_` are dropped by default
(`underscores_in_headers off`), so a gateway whose documented header is the underscore form —
LiteLLM also accepts `langfuse_trace_id` — can silently never receive it when nginx sits in
front. Prefer the hyphenated form, and if a header appears to vanish, test the hop chain before
suspecting rebar.

**Not every provider carries them.** At the pinned pydantic-ai, `extra_headers` is **inert on
Bedrock** — `pydantic_ai/models/bedrock.py` references it zero times, despite `settings.py`
listing Bedrock as supported. rebar's Bedrock path takes no `base_url`, so the `base_url` gate
already excludes it and behaviour is unaffected; but an operator on Bedrock gets no correlation
headers and should not expect them.

**What is recorded.** A signed verdict's `provider_provenance` carries `header_names` — the
sorted **names** of the configured headers. **Values are never recorded**, and never appear in
logs or diagnostics. Put a credential in a header value only via `${env:VAR}`, so it lives in the
environment rather than in a committed config file.

## Deployment notes

- **Langfuse** cloud is low-friction; self-hosting needs Postgres + ClickHouse +
  Redis + S3. Tracing degrades to a no-op when unconfigured.
