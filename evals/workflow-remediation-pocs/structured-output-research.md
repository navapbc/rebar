# Reliable structured output for LLM-fed deterministic code — research report

**Question:** How do popular, actively-maintained projects in the Pydantic
ecosystem make LLM output reliably conform to a specified JSON Schema, so the
output can feed deterministic downstream code **without** a second "interpreter"
LLM?

**For:** `rebar` — Pydantic AI runtime, provider-agnostic, **Anthropic primary**,
LLM-step output feeds deterministic scripts.

**Date:** June 2026. **Method:** six parallel web-research passes (one per angle),
each capturing falsifiable claims with primary-source citations; the load-bearing
Anthropic facts were re-verified directly against the Claude docs. Vendor-marketing
numbers are flagged as such throughout.

**Bottom line up front:** Yes — the second-interpreter LLM can and should be
eliminated. The community has converged on a *layered, deterministic-where-possible*
stack: **(1)** provider-native constrained decoding where the provider offers it
(now including **Anthropic**, GA since early 2026) → **(2)** deterministic tolerant
parsing of near-miss output (BAML SAP / `json-repair`) → **(3)** Pydantic validation
→ **(4)** a single **bounded retry to the *same* model** with the validation error
fed back. Using a *separate* LLM to interpret/repair output is a recognized
anti-pattern (you can't make a non-deterministic process trustworthy by adding more
non-determinism); a bounded retry to the same generator with a *deterministic*
validator (Pydantic) as the arbiter is the accepted fallback, not the same thing.

---

## 1. Pydantic AI's own reliability mechanisms

**Library health (verified live, 2026-06-21):** `pydantic/pydantic-ai` ~17.9k
stars; latest stable **v1.107.0 (2026-06-10)**; v2.0 in active beta; reached
**v1.0 in Sept 2025** with an API-stability commitment. Very actively maintained
(weekly releases). [docs](https://pydantic.dev/docs/ai/core-concepts/output/),
[v1 announcement](https://pydantic.dev/articles/pydantic-ai-v1).

**Three output modes — conformance strength:**

| Mode | Mechanism | Conformance | Default? |
|---|---|---|---|
| **NativeOutput** | Provider-native Structured Outputs / strict json_schema; "the model is **forced** to only output text matching the provided JSON schema" | **Strongest** where the provider supports it (provider-enforced constrained decoding) | No |
| **ToolOutput** | Schema becomes an output tool's parameter schema; model emits a tool call | Strong + **broadest** ("supported by virtually all models … works very well") | **Yes (default)** |
| **PromptedOutput** | Schema injected into the prompt; framework parses the text (uses provider JSON-mode if any) | **Weakest / best-effort** ("the model is not forced to match the schema") | No |

Docs guidance: "we would generally suggest **starting with tool or native
output**"; Prompted is the fallback for models lacking tool/native support.
NativeOutput is the strongest *hard* guarantee where available; ToolOutput is the
most reliable *in practice across providers*.
[output docs](https://pydantic.dev/docs/ai/core-concepts/output/).

**NativeOutput provider support:** OpenAI ✅, Gemini ✅ (cannot combine tools +
structured output — errors), Groq ✅ (select models). **Anthropic via Pydantic AI:**
the output docs do **not** enumerate Anthropic as a NativeOutput provider; per-model
support is resolved through `pydantic_ai.profiles`. Because Anthropic only shipped a
strict json_schema path in late 2025 (§3), older Pydantic-AI guidance routes
Anthropic through **ToolOutput**. *Confirm the current Anthropic model profile in
source if it matters for rebar — Pydantic AI is adding native Anthropic support as
the Claude feature matures.*

**The retry-on-validation loop (how it actually works):**

1. Model produces output → Pydantic validates against the schema. On a
   `ValidationError`, or on an explicit `ModelRetry(msg)` raised from an
   `@agent.output_validator` (or a tool), Pydantic AI builds a **`RetryPromptPart`**
   and appends it to the message history.
2. `RetryPromptPart.content` carries **either** the structured list of Pydantic
   error details (for `ValidationError`) **or** your string message (for
   `ModelRetry`); its `model_response()` renders the "why retry" text sent back to
   the **same** model.
3. The loop repeats until success or the budget is exhausted, at which point it
   raises **`UnexpectedModelBehavior: Exceeded maximum retries (N) for output
   validation`**.
   [RetryPromptPart](https://pydantic.dev/docs/ai/api/pydantic-ai/messages/),
   [exceptions](https://pydantic.dev/docs/ai/api/pydantic-ai/exceptions/).

**Retry budget — defaults and the API churn (important):**

- **Default budget = 1** for both tools and output (verified in source:
  `_normalize_agent_retries(..., default: int = 1)`). One retry by default —
  production users typically raise it.
- **Current API (v1.x):** a unified `retries=` on `Agent(...)`: `retries=N` sets
  both; `retries={'tools': 3, 'output': 1}` sets each; `agent.run(retries=...)`
  overrides the **output** budget per run.
- **`output_retries=` / `tool_retries=` are DEPRECATED in v1.x (removed in v2.0)**
  in favor of `retries={'output': …, 'tools': …}`. *Much older tutorial content
  still shows `output_retries=` — it is stale.*
- Semantics: in the **tool path**, `output` is the default per-output-tool
  `max_retries` (override per tool via `ToolOutput(MyType, max_retries=2)`); in the
  **text path** (Native/Prompted) it is a global budget shared across output-
  validation retries. (Distinct from transport-level HTTP retries.)
  [agent source](https://raw.githubusercontent.com/pydantic/pydantic-ai/main/pydantic_ai_slim/pydantic_ai/agent/__init__.py).

**Strongest documented pattern:** Tool/Native mode to constrain the request →
Pydantic validates the response → automatic re-ask via `RetryPromptPart` feeds the
error back → custom `@agent.output_validator` + `ModelRetry` for semantic checks →
hard budget terminates in `UnexpectedModelBehavior`.

---

## 2. Instructor (567-labs/instructor, formerly jxnl) — the most popular option

**Health (live, June 2026):** ~13k stars (README "10K+"); latest **v1.15.x
(v1.15.3, 2026-06-15)**; "3M+ monthly downloads", "trusted by over 100,000
developers" (repo's own marketing); actively maintained, now multi-language.
[repo](https://github.com/567-labs/instructor).

**Mechanism:** patch the provider client (idiomatically
`instructor.from_provider("openai/gpt-4o-mini")`), pass `response_model=YourModel`,
get back a validated, typed instance — no manual JSON parsing. The model's schema
becomes the tool/function definition (or `response_format`); **Pydantic validation
is the gate**. Philosophy: "Pydantic is all you need" / "just patch your client."
[patching](https://python.useinstructor.com/concepts/patching/).

**`max_retries` — the reliability core (CONFIRMED):** On validation failure
Instructor **injects the `ValidationError` back into the conversation as a new user
message** and re-asks the same model. The reask docs show it literally:
`messages.append({"role":"user","content": f"Please correct the function call;
errors encountered:\n{e}"})`. Orchestrated by **Tenacity** (the docs page is titled
"Retry Logic with Tenacity"); you can pass an int **or** a Tenacity
`Retrying`/`AsyncRetrying` object to `max_retries` for stop/backoff control
(source-verified: `max_retries: int | AsyncRetrying`; under-documented on the
rendered page). Exhaustion raises `InstructorRetryException` with `n_attempts` /
`failed_attempts`.
[reask](https://python.useinstructor.com/concepts/reask_validation/),
[retrying](https://python.useinstructor.com/concepts/retrying/).

**Validation context + hooks:** pass runtime data via `context={…}` and read it in
validators via `info.context` (e.g., "quote must appear in source text"); failures
feed the same reask loop. `@field_validator` / `@model_validator` integrate
directly. Observability hooks: `completion:kwargs`, `completion:response`,
`completion:error`, `parse:error`, `completion:last_attempt`.
[hooks](https://python.useinstructor.com/concepts/hooks/).

**Modes:** default **`TOOLS`** for OpenAI/Anthropic/Gemini; can use provider-native
*strict* structured outputs via `Mode.TOOLS_STRICT` / `JSON_SCHEMA`; `MD_JSON`
fallback for weak models; Anthropic-specific `ANTHROPIC_TOOLS`/`ANTHROPIC_JSON`.
[mode comparison](https://python.useinstructor.com/modes-comparison/).

**Conformance numbers:** Instructor publishes **no first-party headline %**. Its
stated philosophy is the key caveat for rebar: **"schema-compliant ≠ correct"** —
"a system with perfect schema enforcement … can still be wrong 30% of the time,"
which is exactly why semantic `model_validator` + reask matters even on top of
constrained decoding.
[semantic validation](https://python.useinstructor.com/blog/2025/05/20/understanding-semantic-validation-with-structured-outputs/).

---

## 3. Provider-native structured outputs — which TRULY guarantee the schema

All three major providers now have a **genuine constrained-decoding** path (token
masking, not prompt best-effort). The big 2025→2026 change is **Anthropic**.

| Provider | Mechanism | Hard token-masking? | Published claim | Status |
|---|---|---|---|---|
| **OpenAI** | CFG built from schema; invalid tokens masked at sample time; `strict:true` on `json_schema` and tools | **Yes** | **100%** vs **<40%** (gpt-4-0613) | GA since Aug 2024 |
| **Anthropic Claude** | **Constrained decoding with compiled grammar artifacts**; `output_config.format` `type:"json_schema"` + `strict:true` tools | **Yes** | "guarantee schema-compliant responses through constrained decoding" (except refusals/truncation) | **Beta Nov 14 2025 → GA early 2026** |
| **Google Gemini** | Controlled/constrained decoding; `responseSchema` (OpenAPI subset) / `responseJsonSchema` (full JSON Schema, 2.5+) + `responseMimeType` | **Yes (syntax-level)** | "guarantees syntactically correct JSON" but **not** semantic | Long-standing; full JSON Schema from 2.5 |

**OpenAI:** `response_format:{type:"json_schema", strict:true}` converts the schema
to a context-free grammar and masks invalid tokens per step. Subset limits:
`additionalProperties:false` required everywhere, **all** properties must be
`required` (optional ⇒ union with null), nesting ≤5 levels, value-constraint
keywords (`pattern`/`minimum`/`maximum`/`minLength`/`format`…) not enforced.
[OpenAI](https://openai.com/index/introducing-structured-outputs-in-the-api/).

**Anthropic Claude — verified directly against the docs (load-bearing for rebar):**
- **Historically NONE** — structured output was done via tool use + forced
  `tool_choice` + prefill (tool *emulation*, not constrained decoding). Confirmed.
- **Now GA:** "Structured outputs guarantee schema-compliant responses through
  **constrained decoding**"; "constrained sampling with **compiled grammar
  artifacts**." Two features: **JSON outputs** via `output_config.format`
  (`type:"json_schema"`, schema) and **strict tool use** (`strict:true`), usable
  independently or together. Beta header `structured-outputs-2025-11-13` **no longer
  required** at GA (`output_format` → `output_config.format`).
- **Supported models** include Claude Opus 4.5/4.6/4.7/4.8, Sonnet 4.5/4.6, Haiku
  4.5, Fable 5, Mythos 5 (Claude API; subset on Bedrock/Vertex).
- **JSON Schema subset limits (sharper than OpenAI's):** **no recursive schemas**,
  no external `$ref`, no `minimum`/`maximum`/`multipleOf`/`minLength`/`maxLength`,
  array constraints only `minItems` 0/1, `additionalProperties` **must be false**,
  no regex backreferences/lookaround. Internal `$ref`/`$defs`, `enum`, `const`,
  `anyOf`, `allOf`, and common string `format`s are supported. Hard limits: **20
  strict tools**, 24 optional params, 16 union-typed params, **180s grammar-
  compilation timeout** (first use compiles, cached 24h).
- **Escape hatches that break the "guarantee":** a **refusal**
  (`stop_reason:"refusal"`, HTTP 200) "may not match your schema because the refusal
  message takes precedence"; **`max_tokens` truncation** (`stop_reason:"max_tokens"`)
  yields incomplete/non-matching output. **rebar must handle both even with native
  structured outputs on.**
- **Extended-thinking interaction — the catch:** the structured-outputs docs make
  **zero mention** of extended thinking; secondary/Anthropic framing presents it as
  a **tradeoff**: "Extended thinking mode versus structured outputs is a real
  tradeoff — if your task benefits more from Claude's reasoning than from guaranteed
  schema compliance, stick with extended thinking." Separately, the long-standing
  rule stands: **extended thinking is incompatible with forced tool use** — thinking
  only allows `tool_choice:"auto"`/`"none"`; `"any"` or a forced specific tool
  errors ("Thinking may not be enabled when tool_choice forces tool use"). So the
  *old* force-a-tool structured-output trick cannot run with thinking on. Whether the
  *new native JSON path* composes cleanly with extended thinking is **not documented
  either way** — treat "thinking + native structured output" as **unverified**;
  budget for the tradeoff.
  [Claude structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
  [extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking).

**Gemini:** "syntactically valid JSON matching the provided schema" but "does not
guarantee the values are semantically correct"; no % claim; ignores unsupported
keywords; may reject very large/nested schemas.
[Gemini](https://ai.google.dev/gemini-api/docs/structured-output).

**Common carve-outs across all three:** the grammar guarantees *shape/syntax*, not
value semantics (`minimum`/business rules) — so **Pydantic validation is still
required** — and a **safety refusal can override** the schema.

---

## 4. Constrained / grammar-decoding libraries (token-level guarantees)

These mask logits in the sampling loop, so they require access to that loop — native
fit is **local / open-weight serving**, not closed hosted APIs (unless the provider
implements it server-side, which the three above now do for schema-subset cases).

| Library | Stars | Latest | Mechanism / note |
|---|---|---|---|
| **Outlines** (dottxt-ai) | ~14k | v1.3.0 (2026-05) | FSM/regex-from-schema token masking; original popularizer; **weakest on hard schemas** (coverage collapses to 3–9%); `outlines-core` Rust hot path |
| **XGrammar** (mlc-ai) | ~1.8k | v0.2.2 (2026-06) | CFG via pushdown automaton; near-zero overhead (adaptive mask cache); **default backend for vLLM/SGLang/TensorRT-LLM/MLC**; fastest but most likely to *under-constrain* (silently accept invalid) |
| **llguidance** (guidance-ai, Microsoft) | ~795 | v1.0 (2025-06) | Earley parser over a token trie; ~50µs/token; used in Guidance, vLLM, SGLang, llama.cpp, **Chromium**; **most correct** (fewest invalid acceptances) |

**Where usable:** local engines expose them — vLLM (`guided_json`/`guided_grammar`,
xgrammar default), SGLang, TensorRT-LLM, MLC, **llama.cpp GBNF**, Ollama, TGI.
**Hosted closed APIs:** OpenAI / Gemini / Anthropic do constrained decoding
server-side but only for a **JSON-Schema subset** — you **cannot** ship them an
arbitrary CFG/regex/Lark grammar. The one hosted exception offering arbitrary GBNF
grammars is **Fireworks AI**.
[XGrammar](https://github.com/mlc-ai/xgrammar),
[llguidance](https://github.com/guidance-ai/llguidance),
[Outlines](https://github.com/dottxt-ai/outlines),
[Fireworks](https://docs.fireworks.ai/structured-responses/structured-output-grammar-based).

**For rebar:** not directly applicable — rebar talks to hosted closed models
(Anthropic primary). These libraries are the engine *behind* OpenAI/Anthropic/Gemini
native modes and behind any local-model option; you consume the guarantee via the
provider API, you don't run the library.

---

## 5. BAML — Schema-Aligned Parsing (SAP): deterministic tolerant parsing

**Health (live, 2026-06-21):** `BoundaryML/baml` ~8.4k stars, daily nightly builds,
weekly stable (v0.12.x), Apache-2.0, written mostly in Rust; company **Boundary
(YC W23)**, founder Vaibhav Gupta. Very actively maintained.
[repo](https://github.com/BoundaryML/baml).

**What SAP does:** a single-pass, Rust, **deterministic** parser that treats
extraction as an **edit-distance / "least-cost edit to fit the schema"** problem
(Postel's Law). It fixes: JSON syntax errors (unquoted strings, missing commas,
unescaped newlines), **markdown code fences**, **prose around the JSON ("yapping")**,
type coercion (fraction→float, single value→array), misnamed/superfluous keys, and
**partial/streamed** objects — using the target schema as the guide. Because errors
are corrected *during* the initial parse (<10ms claimed), **no retry round-trip and
no second LLM are needed**; opt-in retries remain for the unrecoverable cases.
[SAP blog](https://boundaryml.com/blog/schema-aligned-parsing).

**Benchmark claim (vendor-run on the independent BFCL dataset, n=1000/model — NOT
independently reproduced):** SAP beats native function-calling and a plain AST
parser across models, e.g. claude-3-haiku FC 57.3% → **SAP 91.7%**; gpt-4o-mini FC
19.8% → **SAP 92.4%**; and "SAP + gpt-3.5-turbo (92%) outperforms function calling
on gpt-4o (87.4%)." Companion post claims "2–4x faster than OpenAI FC-strict" and
that "models perform worse when constrained." Treat the numbers as vendor-reported;
the *design* claims (single-pass, deterministic, no second LLM, any model) are
well-documented and credible.
[SOTA FC](https://boundaryml.com/blog/sota-function-calling).

**Friction for rebar:** BAML is its **own `.baml` DSL** with a codegen step (it
*generates* Pydantic models on the Python side, so it interops, but it is not
pure-Python/Pydantic). Adopting BAML wholesale is a larger commitment than a parsing
helper. The **transferable idea** is cheaper: a deterministic tolerant-parse layer.
The pure-Python analogue is **`json-repair`** (§6) — ~5k stars, drop-in fallback for
`json.loads()` (strip fences, fix commas/quotes/brackets, trim prose), used exactly
as the tolerant layer. Independent third-party validation of SAP's *production*
retry-reduction is weak (advocacy blogs relay BAML's own claims).

---

## 6. The community-converged stack + the second-LLM question + benchmarks

**Is a SECOND LLM to interpret/repair output an anti-pattern? — Yes, for the
parsing/validation layer.** The consistent practitioner argument: you cannot validate
non-determinism with more non-determinism — it **compounds** errors, cost, and
latency (the "validation paradox" / "compounding error cascade"). The cleanest
evidence: the most-cited "constraints hurt accuracy" paper, **"Let Me Speak Freely?"
(EMNLP 2024)**, used a **second LLM (Claude-3-Haiku) as its answer parser**, and
.txt's rebuttal showed a plain **regex beat that LLM parser (61% vs 57%)** — i.e.,
the second-LLM-as-parser was *worse* than deterministic parsing and drove the
paper's negative conclusion.
[LMSF](https://arxiv.org/abs/2408.02442),
[.txt rebuttal](https://blog.dottxt.ai/say-what-you-mean.html).

> **The crucial distinction:** a *separate LLM as parser/validator* = anti-pattern.
> A *single bounded retry to the **same** generator, with a **deterministic**
> validator (Pydantic) as the arbiter and the validation error fed back* = accepted
> best practice. These are not the same thing.

**The consensus layered stack (ranked, defense-in-depth):**

1. **Provider-native strict / constrained decoding** where available — push the
   contract into the decoder (OpenAI, **Anthropic now**, Gemini). Highest validity,
   zero extra calls. Subset-limited; refusals/truncation can still break it.
2. **Deterministic tolerant / schema-aligned parsing** of near-miss output — **BAML
   SAP** or **`json-repair`** (strip fences, fix commas/quotes, trim prose, coerce).
   Recovers most failures with *no* extra model call.
3. **Pydantic validation** — the deterministic gate (and `field_validator` /
   `model_validator` to normalize/coerce, e.g. trim, lowercase enums, clamp ranges).
4. **Validate-then-BOUNDED-RETRY to the same model** with the error fed back
   (Instructor `max_retries` / Pydantic AI `retries`) — the cross-provider safety net
   that needs no native grammar support. Typical counts: **2–3** total attempts
   (`max_retries=2`); Pydantic AI defaults to 1, commonly raised to 2–3.

**2024–2026 validity benchmarks:**
- **OpenAI:** 100% vs <40% (vendor self-eval; pure prompting got ~93%, then
  deterministic constrained decoding to 100%).
- **JSONSchemaBench** (Guidance team, arXiv 2501.10868 — independent/academic):
  native constrained decoding hits ~96–100% compliance on easy/mid schemas vs
  **65–90% unconstrained baseline**, but coverage **degrades sharply on hard
  schemas** (Outlines collapsed to 6% on "GitHub Hard"; Guidance highest overall).
  Two failure classes: over-constrained (rejects valid) and the dangerous
  **under-constrained** (silently accepts invalid — **XGrammar worst, Guidance
  best**). Crucially, constrained decoding **improved** downstream accuracy by up to
  +4% (GSM8K 80.1%→83.8%), contradicting LMSF.
  [JSONSchemaBench](https://arxiv.org/abs/2501.10868).
- **"Let Me Speak Freely?" vs .txt:** the "constraints hurt reasoning" finding is
  real but **narrow and contested** — it hinged on JSON-mode-vs-free-form prompt
  differences and an LLM parser; well-done constrained decoding is neutral-to-positive
  (JSONSchemaBench +4%; .txt re-run structured ≥ unstructured on every task).
- **`json-repair`:** ~5k stars; positioned explicitly as the *fallback* (validate
  with stdlib first, repair only on failure; warns against forcing valid JSON
  through it). Contrast LangChain's `OutputFixingParser`, which uses an *LLM* to fix
  — a deliberate example of the path practitioners now avoid for parsing.
  [json-repair](https://github.com/mangiucugna/json_repair/).

---

## 7. Recommendation for rebar

**rebar can eliminate the second-interpreter LLM.** Adopt a four-layer strategy on
the existing Pydantic AI runtime. Provider-agnostic with **Anthropic primary**, this
maps cleanly because Anthropic now has GA native structured outputs.

**Layer 1 — Provider-native constrained decoding, on by default where supported.**
Use Pydantic AI's **`NativeOutput`** for providers that support it (OpenAI, Gemini,
and — newly — Anthropic). For Anthropic specifically: native structured outputs are
**GA** (`output_config.format` json_schema + `strict:true` tools, constrained
decoding). Verify the current `pydantic_ai.profiles` entry for Anthropic exposes
NativeOutput; if Pydantic AI hasn't wired Anthropic native yet for your version,
keep Anthropic on the default **`ToolOutput`** (strict tool use) as the interim — it
is the strongest broadly-available path and what rebar likely uses today. Either way
the request constrains the model to the schema, so non-conforming output becomes the
rare case, not the norm.

  Watch the documented Anthropic carve-outs and handle them deterministically:
  - **Schema subset:** no recursion, no `minimum`/`maximum`/length/`multipleOf`, ≤20
    strict tools, 180s compile cap. **Keep rebar's step schemas flat and simple** to
    stay inside the grammar; enforce numeric/length bounds in Pydantic (Layer 3), not
    in the JSON Schema.
  - **Escape hatches:** check `stop_reason` — `"refusal"` and `"max_tokens"` can
    return schema-violating output even with native mode on. Treat these as explicit
    failure branches, not parse errors.
  - **Extended thinking:** structured output vs extended thinking is a documented
    *tradeoff*, and thinking is incompatible with *forced* tool use. If a rebar step
    needs deep reasoning **and** strict schema, prefer the **native JSON path** (not
    forced-tool) and verify behavior empirically, or split into a reasoning step
    (thinking, free text) followed by a strict-schema extraction step. Do **not**
    assume thinking + structured output composes silently.

**Layer 2 — Deterministic tolerant parse (cheap insurance, no extra model call).**
Before raising a validation error, run the raw text through a deterministic repair:
add **`json-repair`** (~5k stars, pure-Python, Apache-ish, drop-in
`json.loads()` fallback) to strip markdown fences, fix trailing commas/quotes, and
trim surrounding prose. This recovers most near-miss outputs from any provider with
zero latency and zero extra cost — exactly BAML SAP's value, without adopting the
`.baml` DSL. (Reach for BAML itself only if rebar later wants multi-language clients
or hits SAP-class messiness that `json-repair` can't handle.)

**Layer 3 — Pydantic validation + normalizing validators.** Keep Pydantic models as
the deterministic gate. Move all value-level constraints the grammar can't enforce
(ranges, lengths, patterns, enum normalization, cross-field rules) into
`field_validator` / `model_validator`. Remember Instructor's warning: **schema-valid
≠ correct** — validators are where rebar enforces the semantics its deterministic
downstream actually depends on.

**Layer 4 — Bounded retry to the SAME model with the error fed back (cross-provider
safety net).** Set Pydantic AI **`retries={'output': 2}`** (≈3 attempts;
default of 1 is too tight for production) so a Pydantic `ValidationError` is
re-asked with the structured error via `RetryPromptPart`. Use
`@agent.output_validator` + `ModelRetry(msg)` for semantic re-asks where you can give
the model an actionable correction. This is the accepted use of "send it back to a
model" — **same** model, bounded, deterministic Pydantic as arbiter — and is the
fallback for any provider/step where Layer 1 isn't available or the schema exceeds
the native subset. Note the API: use unified **`retries=`**, not the deprecated
`output_retries=`.

**On the second-interpreter LLM:** retire it. It is a recognized anti-pattern
(compounds non-determinism, cost, latency; the LMSF/.txt episode shows a deterministic
parser beats an LLM parser). Everything it was doing is covered better and
deterministically by Layers 1–4: native constraint makes malformed output rare,
`json-repair` fixes the near-misses, Pydantic gates and normalizes, and a single
bounded same-model retry handles the residue. The only legitimate "second pass to a
model" is Layer 4's bounded retry to the *same* generator — keep that, drop the
separate interpreter.

**Concrete config sketch (Pydantic AI):**
- `output_type=NativeOutput(MyStep)` where the provider profile supports it; else
  `ToolOutput(MyStep)` (Anthropic default today).
- `Agent(..., retries={'output': 2})`.
- A pre-validation hook that runs `json_repair.repair_json()` on raw text before
  Pydantic, for the Prompted/text path and as belt-and-suspenders on tool/native.
- Keep step schemas flat (no recursion, no numeric bounds in JSON Schema); enforce
  bounds + semantics in Pydantic validators.
- Branch on Anthropic `stop_reason in {"refusal","max_tokens"}` as explicit failures.

---

## Confidence flags

- **Anthropic native structured outputs GA + constrained decoding + subset limits +
  refusal/max_tokens caveats** — **High** (verified directly against the Claude
  structured-outputs docs).
- **Thinking + native structured output composability** — **Unverified** (docs are
  silent; framed as a tradeoff). The thinking-vs-forced-tool incompatibility is
  **High**.
- **Pydantic AI defaults (retry=1), deprecation of `output_retries=`, mode
  semantics** — **High** (read from source + docs).
- **Instructor reask-injection + Tenacity + object pass-through** — **High** (docs +
  source-verified).
- **OpenAI 100%/<40%** — vendor self-eval (announcement page 403 to direct fetch;
  quoted via index + corroborating secondaries) — **Medium-High**.
- **BAML BFCL numbers, "<10ms", "2–4x faster"** — vendor-reported on an independent
  dataset, **not** independently reproduced — **Medium**.
- **LMSF exact degradation percentages** — qualitative finding **High**; precise
  numbers **Medium** (fetches disagreed; verify against the PDF before quoting).
- **Star counts / versions** — point-in-time June 2026, drift expected.
