# ADR 0059 — The LLM provider seam and the two-tier provider model

**Status:** Accepted
**Date:** 2026-08-03
**Epic:** `061c-ecd1-8967-4a76` — *LLM provider seam — first-class Bedrock + best-effort
OpenAI-compatible, with tiered verdict provenance*. Story S9 `8625-7bea-67db-4cdc`.
**Relation:** SIBLING of [`0057-model-classes-and-the-rebar-llm-model-deprecation.md`](0057-model-classes-and-the-rebar-llm-model-deprecation.md)
(the model-class vocabulary this seam resolves through) and
[`0056-decompose-pydantic-ai-runner-run.md`](0056-decompose-pydantic-ai-runner-run.md) (the runner
decomposition that gave the seam its call site). Neither is amended. This ADR narrows one claim
the epic made about what "first-class" guarantees — see §"What `first_class` does NOT yet mean".

> **Numbering caution.** `docs/adr/` contains DUPLICATE numbers from concurrent branches —
> three files carry `0035`, and `0007`, `0008`–`0012`, `0026`, `0031`, `0032`, `0035`–`0037` are
> each claimed twice or more. A bare "ADR 0035" or "ADR 0037" is ambiguous in this repo. Every
> cross-reference below cites the **filename**.

> **Citation convention, chosen because of a measured problem in this repo.** Anchors below name
> a **symbol or section** wherever the sentence reads naturally either way, and a `file:line`
> only where the line itself is the evidence. Line anchors rot fast here: re-verifying this
> document's own references at authoring time found live rot in nine of them, including two that
> had moved the same day. `scripts/check_comment_hygiene.py` scans **Python only**, so a rotted
> anchor inside a Markdown file is invisible to CI — a reader must re-check before quoting.
> All line numbers below were verified against the working tree at commit `9af88c3ada`.

---

## Context

rebar's LLM runtime was provider-agnostic in principle and Anthropic-shaped in practice. The
runner branched on `resolved.startswith("anthropic")` to decide whether to build a retrying
transport and whether to request prompt caching; the structured-output mode came from a
hard-coded `_NATIVE_OUTPUT_PROVIDERS` name set; and `_pai_check_config` REFUSED any
`base_url`/`api_key` outright — while `docs/llm-framework.md` already documented both as
working. The documentation was false, and every new provider request arrived as a one-off
argument about where the branch should go.

Two concrete requests forced the question. GitHub PR #121 proposed reaching AWS Bedrock by
fronting it with an OpenAI-compatible gateway. PR #120 and the local-server recipe wanted
LMStudio/Ollama/vLLM to work. Those are not the same request: one is infrastructure a client
already runs under contract, the other is an unaccredited endpoint a contributor points rebar
at. Treating them identically forces a choice between refusing the second (which breaks the
OSS on-ramp `docs/plan-review-gate.md` depends on) and vouching for it (which puts an
unverifiable claim into a **signed** gate verdict).

**The signing constraint is what makes this an architecture question rather than a plumbing
one.** rebar's three gates sign their verdicts. Before this seam a verdict recorded only a model
STRING, so a run behind an opaque intermediary still claimed it came from
`anthropic:claude-opus-4-8`. A gateway-fronted Bedrock deployment would have recorded a model
name that does not name what actually answered.

### A note on the premise this epic was justified by

The epic's stated motivation is that rebar's clients "will reach Claude through AWS Bedrock more
often than through the direct Anthropic API". **No survey, count, or citation for that claim
exists anywhere in the epic's ~40 tickets.** It is recorded here as the stated motivation, not
as an established fact, because a later reader deciding whether to extend the first-class tier
should know the tier's founding premise is unevidenced.

---

## Decision

### 1. The seam is pydantic-ai's `provider_factory` hook, owned by a per-run session object

`ProviderSession.provider_factory` (`src/rebar/llm/providers.py:132-176`) is the single
`(str) -> Provider` answer to "how is a provider built for provider X", including when the
answer is "it isn't". It is passed to `infer_model(provider_factory=...)`
(`runner.py:350`, and once more for the single-candidate path at `runner.py:354`). Upstream
directs user-configurable-provider applications to exactly this hook (pydantic-ai#1343) rather
than to patching `infer_provider`, so rebar rides a supported contract instead of a private one.

Resolution is three ordered steps, and the ORDER is the decision:

1. a rebar builder is registered for the name (`providers.py:106-109`: `anthropic`, `bedrock`;
   plus `openai` conditionally at `:117-118`) → run it;
2. otherwise, if pydantic-ai itself recognizes the name → delegate to its own
   `infer_provider` (`providers.py:168-172`);
3. neither → a typed `LLMConfigError` (`providers.py:176`), never `LLMUnavailableError`. A
   misspelled provider name is an operator configuration error, and reporting it as an outage
   would tell the operator the provider was reached and failed — the opposite of what happened.

It is a per-run **session object**, not a bare function, because the hook's return type carries
a `Provider` only, with no channel for the retrying `httpx.AsyncClient` the Anthropic path
opens. The session owns `_closeables` (`providers.py:102`) and closes them in
`ProviderSession.close`, reached through `__enter__`/`__exit__` from
`with ProviderSession(cfg) as provider_session:` at `runner.py:329`. One session per `run()`,
never shared across runs or threads, so `_closeables` needs no lock.

**Builder BODIES live in leaf modules; the REGISTRY and every call site live in
`providers.py`.** `bedrock_model.build_bedrock_provider` and
`anthropic_model._build_retrying_anthropic_model` import boto3/httpx inside the function body so
`import rebar.llm` stays stdlib-only. Story S1 landed the seam as a **pure relocation** before
any new provider used it, so Bedrock and the OpenAI-compatible path were additive registrations
rather than surgery on a live `run()`.

### 2. Capability decisions read `ModelProfile`, never a provider-name string

Every behavioural fork that used to key on a provider name now keys on a field of the resolved
`ModelCapabilities` record:

- output mode — `structured.output_mode` branches on `caps.native_structured_output`
  (`structured.py:58`), with `thinking` forcing `PromptedOutput` regardless;
- prompt caching — `capabilities.cache_settings_for` dispatches SOLELY on
  `caps.prompt_cache_style` (arms at `capabilities.py:493` and `:503`);
- sampling — `supports_temperature` (`capabilities.py:444`).

`_NATIVE_OUTPUT_PROVIDERS` and PR #121's proposed `force_prompted: bool` argument are both gone:
`grep -rn '_NATIVE_OUTPUT_PROVIDERS\|force_prompted' src/rebar --include="*.py"` returns **zero
hits** (verified 2026-08-03).

`prompt_cache_style` is derived from profile **field presence**, not concrete type
(`capabilities.py:216-221`). This is a hard constraint, not a style preference: an
`isinstance(BedrockModelProfile)` test is unimplementable in CI, because that class is exported
only from `pydantic_ai.models.bedrock`, which imports `botocore` at module top, there is no
SDK-free `pydantic_ai.profiles.bedrock`, and boto3 appears in `pyproject.toml` only under
optional extras. The isinstance check would break the stdlib-only `import rebar.llm` contract
AND fail CI.

Two override tables sit deliberately apart, and both are **membership-keyed**:

- `_REBAR_OVERRIDES` (`capabilities.py:151-153`) is an ORDERED TUPLE of
  `(predicate, overrides)` — explicitly NOT a dict keyed by provider name, because a name key
  would reintroduce the string matching this work exists to remove, and could not express a rule
  spanning two different HOSTS of the same model family. Its sole predicate, `_is_claude`
  (`capabilities.py:121-135`), requires BOTH arms: `isinstance(AnthropicModelProfile)` OR
  `bedrock_thinking_variant == "anthropic"`, because Bedrock-hosted Claude's profile is a
  `BedrockModelProfile` — a SIBLING subclass, not a descendant — so an isinstance test alone
  would miss the very models this epic exists to support. The second arm is a capability FIELD
  READ whose value happens to be a string enum; that is materially different from
  `resolved.startswith("anthropic")`.
- `_MODEL_ID_CAPABILITY_OVERRIDES` (`capabilities.py:186-206`) keys on the EXACT full model id
  and records per-model API DEFECTS (six entries, all `supports_temperature: False`) plus the
  two E1-measured Bedrock cells (`us.anthropic.claude-sonnet-4-6` and the dated
  `us.anthropic.claude-haiku-4-5-20251001-v1:0`) that set `native_structured_output: True` and
  `native_output_with_thinking: True`. It is a
  separate table rather than a widening of the first because the first table's predicate
  signature is a signed contract of a closed story, and a downstream leaf must not silently
  redefine a shipped signed contract. A **denylist** is used rather than an allowlist so an
  unknown-but-affected model fails LOUDLY instead of an unknown-but-fine model silently losing
  temperature.

`supports_thinking` and `supports_temperature` are recorded even though nothing branches on
`supports_thinking`. This is a **deliberate, documented exception** to "no field without a
consumer": the signed record must already state whether the model that produced each historical
verdict was CAPABLE of thinking, because that fact cannot be reconstructed later, and adding the
field after signing starts is strictly more expensive.

Provider-shaped error handling is **error-CONTENT-keyed**, never a provider branch:
`failure.translate_sampling_parameter_rejection` (`failure.py:371`) requires three conjuncts —
400-or-`ValidationException`, AND a named sampling parameter (`_SAMPLING_PARAMS`), AND a
rejection word (`_REJECTION_WORDS`) — so an unrelated 400 is not swallowed.

### 3. Provider qualification is by REGISTRY MEMBERSHIP, with zero exceptions

`llm/config.KNOWN_PROVIDER_NAMES` (`src/rebar/llm/config.py:214-241`) is a frozenset of **24
plain string literals**. `split_provider_qualifier` (`llm/config.py:244-261`) is THE single place
that answers "is this string provider-qualified?", and it answers by set membership:

```python
prefix, sep, rest = model.partition(":")
if sep and rest and prefix in KNOWN_PROVIDER_NAMES:
    return prefix, rest
return None, model
```

> Note the two `config.py` modules. Every `config.py` anchor in this ADR means
> **`src/rebar/llm/config.py`**, not the top-level `src/rebar/config.py`. The draft record of
> this work cited them bare, which resolves to the wrong file.

**Never by prefix shape.** The shape test — "does this prefix LOOK like a provider name?" —
answers the wrong question; the question is "IS this prefix a provider name?". This matches OSS
consensus: LangChain's `model.split(":",1)[0] in _BUILTIN_PROVIDERS`, LiteLLM's `provider_list`
membership test, pydantic-ai's `infer_provider_class` raising on an unknown name.

Four properties of the rule are load-bearing:

- **The set is a LITERAL of plain strings** — no imports, no lazy loading — because
  `split_provider_qualifier` runs during config resolution, where the optional `[agents]` extra
  may not be importable at all.
- **No name is grandfathered.** Neither `test` nor `google_genai` is a member. Probed against
  the pinned pydantic-ai 1.107.1: `infer_model("test:foo")` and
  `infer_model("google_genai:gemini-2.0")` both raise `ValueError: Unknown provider`. Granting
  either membership would make rebar MORE permissive than the library it wraps.
- **The two paths are deliberately ASYMMETRIC:** strict on an explicitly CONFIGURED provider
  (`model_classes.py:247-252` raises `LLMConfigError` naming the valid set), permissive on an
  unrecognized INLINE prefix. `"anthropic.claude-haiku-4-5-20251001-v1:0".partition(":")` yields
  a prefix that is by construction not a provider name, so "not qualified" is the only answer
  that keeps a colon-bearing Bedrock model id intact. The **canonical** Bedrock id — the string
  the AWS console displays and an operator will paste — carries that colon, so this is the
  common case, not an edge case.
- **GUARD ORDER is the fix, and there is no silent fallback.** An explicitly configured provider
  is checked FIRST, before the model string is scanned at all. Graceful fallback was considered
  and rejected on measured grounds: the defect that motivated the rule was **41 of 42 calls
  silently reaching direct Anthropic**. The policy is *fail closed (already) + fail fast (added);
  never fall back*.

`:` stays the separator. pydantic-ai's own `KnownModelName` contains TWO-COLON entries
(`bedrock:anthropic.claude-haiku-4-5-20251001-v1:0`), no surveyed project regretted its
separator choice, and the direction of travel supports membership over inference — pydantic-ai
REMOVED bare-name inference in v2.0.0b1.

**This is enforced, not merely intended.**
`tests/unit/test_bedrock_provider.py::test_capabilities_module_still_has_no_provider_name_prefix_matching`
(`:119`) `ast.parse`s `capabilities.py`'s own source, collects every `ast.Call` whose func is an
`ast.Attribute` with `attr in {"startswith","endswith"}`, and asserts the list is empty. It
asserts on real CALLS, not on text, so a comment naming the banned pattern is fine.
`grep -c startswith src/rebar/llm/capabilities.py` returns **0** (verified 2026-08-03).

The guard is load-bearing rather than decorative, and there is a clean demonstration:
**bug 7fe2 was ORIGINALLY FILED proposing `provider.startswith("gateway/")`.** That one-liner
would have violated the invariant, so it was not implemented; the fix is instead a frozenset of
five exact ids (`_GATEWAY_PROVIDER_NAMES`, `capabilities.py:384-392`). *The filed one-liner was
rejected on an architectural invariant, not on correctness.*

Companion guards, all verified present:

| Guard | Location | What it pins |
| --- | --- | --- |
| `test_f184_predicate_contract_is_untouched` | `tests/unit/test_bedrock_provider.py:103` | every `_REBAR_OVERRIDES` predicate keeps a one-argument `ModelProfile` signature, so the table cannot be widened into a name-keyed dict |
| `test_gateway_tier_is_not_decided_by_the_provider_names_shape` | `tests/unit/test_verdict_provenance.py:278` | `mygateway`, `openai-gateway`, `gateway`, `gateway-anthropic` all stay `first_class` — the anti-prefix rule asserted as BEHAVIOUR |
| `test_the_enumerated_gateway_set_matches_the_config_registry_exactly` | `tests/unit/test_verdict_provenance.py:235` | the drift pin between `_GATEWAY_PROVIDER_NAMES` and `KNOWN_PROVIDER_NAMES` |
| `test_known_provider_names_is_exactly_the_registry` | `tests/unit/test_provider_qualifier.py:203` | the 24-name literal |
| `test_the_static_set_does_not_drift_from_the_runtime_registries` | `tests/unit/test_provider_qualifier.py:288` | `set(session._builders) <= KNOWN_PROVIDER_NAMES`, one-directional BY DESIGN so a future rebar builder for a name pydantic-ai does not enumerate is not a failure |

**ONE surviving exception, named rather than papered over.**
`src/rebar/llm/anthropic_model.py:54` is
`if not web or not resolved.startswith("anthropic"):` in
`_anthropic_web_search_capabilities`, called from `runner.py:417-419`. That is a LIVE
provider-name prefix match on a decision path, OUTSIDE `capabilities.py`, so the AST guard does
not cover it. Three other `startswith` sites are **not** decision paths and should not be
reported as violations: `providers.py`'s docstrings describing the REMOVED branch (`:4`, `:184`);
`llm/config.py`'s model-NAME (not provider-name) inference, reached only after the membership
test fails; and `llm/evals/provider_parity.py:479`, a harness helper.

### 4. Bedrock is reached NATIVELY, on the ambient AWS credential chain

`build_bedrock_provider` (`bedrock_model.py:41-101`) returns a native `BedrockProvider`.
Authentication rides the ambient AWS credential chain ONLY — instance role in production,
`AWS_PROFILE`/env locally, or boto3's default chain. **rebar manages no Bedrock credential and
has no field for one:** `llm/config.py:450-453` records that absence deliberately, contrasting it
with the `api_key` field above.

The documented default is `us.anthropic.claude-sonnet-4-6` (`bedrock_model.DEFAULT_BEDROCK_MODEL_ID`,
`:38`) — an **inference-profile** id, not a foundation-model id. Two measured facts force this:

- plain on-demand ids are **not invokable at all**: AWS returns
  `ValidationException: Invocation of model ID … with on-demand throughput isn't supported.
  Retry your request with the ID or ARN of an inference profile.` (`bedrock_model.py:17-20`). So
  any Bedrock example config that omits the `us.`/`global.` prefix is broken on arrival, and
  `list-foundation-models` is a trap — it enumerates on-demand FOUNDATION models, and the
  profile id is exactly the form that is not one.
- `us.anthropic.claude-sonnet-4-6` is the **measured-caching** id (see §"Prompt caching").

**Inference profiles are used because on-demand ids are refused, not as a throttling
mitigation.** Earlier text in the epic hedged that profiles were chosen to mitigate throttling.
Zero throttle events were observed across ~1,400 parity requests, the local dogfood gate runs,
or the 19-call plan-review run. The evidenced reason is the refusal above.

Region resolution is pre-checked rather than defaulted (the region block in
`build_bedrock_provider`, ending at the typed raise on `bedrock_model.py:88`), raising an
`LLMConfigError` that names rebar's own knob. This is not defensive boilerplate; it fixes
a measured hard failure:

- **IMDS supplies credentials but never a region.** botocore's `IMDSRegionProvider` is wired
  only into the smart-defaults path (`defaults_mode=auto`), so inside the review-bot container
  `boto3.session.Session().region_name` is `None` and `boto3.client("bedrock-runtime")` raises
  `NoRegionError` — even though the instance role authenticates fine. Credential discovery and
  region discovery are INDEPENDENT concerns.
- **rebar's own knob alone is not sufficient for the rest of the image.**
  `REBAR_LLM_BEDROCK_REGION` threads into `BedrockProvider`, so rebar's path works while every
  other AWS caller in the image stays region-less. `infra/compose/docker-compose.yml` therefore
  sets both it and `AWS_DEFAULT_REGION`.
- **`AWS_REGION` alone does not work on this botocore.** Measured on boto3/botocore **1.43.62**:
  `AWS_REGION=us-east-1` with `AWS_DEFAULT_REGION` genuinely unset leaves
  `Session().region_name is None`; `AWS_DEFAULT_REGION` resolves. botocore's session-variable
  table maps region to `AWS_DEFAULT_REGION` only. **Carry the version qualifier** — this is a
  botocore behaviour, not a law. rebar's own error text once told operators to set `AWS_REGION`,
  which does not resolve a region; that was bug `4e71-f237-28c4-4c65`, now CLOSED, and the
  message and docstring in `build_bedrock_provider` carry the corrected guidance plus the
  measured version.
- **The escaping exception is not `NoRegionError`.** pydantic-ai already catches it and re-raises
  `pydantic_ai.exceptions.UserError`, which is NOT a subclass of rebar's
  `LLMError`/`LLMUnavailableError` — so no `except LLMError` handler catches it, including the
  review bot's fail-closed path.
- The preflight oracle is `boto3.session.Session().region_name` (`bedrock_model.py:87`), a
  read-only property measured at 0.002 s — never `os.environ.get("AWS_REGION")`, which would
  report a region boto3 then refuses.

**The methodological lesson is worth more than the fix:** the Bedrock runbook ENCODED THE BLIND
SPOT. Every probe in it passed `--region` explicitly, so none ever exercised ambient resolution.
That is precisely why `NoRegionError` survived an entire epic of Bedrock verification.

IAM is scoped to Claude only, in a NEW `infra/terraform/iam_s7.tf:57-75` (the single-owner
contract at `iam.tf:4-9` forbids editing `iam.tf`). Actions are `bedrock:InvokeModel` and
`bedrock:InvokeModelWithResponseStream` (`iam_s7.tf:60-63`). Both resource wildcards
(`iam_s7.tf:64-67`) are load-bearing: the REGION field is wildcarded because a cross-region
inference profile invokes the foundation model in its member regions, and the profile-prefix
wildcard is there because `global.` profiles are a valid MEASURED form. **The region is
wildcarded; the MODEL is not.**

`bedrock:Converse` is deliberately NOT granted — it is not the action the service checks, and
granting an unused action is blast radius for nothing. **This was reversed once, and the
reversal is the lesson.** A comment headed "SETTLED … CONFIRMED" recommended granting
`bedrock:Converse` on IAM **policy-simulator** evidence. A real call from the instance with only
`Converse` granted returned
`AccessDeniedException … not authorized to perform: bedrock:InvokeModel on … inference-profile/us.anthropic.claude-sonnet-4-6`.
The generalizable rule: **the policy simulator answers "what does the policy language permit for
the action you NAMED", not "which action does the service actually CHECK". Only a real call
distinguishes those two questions.**

> **Terraform in this file is not gated by code review.** `iam_s7.tf` and the monitoring
> stanzas below live in the repo but nothing verifies they were APPLIED before a change lands,
> so an un-applied plan reds `main` after submit. Tracked as bug `1c39-96d4-8f9e-4d38`. Read
> any IAM or alarm claim in this ADR as "declared in the tree", not as "live in the account",
> unless it is separately attested.

### 5. Two support tiers: `first_class` and `best_effort`

**There is no enum and no `Literal` type.** The tier is two bare string literals produced by ONE
expression (`capabilities.py:439`):

```python
"tier": "best_effort" if (base_url or via_gateway) else "first_class",
```

Within `src/rebar/llm`, `first_class`/`best_effort` appear only in `capabilities.py` (`:403`,
`:415`, `:416` docstring, and `:439`) — verified 2026-08-03. *(The same two tokens occur
elsewhere in `src/rebar` for unrelated purposes; scope the grep to `src/rebar/llm`.)*

**Two independent triggers, and NEITHER is registry membership:**

1. `base_url` truthy → `best_effort`;
2. `via_gateway = provider in _GATEWAY_PROVIDER_NAMES` (`capabilities.py:434`), a SEPARATE
   frozenset of five exact ids (`:384-392`) → `best_effort`.

This nuance matters and is easy to get wrong when reading the epic: the tier is **not** a
per-registry-entry field and **not** `ProviderSession._builders` membership. Everything else
defaults to `first_class` — **including an unregistered provider name.**

#### What `first_class` means

Anthropic and Bedrock: native builders, capability records derived from real profiles, covered by
the unit suite and by the provider-parity harness, and — as of this epic —
`gateway/*`-free by construction.

The epic's phrase was "native builders, **full fidelity**, covered by tests and the parity bar".
Both halves of that need narrowing.

**"Full fidelity" was explicitly redefined, per transport.** Retry and timeout on Bedrock are
botocore's, not httpx's, so the epic restated full fidelity as *equivalent retry and timeout
guarantees achieved PER TRANSPORT, not the same transport object on both providers*. See the
next subsection: even that narrower claim is not yet true.

**"Covered by the parity bar" was rescoped by operator ruling.** Cross-model verdict comparison
is OUT OF SCOPE: *"LLMs are non-deterministic. Expecting two runs to produce identical results is
unrealistic and beyond the scope of this epic. The epic's scope is to ensure THE SAME PAYLOADS
REACH THE LLM and THE LLM HAS THE SAME TOOLS AVAILABLE, regardless of model and provider."* Under
that definition the harness reported: same payloads reach the LLM, same tools are available,
routing is provider-clean (zero non-Bedrock calls across 84/84 case-runs per slot), and both
inference-profile forms invoke. Validity was 1.000 on both arms across 360 case-runs; the
standard slot ran 9.9% FASTER on Bedrock (2484.6 s vs 2756.5 s) and the frontier slot within
0.1%.

Two caveats a reader of the parity artifact will need:

- The residual `gating_failures` (standard: 1 flip; frontier: agreement 0.929 + 2 flips) are
  **cross-model verdict comparisons — informational, not gates.** Both slots therefore carry
  `passed=False` while no acceptance criterion is unmet. An earlier verdict on the same run
  said the opposite and was superseded; the sentence that reconciles them is *"my earlier
  judgement — that the ticket could not honestly close as 'Bedrock cleared the bar' — was
  correct under the OLD criteria; the criteria were the problem, not the measurement."*
- The measurement is a **snapshot on a stale base** (commit `4214516f95a`), predating later
  overlap-token work. No re-measurement on current `main` was performed. Also,
  `metrics.latency_s` in the committed artifact reads `0.0/0.0` after an offline re-score — the
  real numbers survive in the ticket comments and in git at `72f863b5a2`. **An ADR or runbook
  citing the artifact rather than the comment will read the wrong number.**

#### What `first_class` does NOT yet mean — the retry/timeout gap

**The retry/timeout half of "full fidelity" is an UNMET INTENTION, not a landed property.** The
epic described the Bedrock builder constructing its client with
`botocore.config.Config(retries={"max_attempts": cfg.llm_retry_max_attempts, "mode": "adaptive"})`
and taking its read bound from
`BedrockProvider(aws_read_timeout=cfg.timeout_s, aws_connect_timeout=10.0)`, concluding that "the
retry-attempt count and the read timeout are thus driven by the SAME `LLMConfig` fields on both
first-class providers."

**None of that shipped.** Verified against the working tree on 2026-08-03:

- `grep -rnE 'botocore\.config|BotoConfig|aws_read_timeout|aws_connect_timeout' src/rebar tests
  --include="*.py"` returns **nothing**. There is no botocore `Config`, no `adaptive` retry mode,
  and no `aws_read_timeout` anywhere in the source or the test suite.
- The whole Bedrock client construction is one line — `bedrock_model.py:101`:
  `return BedrockProvider(region_name=region)`.
- The tenacity envelope and its `RetryConfig` exist ONLY on the Anthropic path
  (`anthropic_model.py:129-153`), together with the `AsyncAnthropic(max_retries=0)` guard and the
  explicit `httpx.Timeout(read=cfg.timeout_s, connect=10.0, …)` built at `providers.py:219` and
  passed at `:220-222`.

So `llm_retry_max_attempts` (`llm/config.py:159`, default 4), `llm_retry_max_wait_s`
(`llm/config.py:160`, default 60) and `timeout_s` **do not reach Bedrock at all.** Bedrock inherits
botocore's stock defaults while the Anthropic path owns an explicit, configured envelope.

**This is tracked as bug `61d8-ff23-8ee0-4289` — "Bedrock ignores `llm_retry_max_attempts` and
`timeout_s`: no botocore retry envelope."** Until it closes, "first-class" means *native builder
+ profile-derived capabilities + tested + payload/tool/routing parity*, and does **not** mean
retry/timeout parity. Anyone quoting the epic's "same `LLMConfig` fields drive both" sentence is
quoting an intention. This matters more now than when it was written, because the production
review bot runs on Bedrock (§10) — the provider with no configured envelope is the one in
production.

#### What `best_effort` means

An intermediary may have rewritten the request and **rebar cannot vouch for what reached the
model** (`capabilities.py:409-418`). Everything outside the two native providers is reached
through ONE OpenAI-compatible builder (`providers.py:235-290`), and that builder performs a
deliberate capability DOWNGRADE: `_RebarOpenAICompatibleProvider` withdraws
`supports_json_schema_output` from upstream's OpenAI profile (`providers.py:256-272`). An opaque
OpenAI-*compatible* endpoint has no obligation to implement strict `json_schema` decoding, while
`openai_model_profile(...)` describes OpenAI's HOSTED API and says `True` — which would steer
`capabilities_for()` onto `NativeOutput` and fail opaquely. Withdrawing that one flag makes the
claim true BY CONSTRUCTION rather than by assumption.

Three shape decisions inside best-effort:

- **`base_url` is provider CONFIGURATION, not provider SELECTION.** The `openai` builder is
  registered only when `cfg.base_url` is set (`providers.py:117-118`), and the reason is not SDK
  absence: with no `base_url`, rebar has NOTHING to contribute to provider construction — no
  endpoint to inject, no key to place, no profile to override. rebar builds a provider only when
  it has configuration to apply.
- **The profile override rides the PROVIDER, not a model argument** — forced by the seam. rebar's
  factory hands back a `Provider` and pydantic-ai's `infer_model` constructs the model itself, so
  a model-level argument is unreachable from here.
- **`_pai_check_config` VALIDATES instead of refusing** (`structured_run.py:576-597`). Exactly
  two errors remain: `api_key` without `base_url` and a non-absolute `base_url`.

The optional SDKs are in the `dev` extra only, so CI can prove the story — precedent: `dev`
already installs `mcp`, likewise an optional runtime extra.

### 6. Verdicts SIGN on every tier, and STAMP the tier

`provenance_for` (`capabilities.py:395-446`) assembles an **additive sibling**
`provider_provenance` object; the pre-existing `model` string is untouched. Additive-only is
deliberate: an older reader ignores the new key, so no store-compatibility gate and no migration
are required. It is stamped at `runner.py:405-407` and persisted by all three gate sidecars —
`completion_sidecar.py:279` and `:297`, `plan_review/sidecar.py:637`,
`code_review/sidecar.py:106`. The payload contract is documented in
`docs/event-schema.md` §"Provider provenance (`provider_provenance`)".

**rebar signs on every tier and gives the consumer a lever rather than a refusal.** Refusing to
sign an off-tier verdict gives a stronger guarantee but breaks local development for anyone
without a first-class provider key — which weakens exactly the contributor on-ramp
`docs/plan-review-gate.md` depends on. A contributor running Ollama can still exercise the gates.

Three construction rules:

- **Reuse the capability record, never recompute.** Provenance stamps the SAME `caps` that drove
  the run (`runner.py:404-407`); a second `capabilities_for` resolution could silently diverge
  from the record that actually drove it.
- **`.hostname`, never `.netloc`** (`capabilities.py:433`, rationale at `:428`). `.netloc`
  retains embedded `user:secret@` credentials, and no credential material may enter a signed
  record. *(This corrects a stale citation: the `parsed.netloc` site the epic named in
  `runner.py` no longer exists. `grep -rn netloc src/rebar` now returns only
  `structured_run.py:593` — a `base_url` absoluteness check — that rationale comment, and
  `review_bot/gerrit_client.py`.)*
- **OMIT the key where no model ran.** A cfg-derived record at a site where nothing ran would
  reproduce the exact defect the record exists to fix.

**Beware a name collision:** `plan_review/sidecar.py:557` `"tier": f.get("tier")` is a FINDING
tier inside `_slim()`, entirely unrelated to the provider tier. The `tier` token in
`review_kernel/decide.py` and `plan_review/pass1.py` is impact-scoring / DET-vs-LLM. Do not
overload it.

#### What a consumer can infer — and four caveats

A consumer reads `provider`, `model` (provider-qualified), `endpoint_host` (HOST only), `tier`,
and `capabilities` — exactly four fields: `native_structured_output`, `prompt_cache_style`,
`supports_thinking`, `supports_temperature` (`capabilities.py:440-445`). On a fallback chain it
also gets `candidates` (`runner.py:413`) and `ran_model` (`runner.py:544`).
`cache_min_prefix_tokens` is deliberately NOT carried: adding a fifth capability key would change
the bytes of already-signed verdicts, and the floor is a published property of the model,
recoverable from the recorded model id.

**CAVEAT 1 — the tier is derived purely from CONFIGURATION-TIME facts, never from an observed
endpoint.** A `first_class` stamp does NOT prove where traffic went. Two concrete holes: an
ambient loopback `ANTHROPIC_BASE_URL` proxy is BYPASSED rather than recorded
(`providers.py:190-197`), and an unregistered or nonsense provider name stamps `first_class` by
default (§5).

**CAVEAT 2 — the tier is NOT in the signed bytes.** Signing binds `(ticket_id, manifest)`, where
the manifest is a list of `"key: value"` strings; the provenance object lives in the adjacent
sidecar PAYLOAD. `docs/event-schema.md` states this plainly: *"It is not part of the
signed bytes … So adding this key cannot invalidate an existing signature."* Whether provenance
should ALSO enter the signed bytes was explicitly identified as a second, deliberate decision —
and **that decision was never taken.** Treat any phrasing that says verdicts "stamp the tier into
the signed payload" as loose. Whether a consumer can CRYPTOGRAPHICALLY trust the tier is not
established. A precedent for how it could be done without invalidating prior signatures exists in
the rebar-version stamp (`tests/unit/test_attestation_signing.py:285`).

**CAVEAT 3 — absence is OVERLOADED, and the two meanings are indistinguishable.** By the
additive-compatibility rule, an absent key means "unknown, legacy". By the omit-where-nothing-ran
rule it ALSO means "no model ran / no provider resolved". A reader cannot tell which.

**CAVEAT 4 — NO CONSUMER EXISTS.** The claim that "a consumer now has the lever to reject an
off-tier verdict" is an assertion: no consumer was built, none is named, and no test asserts that
a consumer can act on `tier`. The in-tree statement of intent is
`tests/unit/test_verdict_provenance.py:217-218` — *"The two-tier field exists so an attestation
consumer can reject an off-tier verdict; a wrong tier makes it worthless."* That is the rationale
for the field's correctness, not evidence of use.

The tier's own biggest near-miss is worth preserving: an `[operator-attested]` criterion found
that a real `rebar review-plan` wrote a sidecar with `provider_provenance` **ABSENT** while all
ten unit tests passed — because every test handed `build_payload` a verdict dict that ALREADY
contained the key. *A textbook constructed-input pass: the oracle proved the persist step, not
the path.* The guard that now protects it uses no hand-built verdict anywhere in the file.

### 7. Prompt caching is a capability, keyed per provider, with per-model floors

`cache_settings_for` sets instructions + tool-definitions breakpoints on every caching call, plus
a THIRD message-tail breakpoint on the AGENTIC path only, **with DIFFERENT keys per arm**
(`capabilities.py:492-514`): `anthropic_cache=True` vs `bedrock_cache_messages=True`.

**A shared automatic key was rejected — mind the provider asymmetry.** Bedrock has NO
`bedrock_cache` automatic key and REJECTS a top-level `cache_control` outright
(`"Extra inputs are not permitted"`). LangChain shipped exactly this regression: their Anthropic
prompt-caching middleware broke on Bedrock in 1.4.1 by switching to top-level automatic caching
(langchain#37042). Recorded at `capabilities.py:475-484`.

Budget is safe on both arms: pydantic-ai's `_limit_cache_points` drops the Anthropic budget to
`MAX_CACHE_POINTS = 3` when automatic caching is on, and this sets exactly 3; Bedrock's limit is
4 and this sets 3.

**Per-model cache floors come from Anthropic's PUBLISHED table, and the empirical brackets are
VERIFICATION ONLY.** `_MODEL_CACHE_MIN_PREFIX_TOKENS` (`capabilities.py:68-84`) is 13 entries at
512/1024/2048/4096, sourced from
`platform.claude.com/docs/en/build-with-claude/prompt-caching`, section "Minimum cacheable prompt
length", read 2026-08-02. Two independent in-repo measurements were used only to verify it, and
all three agree. **This is the citation standard the rest of this ADR should be held to** — a
published URL plus a read date, with measurements explicitly demoted to corroboration. A single
global floor was rejected because *the published table is not monotonic across generations*,
which is exactly what one global cannot express. The sharpest consequence: `claude-opus-4-8` is
rebar's `DEFAULT_MODEL` and its floor is **1024**, so the previous 4096 global was 4x too high on
the model rebar runs most (`capabilities.py:72-74`).

**Bedrock ids are deliberately EXCLUDED from the floor table** (`capabilities.py:63-67`).
Anthropic's table states these minimums apply on every platform EXCEPT Amazon Bedrock, which
publishes its own; rebar has measured none of Bedrock's, so a Bedrock model falls through to the
conservative fallback rather than being assigned a guessed number.

The warning compares the MARKED PREFIX, not total `input_tokens` (`structured_run.py:474`).

#### Measured caching facts worth not rediscovering

- **Prompt caching WORKS with a regional inference profile.** `us.anthropic.claude-sonnet-4-6`:
  attempt 1 `cache_write=4017`, `cache_read=0`; attempt 2 `cache_write=0`, `cache_read=4017`;
  reproducible. Caching and inference profiles are NOT mutually exclusive. This refutes the
  premise of pydantic-ai#4381 for this model/region — the issue this work was partly built around
  does not reproduce.
- **Caching is MODEL-dependent and fails SILENTLY, and the discriminator is the MODEL, not the
  profile prefix.** `us.anthropic.claude-opus-4-5-…` AND `global.anthropic.…` both returned
  `cache_read=0` and `cache_write=0` on both attempts with the full 4029 input tokens billed
  every call — no error, no warning. A first inference blaming the `global.` prefix was
  CORRECTED by a controlled same-model comparison showing both fail. This silent double-billing
  is the real hazard.
- The existing `_warn_if_zeroed_usage` guard (`structured_run.py:541`) cannot detect it: that
  predicate needs `input_tokens == 0`, and the measured Bedrock failure is the OPPOSITE shape
  (`input_tokens=4029` with both cache counters zero). Hence a separate cache-zero warning.
- **Bedrock's `cachePoint` block DOES have a TTL — it is defaulted, not missing.** The optional
  `ttl` defaults to **5 minutes**, the same default Anthropic's `cache_control` applies. There is
  no observable difference to account for and no per-provider cache tuning is needed. Bedrock
  additionally offers a 1-hour TTL for some Claude models; it is opt-in and rebar does not
  request it, so it is not a difference either. **This SUPERSEDES the "Bedrock `cachePoint`
  carries no TTL" claim, which appears in at least four places in the ticket record, including
  the epic's own final comment.** Honest limit: no gate run exercises a gap LONGER than 5
  minutes, so the runs confirm the lifetime is not SHORTER than a gate run; they do not probe
  expiry. rebar never writes the raw wire key — `grep -rn cachePoint src/rebar` returns nothing.
- End to end through rebar on Bedrock (19-call gate run): `cache_read_tokens=542,723`,
  `cache_write_tokens=6,239`. Local dogfood run: 27 calls, 27/27 `provider=bedrock`, zero direct
  Anthropic, `cache_read 226,668 / cache_write 33,066`, signable PASS, stamped
  `tier=first_class`, `native_structured_output=false`, `prompt_cache_style=bedrock`,
  `supports_thinking=true`. Notable: **despite `native_structured_output=false` on the Bedrock
  profile, the run emitted a fully-formed verdict envelope** — `PromptedOutput` is sufficient.
- **Cache writes cost a 1.25x premium**, so parallelizing cold calls that share a prefix is
  strictly worse than the sequential version it replaces and worse than no caching at all. If
  ever parallelized, the required shape is one call first to completion, then fan out N-1
  readers.

### 8. The Bedrock model-id trap set (record it once so it is not rediscovered)

- Plain foundation-model ids are refused on-demand (§4).
- `list-foundation-models` does not list inference profiles.
- `global.` profiles are a valid MEASURED form — do not pin `us.` in IAM.
- **VERSIONED ids broke provider qualification**: the old "already provider-qualified" test was
  `":" in model`, and a versioned Bedrock id contains a colon in its version suffix, so a
  configured `provider="bedrock"` was silently dropped. Fixed by the membership rule (§3). The
  operational sting: the canonical id is what the AWS console displays, so `…-v1:0` is the
  string an operator will paste.
- **The inverse also bites**: unversioned haiku is INVALID on Bedrock — the valid form carries
  the version suffix, else `ValidationException: The provided model identifier is invalid`. The
  suffixes are NOT uniform across models, which is why
  `.github/llm-providers/bedrock.toml` instructs taking every new id VERBATIM from
  `aws bedrock list-inference-profiles`.
- **`temperature` is FATAL on some Bedrock models**: `us.anthropic.claude-opus-4-7` +
  `temperature=0` → 400 `'temperature' is deprecated for this model`; the identical call without
  it succeeds. Root asymmetry: `_drop_unsupported_sampling_settings` exists ONLY in pydantic-ai's
  `models/anthropic.py`, NOT in `models/bedrock.py` — so the direct-Anthropic path degrades
  gracefully and the Bedrock path hard-fails. Same defect, two symptoms. Handled as a capability
  (`_MODEL_ID_CAPABILITY_OVERRIDES`, §2) rather than a provider branch.

### 9. The IMDS finding: measure first, change nothing

The epic opened a contingency to raise `http_put_response_hop_limit` from its default of 1, on the
theory that the extra Docker-bridge hop would block IMDS from inside the review-bot container.
The manifestation was a **documentary contradiction in the tree**, not a credential failure:
`main.tf` set `http_tokens = "required"` and left the hop limit unset, and a `pyproject.toml`
comment attributed a journald fallback to "in-container IMDS is unreachable", while
`docker-compose.yml` documented the opcert container fetching SSM keys via the instance profile —
which only works if IMDS IS reachable.

**Measured, operator-attested, from inside the review-bot container: IMDSv2 token request
`http_status=200`, `token_len=56`, `role=rebar-gerrit-instance-role`.** The contingency did NOT
fire. No `metadata_options` terraform change was made and no
[`0046-security-posture-and-accepted-limitations.md`](0046-security-posture-and-accepted-limitations.md)
entry was written. **Anyone reading the epic should not infer a hop-limit change happened.**

Two things survive as durable content:

- **The standing exposure fact is the flat single-bridge compose topology.**
  `docker-compose.yml` defines no `networks:` section, so gerrit, review-bot and opcert already
  share one default bridge — all three first-party components that already receive
  instance-role-derived secrets through the `.env` that `fetch-secrets.sh` writes. Had the hop
  limit been raised, it would have widened reach only to containers already inside the same trust
  boundary. That exception was pre-authorized with that bounded-exposure rationale, and was not
  needed.
- **The `pyproject.toml` comment was wrong about its own cause.** The review bot's CloudWatch
  voter metrics no-op for the same missing-REGION reason, and their comments blame IMDS
  reachability. Cite this as a worked example of an **unevidenced in-tree comment misdirecting a
  design** — the comment cost a terraform contingency that measurement then showed was
  unnecessary.

### 10. Where the seam is actually running

**The production review bot runs on Bedrock. This is landed, not planned** — story
`eb6e-f43a-e86b-443a` is CLOSED. `infra/compose/docker-compose.yml` sets the three class slots to
Bedrock inference profiles (`:186-188`) plus `REBAR_LLM_BEDROCK_REGION` (`:151`), and
`REBAR_LLM_MODEL` is deliberately absent so the per-pass split survives (see
[`0057-model-classes-and-the-rebar-llm-model-deprecation.md`](0057-model-classes-and-the-rebar-llm-model-deprecation.md)).

Observed on a real Gerrit change (1329) that received `LLM-Review +1`: the verdict sidecar's
`provider_provenance` reads `provider='bedrock'`,
`model='bedrock:us.anthropic.claude-sonnet-4-6'`, `prompt_cache_style='bedrock'`. The per-call
usage log for that vote showed **8/8 calls on `bedrock:` inference profiles and ZERO
direct-Anthropic calls**, with `frontier` resolving to the opus profile and `standard` to the
sonnet profile, and nonzero `cache_read` on three calls (386,204 / 308,003 / 149,162). That is the
seam, the class split, the tier stamp and Bedrock prompt caching all exercised together by the
gate that guards `main`.

**Local development also selects Bedrock, through the project's own config file.** `rebar.toml`
is this project's authoritative config, and its `[llm.model_classes]` table names the same three
Bedrock inference-profile ids as `.github/llm-providers/bedrock.toml`, so a local gate run
dogfoods the provider production uses without any per-developer opt-in. That table was added by
bug `d2ce-36f5-fd08-4e40`; before it, `rebar.toml` carried no `[llm]` table at all and every
local gate silently resolved to `anthropic:claude-*` while the production bot ran on Bedrock —
the exact dogfooding gap this epic exists to close, left open on the path developers and coding
agents hit most. The documented escape hatch is one line, `REBAR_LLM_CONFIG_FILE` pointed at the
Anthropic overlay, because `REBAR_LLM_CONFIG_FILE` OUTRANKS the project file; the cost of the
default is that a contributor without Bedrock access must use it. **Read `d2ce`'s state before
quoting this paragraph as current** — it is the ticket that establishes it.

---

## Alternatives rejected

**LiteLLM as an in-process SDK (the aider / OpenHands pattern).** It buys 100+ providers for one
dependency, but its normalization layer sits on exactly what rebar depends on — prompt-cache
semantics, tool-call shapes, cost accounting — and neither aider nor OpenHands escapes
per-provider code anyway: OpenHands still carries a `model_features.py` registry and two
auto-detected completion modes. rebar chose pydantic-ai for weight and structured-output
reliability, and adding LiteLLM in-process would duplicate its model layer. *Citation honesty:
the epic's phrase "documented to drift" carries no URL, issue number, or measurement, so this
ADR does not repeat it as a fact; the OpenHands `model_features.py` observation is the concrete
leg.* **This is a PARTIAL rejection — LiteLLM's SHAPE was adopted where it was right:** its
`model_list` alias / `litellm_params` shape is the prior art for class slots, and its
`provider_list` membership test is the precedent for §3. It also refuted rebar's own objection to
the membership approach: LiteLLM's `provider_list` is explicitly lazy-loaded to avoid importing
providers at import time — the same constraint, already solved. rebar diverges on exactly one
point: LiteLLM puts `api_key` inline in `litellm_params`, and rebar forbids it inside a class
slot (`model_classes.py:80-83`). **A LiteLLM PROXY remains reachable** through the best-effort
OpenAI-compatible builder, so breadth is preserved without the dependency.

**A gateway fronting Bedrock (PR #121's approach).** It requires every operator to stand up and
accredit a proxy, and gives up caching, thinking, native structured output, and the retrying
transport. The strongest argument is the signing one: a gateway records a `model` string in the
SIGNED verdict sidecar that no longer names what actually answered. The native path was then
shown to deliver what the gateway gives up — measured caching on a real inference profile, and a
full `rebar review-plan` driving the entire agentic gate on Bedrock to a well-formed verdict
(not merely a single-turn canary), now confirmed in production (§10).

**An `elif` chain in `run()` (PR #121's shape).** Rejected: it keeps provider knowledge smeared
across the runner and leaves no place to attach the capability lookups §2 needs. A bare
`build_model()` bypassing `infer_model` was rejected too — it abandons the upstream hook, so
rebar would own provider-string resolution forever. So was a mutable out-parameter to smuggle the
client back: an unnamed side channel with no ownership story.

**A per-call-site `force_prompted: bool` (PR #121).** Rejected: it grows one argument per
capability per provider, and it encodes the answer at the CALLER rather than at the model.
`grep force_prompted src/rebar` is empty.

**A capability rule derived purely from profile flags.** TRIED AND MEASURED WRONG.
`supports_json_schema_output and not supports_thinking` looks elegant and reproduces Anthropic
and OpenAI correctly, but tested against every provider then in `_NATIVE_OUTPUT_PROVIDERS` it
BREAKS TWO: `gemini-2.5-flash` has `supports_thinking=True` (would lose NativeOutput) and `groq`
has `supports_json_schema_output=False`. Recorded at `capabilities.py:147-150`: *do not
reintroduce it.*

**`provider.startswith(...)` prefix matching.** Rejected for registry membership (§3), and the
prohibition is enforced by an AST guard rather than by convention.

**A gateway run stamped `first_class`.** Rejected. `KNOWN_PROVIDER_NAMES` admits five
`gateway/*` names and they are LIVE — pydantic-ai's `infer_provider` resolves them — yet a
`gateway/anthropic:claude-opus-4-8` run carries NO `base_url`, because the gateway URL is
resolved inside pydantic-ai from its own env/API key. Under a base_url-only rule it would have
signed as `first_class` with `endpoint_host: None`, even though every byte traversed an
intermediary that can rewrite the request. *Evidence caveat: the supporting citation is a SINGLE
third-party blog post about the **Vercel** AI Gateway silently downgrading Anthropic's 1-hour
prompt cache, with no read date and no corroboration, and it concerns a DIFFERENT gateway from
the one being tiered. It supports "gateways can silently rewrite"; it does NOT establish that
Pydantic's AI Gateway does.* **Also rejected: back-filling `endpoint_host` for a gateway** —
`endpoint_host` reports what was CONFIGURED, so synthesising a hostname from a provider id would
place a value nobody set into a signed record (`capabilities.py:420-426`).

**REFUSING to sign off-tier verdicts.** Rejected: a stronger guarantee that breaks local
development without a first-class provider key, weakening the contributor on-ramp (§6). Also
rejected: **REPLACING the model string** with the structured object — a breaking payload change
requiring a compatibility gate and a migration for zero added information. And **carrying
provenance through `coverage['usage']`** — rejected on a code fact: it is DROPPED one layer
earlier than expected, because the usage record is built from the runner result's `_usage`
sub-dict only and all five mint sites pass only the usage dict.

**A long-term Bedrock API key in SSM.** It would have fitted the existing `fetch-secrets.sh`
pattern with no IAM change — genuinely CHEAPER. Rejected on a directional argument that stands on
its own: **trading working short-lived role credentials for a long-lived bearer token is the
wrong direction on a box that already has an instance role.** *Two supporting legs are
UNCITABLE as written and are demoted here rather than repeated: AWS's guidance restricting
long-term Bedrock API keys to development is paraphrased with no doc URL or section, and the
claim that "Wiz documented such keys appearing in public repositories within two weeks of
launch" has no link, date, or report title.* Also rejected: a host-side credential broker
minting short-lived credentials into the container — a bespoke component to build, operate and
secure for a problem the instance role already solves. And explicitly NOT done: iptables
containment of `169.254.169.254` from non-bot container networks — not implementable here (there
are no per-container networks to block between) and it would break opcert, which depends on
IMDS. On the CI arm, static long-lived keys are rejected separately: the mechanism must be OIDC
federation — *reuse the policy shape, not the delivery path.*

**A cross-provider `FallbackModel` to direct Anthropic, for the production cutover.** It would
keep landings flowing during a Bedrock outage, but it would mask exactly the failures the
cutover exists to surface, and a persistently degraded Bedrock could run unnoticed behind a
silent fallback. It would also keep an Anthropic key in SSM that the cutover otherwise retires.
The replacement chain is four mechanisms: the existing tenacity transport retry absorbs transient
faults *(on the Anthropic path ONLY — see the retry gap, `61d8`, which means this leg does not
currently apply to the provider now in production)*; the existing fail-closed
`VOTER_ERROR` path handles hard failure (`review_bot/voter.py:115-128`); a CloudWatch alarm fires
on `VOTER_ERROR`; and a documented one-line kill switch is the human recovery path.
**The cost is stated, not hidden: if Bedrock degrades, landings stop until a human runs the kill
switch** — and since the cutover has landed (§10), that is the live production posture, not a
hypothetical.

> **Gap, recorded rather than glossed.** The alarm that EXISTS is
> `rebar-gerrit-voter-errors` (`infra/terraform/monitoring_s4b.tf:36-54`) on
> `rebar/host:voter_errors`, `period = 300`, `evaluation_periods = 1`, `threshold = 0`. The
> epic's acceptance criterion asked for a DIFFERENT alarm — Bedrock `InvokeModel` client-error
> rate, threshold >25% of invocations over a 15-minute window, 2 consecutive periods, evidenced
> by an alarm ARN and one forced test notification. **No such alarm, ARN, or forced test
> notification exists.** Do not cite the voter-error alarm as the Bedrock-specific fallback
> replacement the epic specified. Worse, **that alarm declares no `alarm_actions`, so it
> notifies nobody** — it can only go red on a dashboard someone happens to open. Tracked as bug
> `9baf-06ce-60ba-4f0b`. So the third leg of the four-mechanism chain above is, today, not a
> paging mechanism at all.

Note the **INTRA**-provider `FallbackModel` was ACCEPTED (`model_classes.build_fallback_model`,
`:388`). The rejection is specifically of CROSS-provider fallback. And a cross-provider fallback
would have happened AUTOMATICALLY, which was treated as a BUG:
`models_at_or_above("bedrock:us.anthropic.claude-sonnet-4-6")` returned BARE Anthropic ids, so on
a context-limit error a Bedrock run silently escalated to DIRECT ANTHROPIC — *"the founding
defect of this epic on a second, independent code path."* Fixed by moving escalation TARGETS to
the class vocabulary while escalation WINDOWS stay keyed on concrete model families, under the
provider-agreement rule: *providers differ on a HIGHER rung → drop the rung*; escalation stops
and the existing too-big failure reports it, a visible outcome instead of a silent provider jump.

**Branching on `cfg.base_url` BEFORE provider inference.** Rejected: it makes `base_url` silently
override an explicit `REBAR_LLM_MODEL_PROVIDER`, contradicting rebar's own documented recipe, and
adds a fourth implicit provider selector.

**Adding `google_genai` to `KNOWN_PROVIDER_NAMES`** (and `google-gla`/`vertexai` as first-class
targets). Rejected: it would make a name a first-class member that NEITHER pydantic-ai NOR rebar
can build. pydantic-ai 1.107.1 emits a deprecation warning for the legacy Google prefixes and
REMOVES them in v2.0, so choosing one would re-introduce a scheduled break. (The legacy names
remain in the registry as *admissible qualifiers*, which is a different claim from
first-class support.)

**Reading the real context window from the model profile.** Rejected ON AN ABSENCE:
`ModelCapabilities` exposes `native_structured_output`, `prompt_cache_style`,
`supports_thinking` and `supports_temperature` — there is **no context-window field**, upstream
or in rebar.

**"Presence of a `verifications` list maps to `block`" in the parity scorer.** This was the plan
reviewer's OWN suggested fix, and it was rejected after checking the spec: the
`code-review-verify` arm is scored on PRESENCE of verifications, not on a FAIL/BLOCK verdict, and
BOTH gold cases (real-defect → `block`, clean-rename → `advisory`) are REQUIRED to emit a
non-empty list. Mapping presence to block would have fixed the recall miss and simultaneously
scored the CLEAN case as a block — **manufacturing a false accept.** The arm carries no polarity,
so no adapter can derive one. The correct remedy is EXCLUSION:
`POLARITY_FREE_SOLVERS = frozenset({"code-review-verify"})` at
`src/rebar/llm/evals/provider_parity.py:101`, kept deliberately SEPARATE from `AGENTIC_SOLVERS`
because the reasons differ and collapsing them would lose one — *a future reader deleting the
"redundant" set would silently re-admit mis-scored cases*. Rows are RETAINED in the artifact for
provenance and dropped only from SCORING. **The headline: the mis-scored case was the ONLY recall
miss on the Bedrock arm, on both slots. Bedrock's true recall is 1.000, not the 0.929 recorded.
The bug was UNDERSTATING Bedrock.**

**A CI job for the parity harness.** Rejected on two independent grounded reasons: the Bedrock
grant in `iam_s7.tf` is attached to the Gerrit INSTANCE role and the only GitHub-OIDC role is
ReadOnlyAccess, so a CI arm would first require a new Bedrock-capable OIDC role (unfunded work
outside the story); and at ~3,200 calls with concurrency 1 the run is roughly 9 hours wall
clock, EXCEEDING the 6-hour GitHub-hosted job limit. An **Anthropic-vs-Anthropic noise-floor
control run** was also rejected, twice, with the consequence stated honestly: *without it we
cannot say whether "zero decision flips" is achievable by ANY provider pair, including Anthropic
against itself.* What later made it unnecessary was already-paid-for data — a separate
measurement found relation labels shifting on 6 of 12 ordered pairs **between builds of
identical code**. A zero-flip bar across two different models presumed a determinism that does
not exist.

**Documenting all of this in `docs/llm-framework.md` only.** Rejected: that records the *what*
but not the *why*, and rebar's ADR set is where rejected alternatives live. Without them, the
LiteLLM and gateway options get re-proposed as one-offs — the failure mode this work exists to
end.

---

## Consequences

**Positive.**

- A new provider is a registry entry plus a leaf builder, not surgery on `run()`. The
  capability layer means a new provider inherits correct output-mode and cache decisions without
  a single name comparison.
- The per-pass model split survives crossing a provider boundary — measured: 41 calls on
  `anthropic:claude-opus-4-8` and 1 call on `bedrock:us.anthropic.claude-sonnet-4-6` in one gate
  run, and in production 8/8 calls on Bedrock with `frontier` and `standard` landing on different
  profiles (§10). That split is the capability this seam exists to deliver and which no single
  scalar `REBAR_LLM_MODEL` could express (see
  [`0057-model-classes-and-the-rebar-llm-model-deprecation.md`](0057-model-classes-and-the-rebar-llm-model-deprecation.md)).
- A signed verdict now carries what actually answered, so a consumer CAN in principle
  distinguish a Bedrock-backed `LLM-Review` from an Ollama-backed one.
- The OSS on-ramp survives: a contributor with a local server can exercise every gate, and the
  verdict says honestly that rebar cannot vouch for the route.
- rebar dogfoods the provider it recommends: the gate that guards `main` and the local gates a
  developer runs both resolve to Bedrock (§10).

**Negative, and deliberately accepted.**

- **`first_class` does NOT currently mean retry/timeout parity.** `llm_retry_max_attempts` and
  `timeout_s` do not reach Bedrock at all (`bedrock_model.py:101`). Tracked as bug
  `61d8-ff23-8ee0-4289`. Until it closes, a Bedrock deployment runs on botocore's stock retry
  defaults while an Anthropic deployment runs on rebar's configured envelope, and no
  configuration change reveals the difference. Since production is Bedrock, the unconfigured
  path is the live one.
- **The tier is a configuration-time claim, not an observation, and it is unsigned.** A
  `first_class` stamp does not prove where traffic went; an unregistered provider name stamps
  `first_class`; and the tier lives outside the signed bytes (Caveats 1–2). Anyone building the
  consumer this field exists for must know that before relying on it.
- **No consumer exists.** The lever is available and unused (Caveat 4).
- **Absence of `provider_provenance` is ambiguous** between "legacy" and "nothing ran"
  (Caveat 3).
- **One provider-name prefix match survives** on a live decision path outside the AST guard's
  scope (`anthropic_model.py:54`). The invariant is "zero exceptions in `capabilities.py`", which
  is narrower than "zero exceptions in rebar".
- **Bedrock has no measured cache floor**, so Bedrock models fall through to a conservative
  fallback rather than to a correct number.
- **`[bedrock]` declares a uv RESOLUTION CONFLICT with `[eval]`** (`pyproject.toml:260-263`), so
  the two are not co-installable.
- **A cross-provider outage stops landings** until a human runs the kill switch — the accepted
  price of refusing a silent fallback — the Bedrock-specific alarm the epic specified does not
  exist, and the alarm that does exist pages nobody (see the gap box above, and
  `9baf-06ce-60ba-4f0b`).
- **The infra in this repo is not gated on being applied** (`1c39-96d4-8f9e-4d38`), so every
  terraform anchor in this ADR describes the tree, not necessarily the account.

**Remaining documentation debt, outside this change's declared scope.** The companion change to
`docs/llm-framework.md` lands with this ADR and closes that file's gaps. `README.md` is NOT in
this story's file impact and remains behind:

| Location | What is wrong |
| --- | --- |
| `README.md` §"Optional runtime capabilities" | the extras list omits `[bedrock]` and its declared resolver conflict with `[eval]` |
| `README.md` §`[agents]` description | *"multi-provider (Claude and ChatGPT out of the box)"* — ChatGPT is NOT out of the box (`[agents]` ships only `pydantic-ai-slim[anthropic,retries]`, `pyproject.toml:122`), and Bedrock is absent (`grep -in bedrock README.md` → 0 hits) |

`docs/env-vars.md` is generated and current — `scripts/gen_env_registry.py --check` exits 0 on a
clean tree. **Caveat to record:** a clean `--check` proves agreement with the GENERATOR, never
completeness — which is exactly why `model_classes.py:99-104` forbids f-string env-var names.

**Scope note for a later reader.** The delivered work is WIDER than the epic's child list shows:
several tickets that produced landed code are not children of `061c` (one closed story carries
`parent=None` along with its whole closed subtree). Judge this ADR's scope by delivered work, not
by `list-descendants`.

---

## Corrections this ADR makes to the record

Recorded explicitly, because each of these appears somewhere in the ticket trail in its wrong
form and would otherwise be re-propagated:

1. **The cutover premise "`--extra bedrock` is required or the `LLM-Review` gate dies" is
   FALSE.** The cutover story's plan asserted that the review-bot image lacked
   `pydantic_ai.providers.bedrock`, so flipping the class slots to `bedrock:` ids without also
   adding the extra would take the gate down. **MEASURED in the running production container:
   `from pydantic_ai.providers.bedrock import BedrockProvider` SUCCEEDS without the extra.** The
   provider module ships in the BASE `pydantic-ai-slim` wheel; slim's `bedrock` extra contributes
   exactly one thing, `boto3>=1.42.63`, and the image already installs `boto3` via the
   `reviewbot` extra at the same version. Three independent plan-review criteria (E4, G6, T3)
   later reached the same conclusion at critical severity. The extra was still added, and
   correctly so, but as **EXPLICITNESS**: the dependency was satisfied only incidentally, by an
   extra that does not exist for that purpose, and an unrelated trim of `reviewbot` would have
   broken Bedrock silently. **The reasoning error is the durable lesson: absence of the EXTRA was
   taken as absence of the MODULE, and the inference was "confirmed" by READING packaging
   metadata rather than by EXECUTING the import in the target environment.** One `docker exec`
   would have settled it.
2. **The retry/timeout half of "first-class = full fidelity" never shipped** — bug
   `61d8-ff23-8ee0-4289` (§"What `first_class` does NOT yet mean").
3. **Bedrock's `cachePoint` DOES have a TTL**, defaulted to 5 minutes — the "no TTL" claim
   appears in at least four places and is wrong (§7).
4. **The IMDS hop-limit contingency never fired.** No terraform change, no
   `0046-security-posture-and-accepted-limitations.md` entry (§9).
5. **The reason for inference profiles is that on-demand ids are refused**, not throttling
   mitigation — zero throttle events were observed (§4).
6. **`bedrock:Converse` should NOT be granted.** The simulator-based recommendation to grant it
   was reversed by a real call (§4).
7. **`AWS_REGION` is not a region source.** rebar's own error text once directed operators to it;
   `4e71-f237-28c4-4c65` corrected that to `AWS_DEFAULT_REGION`, measured on botocore 1.43.62.
8. **"Existence is not reachability."** A claim in the ticket trail that a superseded equality
   rule was still live was retracted: the expression exists, but has zero production call sites
   and a test enforces that nothing calls it. *Verifying existence and inferring reachability are
   not the same thing.*
9. **There is no single interception point for model-class resolution** — one FUNCTION, several
   CALL SITES (`model_classes.py:321-325` says so in-tree, in those words). An earlier "one
   funnel" keystone claim was retracted by its own author after three verified falsifications,
   one of which was that the proposed funnel would have created an import cycle the same author
   had prohibited one ticket earlier.
10. **Bedrock does not straightforwardly "fix" the opus caching failure.** A sweeping claim that
    it does is broader than the evidence and is contradicted within the same ticket set by
    `cache_read=0`/`cache_write=0` on a Bedrock call and by non-caching `us.` and `global.`
    opus-4-5 profiles. **The opus-4-5 datapoint is never reconciled** (§7).
11. **Some measurements were taken in the WRONG AWS ACCOUNT AND REGION** before the operator
    caught it. All landed work is `896586841071 / us-east-1`, and Bedrock model access is enabled
    per account AND per region. Re-measurement was completed in the correct account.
12. **The provider seam was EXONERATED** for one bug initially attributed to it: root cause
    confirmed as two TEST-side causes, seam not implicated.
13. **"The repo-config half of the switch is done" was an overstatement** for most of this epic.
    A per-developer opt-in shipped; the project default did not, so local gates ran on direct
    Anthropic while production ran on Bedrock. Closed by `d2ce-36f5-fd08-4e40` (§10).

### Evidence-hygiene rules earned the hard way

Recorded because they generalize past this seam:

- **Execute the import; do not read the manifest.** Packaging metadata tells you what an extra
  DECLARES, not what the environment CONTAINS (correction 1).
- **Assert `git status --porcelain` is EMPTY before the suite you intend to cite.** CI caught a
  real defect every local run missed: the test edits were never committed. *A suite run against a
  dirty tree is not evidence about the commit.*
- **A simulator answers a different question than a real call** (§4).
- **Never validate a scoped policy with a wildcard-holding identity — and check the account.**
- **Verification that encodes its own blind spot** is a recurring pattern, not bad luck: a
  runbook passing `--region` everywhere; two cache arms reading each other's cache inside the
  5-minute TTL; a pre-existing test pinned at a sub-floor token count; a floor test passing the
  floor as a literal instead of reading it from `capabilities_for`. **In every case the GUARD
  shared the BUG's assumption.**
- **Negative-form ACs pass vacuously.** "A TYPED error, not a bare `NoRegionError`" was already
  true and proved nothing. "A cache breakpoint exists" passes for the wrong reason.
- **An AC that checks for the PRESENCE of a measurement cannot detect a measurement that is
  wrong.**
- **The completion verifier reads REPOSITORY FILES ONLY, never git history** — out-of-repo
  criteria must be tagged `[operator-attested]` or they burn a close cycle.
- **Set `file_impact` BEFORE a review, never after** — an unscoped attestation dies stale-head on
  every commit, and setting `file_impact` after a review is a material edit that destroys the
  PASS.
- **CI cannot see a plan/code contradiction.** Three defects in one landed change — including a
  violation of the change's own single-owner contract — passed both `LLM-Review +1` and
  `Verified +1`. The close-time plan-review re-run caught all three, because it is the only gate
  that reads the PLAN against the CODE.
- **A `file:line` anchor in a Markdown file has no CI guard.** `check_comment_hygiene.py` scans
  Python only. Prefer a symbol or section name in any long-lived document; nine of this ADR's
  inherited anchors had already rotted at authoring time.
