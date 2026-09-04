# Example LLM configurations (per provider)

Four complete, paste-able `[tool.rebar.llm]` configurations. Each is a **whole** table, not a
fragment — copy one into your `pyproject.toml` and adjust the model ids.

All four configure the three **model classes** (`trivial`, `standard`, `frontier`). Classes exist
because the operations differ in kind: a plan review's Pass-1 is open-ended reasoning, while a
completion verifier's check is a decisive yes/no. Sizing them separately is the point.

> The single bare `REBAR_LLM_MODEL` variable was **removed** (pre-1.0 breaking pass #3) and now
> fails loud with a migration error; use the slots below.
> See [adr/0057-model-classes-and-the-rebar-llm-model-deprecation.md](adr/0057-model-classes-and-the-rebar-llm-model-deprecation.md).

Secrets are **never** put in these tables — an `api_key` on a slot is rejected. Credentials come
from the environment (`ANTHROPIC_API_KEY`, `REBAR_LLM_API_KEY`, or the provider's own variable).

## 1. Anthropic only (the default)

What you get with the `[agents]` extra and nothing else installed.

```toml
[tool.rebar.llm.model_classes]
frontier = { model = "anthropic:claude-opus-4-8" }
standard = { model = "anthropic:claude-sonnet-4-6" }
trivial  = { model = "anthropic:claude-haiku-4-5" }
```

## 2. Bedrock only

Same Claude models through AWS Bedrock. The region comes from `REBAR_LLM_BEDROCK_REGION`, else
boto3's own resolution (`AWS_REGION` / `AWS_DEFAULT_REGION` / the active profile). No region
resolving at all is a hard error rather than a silent default.

```toml
[tool.rebar.llm.model_classes]
frontier = { model = "bedrock:us.anthropic.claude-opus-4-8" }
standard = { model = "bedrock:us.anthropic.claude-sonnet-4-6" }
trivial  = { model = "bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0" }
```

Only **inference-profile** ids (the `us.`/`global.` prefix) are invokable — a bare on-demand
`anthropic.claude-*` id returns a `ValidationException` directing you to a profile. Take each id
**verbatim** from `aws bedrock list-inference-profiles --region <region>`: the suffixes are not
uniform, so a plausible-looking id can simply not exist. MEASURED against account 896586841071 /
`us-east-1` (story 1aa2): `us.anthropic.claude-sonnet-4-6` and
`us.anthropic.claude-haiku-4-5-20251001-v1:0` invoke; `us.anthropic.claude-sonnet-4-6-v1:0` and
`us.anthropic.claude-haiku-4-5-v1:0` raise `ValidationException: The provided model identifier is
invalid`. For a developer machine, prefer pointing `REBAR_LLM_CONFIG_FILE` at a file holding this
table over editing `pyproject.toml` — see
[local-dev-env.md](local-dev-env.md#running-your-local-gates-on-aws-bedrock-the-project-default-and-how-to-opt-out).

## 3. Mixed provider

One provider per class. Each non-Anthropic provider needs its pydantic-ai slim group installed
(`pydantic-ai-slim[openai]`, `pydantic-ai-slim[google]`); a missing one raises an error naming the
package.

```toml
[tool.rebar.llm.model_classes]
frontier = { model = "anthropic:claude-opus-4-8" }
standard = { model = "openai-responses:gpt-4o" }
trivial  = { model = "google:gemini-2.5-flash" }
```

## 4. Local model with a hosted fallback

A local OpenAI-compatible server (LMStudio / Ollama / vLLM) for the cheap class, falling back to a
hosted model when the local endpoint is unreachable. `fallback` entries are provider targets in
the same shape as the slot itself; they must not nest a further `fallback`.

```toml
[tool.rebar.llm.model_classes.frontier]
model = "anthropic:claude-opus-4-8"

[tool.rebar.llm.model_classes.standard]
model = "anthropic:claude-sonnet-4-6"

[tool.rebar.llm.model_classes.trivial]
model    = "local-model"
provider = "openai"
endpoint = "http://localhost:1234/v1"
fallback = [{ model = "anthropic:claude-haiku-4-5" }]
```

Set `REBAR_LLM_API_KEY=not-needed` for a local server that ignores authentication.

## Overriding from the environment

Every slot field has a matching variable — `REBAR_LLM_<CLASS>_MODEL`,
`REBAR_LLM_<CLASS>_PROVIDER`, `REBAR_LLM_<CLASS>_ENDPOINT` — applied **per field**, so overriding
one field never clears its siblings:

```bash
REBAR_LLM_STANDARD_MODEL=openai-responses:gpt-4o rebar review-plan <id>
```

Resolution order, per field: `rebar -c llm.<class>.<field>=…` > `REBAR_LLM_<CLASS>_<FIELD>` >
the config table > the built-in default.
