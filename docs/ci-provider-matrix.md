# The external suite's live-provider matrix

The external-integration suite is the only place rebar exercises a **real** LLM provider end to
end. Until story `f124` it ran on **one** provider: every live job gated on `ANTHROPIC_API_KEY`,
so the provider seam rebar built — first-class Anthropic **and** Bedrock, best-effort
OpenAI-compatible — had exactly one arm under live test. A Bedrock-only regression (a `cachePoint`
envelope change, an inference-profile id that stops resolving, structured output degrading, a new
throttling shape) was **undetectable by CI**; it could only be found by a human running the gates
by hand, which is not automation.

This is deliberately a **matrix, not a switch**. Flipping the single arm from Anthropic to Bedrock
would trade one blind spot for another.

Workflow: `.github/workflows/external-integration.yml`. Overlays: `.github/llm-providers/`.
Guard tests: `tests/unit/test_ci_provider_matrix.py` (the workflow's shape) and
`tests/external/test_provider_matrix_live.py` (the realised arm).

## Shape

| job | selection | runs |
|---|---|---|
| `external-llm` | `-m "external and llm_live"` | **once per provider** (matrix: anthropic, bedrock, openai) |
| `external` | `-m "external and not llm_live"` | once (live Jira Cloud, deps, link-sync) |
| `langfuse-trace` | one module by path | once (default provider) |
| `jira-dc-harness` | `tests/external/live_jira_dc` | once |

The two `-m` selections **partition** the tier: the union is exactly the set that ran before the
split. They must stay disjoint for a second reason — each job's all-skip canary counts
collected-vs-executed **globally per session**, so live-LLM tests executing inside the `external`
job would mask an all-skip of the Jira tests, and vice versa.

`llm_live` is applied automatically to any `tests/external` module that declares a module-level
`_live_llm_ready` sentinel (`tests/external/conftest.py`), so a **new** live-LLM module joins the
matrix without editing the workflow.

## Provider selection: `REBAR_LLM_CONFIG_FILE`, not `REBAR_LLM_MODEL`

Each arm points `REBAR_LLM_CONFIG_FILE` at `.github/llm-providers/<provider>.toml`, a file setting
**only** `[llm.model_classes]`. Three reasons this is the right instrument rather than bespoke
per-arm CI wiring:

1. It is the mechanism rebar already **documents** for exactly this purpose
   (`docs/local-dev-env.md`), so CI and a developer's workstation use the same switch.
2. It **deep-merges** over the discovered config instead of replacing it, so an arm overrides
   provider and models and *nothing else* — any difference observed between arms is attributable
   to the provider.
3. CI therefore **dogfoods** the override path, giving the pointer live coverage on every run
   instead of only when someone uses it by hand.

The deprecated bare `REBAR_LLM_MODEL` is explicitly not used: it applies one model to all three
classes, so it cannot express a per-class provider selection (ADR 0057).

`tests/unit/test_ci_provider_matrix.py::test_an_overlay_repoints_every_class_and_preserves_the_discovered_config`
runs each committed overlay through rebar's **real** config layering and asserts both halves —
all three classes repointed, and the discovered config's unrelated keys untouched.

## Credentials, and why an unconfigured arm is RED

| provider | credential | tier |
|---|---|---|
| anthropic | `ANTHROPIC_API_KEY` secret | first-class |
| bedrock | ambient AWS chain via **OIDC role assumption** — `infra/runbooks/bedrock-ci-oidc.md` | first-class |
| openai | `OPENAI_API_KEY` secret | best-effort |

Each arm receives **only its own** provider's credential: every key expression is guarded on
`matrix.provider`, so the Bedrock arm cannot see `ANTHROPIC_API_KEY` and cannot fall back to direct
Anthropic on a path that reads a key rather than the resolved model string. Asserted statically
(`test_every_api_key_expression_is_guarded_on_the_arms_provider`,
`test_the_bedrock_arm_receives_no_llm_api_key`) and again at runtime from inside the arm
(`test_the_arm_carries_no_other_providers_credential`).

**An arm with no credential fails; it does not skip.** "Loud, and never green" has one unambiguous
rendering in GitHub Actions and it is a red job — a skipped job renders grey/neutral and is
trivially read as fine, and the `secrets` context is not even available in a job-level `if:`, so a
genuine per-arm skip is not expressible anyway. Two independent defences:

1. **The credential preflight** emits `::error::` and exits non-zero when the arm's credential (or,
   for Bedrock, its role-ARN / region variable) is absent.
2. **The `llm-live-canary`** (`tests/external/conftest.py`) flips the session to a failure when at
   least one `llm_live` test was collected and **none executed** — which is what happens if a
   credential is present but wrong for the provider the overlay selected. Measured locally: with
   the openai overlay and no `OPENAI_API_KEY`, the run reports
   `[llm-live-canary] collected=14 executed=0 skipped=14` and exits **1**.

### Bedrock: region is a separate, mandatory setting

The arm sets **both** `AWS_DEFAULT_REGION` and `REBAR_LLM_BEDROCK_REGION`, from one repository
variable. This is not belt-and-braces; it is measured. Ticket `a574`:
`boto3.session.Session().region_name` is `None` on a host with a **working** instance role, so IMDS
reachability supplies no region and credential discovery says nothing about region discovery — and
rebar's own knob **alone was insufficient**, `AWS_DEFAULT_REGION` was required too. rebar's knob is
still set because it is what puts the region into the verdict's `provider_provenance`, which a bare
`AWS_*` var does not.

rebar invents no default region: with none resolving, `build_bedrock_provider` raises a typed
`LLMConfigError` naming `REBAR_LLM_BEDROCK_REGION`
(`tests/unit/test_bedrock_provider.py::test_missing_region_raises_a_typed_error_naming_the_setting`),
never a bare boto3 `NoRegionError` and never a silent skip.

## Cost — measured, not estimated

Measured from the **real** `external-usage-log` artifacts of three consecutive runs of the
pre-split job on `main` (GitHub Actions runs `30772461871`, `30784592232`, `30792766156`; 1–3 Aug
2026). Each run made **29 LLM calls** across `claude-opus-4-8` (19), `claude-sonnet-4-6` (9) and
`claude-haiku-4-5` (1).

| run | calls | input tokens¹ | output | cache read | cache write | Anthropic | Bedrock² | OpenAI³ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 30772461871 | 29 | 493,296 | 33,057 | 227,406 | 30,481 | **$1.95** | $2.14 | ~$0.83 |
| 30784592232 | 29 | 483,775 | 33,792 | 198,764 | 48,028 | **$2.04** | $2.25 | ~$0.85 |
| 30792766156 | 29 | 546,654 | 36,388 | 243,070 | 39,914 | **$2.15** | $2.36 | ~$0.91 |

¹ `input_tokens` as logged is **inclusive** of cache read and cache write (verified against
genai-prices' own extraction and pricing arithmetic), so billable uncached input is
`input − cache_read − cache_write`.
² Bedrock `us.*` are **regional** inference profiles, which list at exactly **+10%** on every one
of the four token rates versus Anthropic direct (genai-prices offline price data, read 2026-08-03).
Same models, same token counts, so the delta is purely the provider.
³ **Estimated, not measured** — no OpenAI run exists yet. Same token volumes priced at
`gpt-5.4` / `gpt-5.4-mini` / `gpt-5.4-nano` list, mapping frontier/standard/trivial. OpenAI bills
no cache-*write* premium, which is most of why it lands lower. Replace this column with the arm's
first real usage log.

**Per-trigger total for the full matrix: ~$4.90–$5.40**, against ~$2.00–$2.15 for the single arm it
replaces.

Rates were read from genai-prices' offline data rather than typed from memory. Note the logs
themselves report `est. cost —` because `_price_row` passes the stored `provider:model` string
straight to `genai_prices.calc_price`, which wants a bare model id and raises `LookupError` on the
prefixed form — the numbers above were therefore computed from the logged token counts and the
published rates. That is a pre-existing defect in `rebar.llm.usage_log`, not a property of this
matrix, and it is worth fixing separately (it would make this table self-maintaining).

## Cadence decision

**Every credentialed provider runs on every trigger, including the weekly schedule.**

Rationale, with the tradeoff stated rather than implied:

- The incremental spend is **~$2.25/trigger** for Bedrock and **~$0.9/trigger** for OpenAI — about
  **$13/month** on top of the existing ~$9/month at the weekly cadence.
- A reduced cadence (say Bedrock monthly) would leave a Bedrock-only regression undetected for up
  to a month. The entire purpose of this lane is to shorten that window, so paying ~$13/month to
  keep it at a week is the right side of the trade.
- A reduced *case subset* per provider was also rejected: the modules in the `llm_live` lane are
  already the minimum that covers the distinct paths (findings mode, structured output, the close
  gate, text mode, the workflow agent bridge). Dropping any of them removes a path from
  provider-comparability, which is the only thing the arms are for.
- `fail-fast: false`, so one provider's outage or regression does not cancel the others.

**Revisit when:** manual `workflow_dispatch` volume makes the spend material. The run history shows
several dispatches per day during active work, which at ~$5.40 each is the real cost driver — not
the schedule. The obvious next step is a `workflow_dispatch` input narrowing the matrix to one
provider, deliberately **not** built yet: it needs a dynamic matrix (a planning job plus
`fromJSON`) whose branching would itself be unverifiable YAML logic, which is a poor trade against
a static, fully-asserted matrix until the spend justifies it.

## See also

- `infra/runbooks/bedrock-ci-oidc.md` — the IAM role the Bedrock arm assumes (trust + permission
  policy, creation, verification).
- `infra/runbooks/bedrock-access.md` — the review bot's instance-role Bedrock path and how **not**
  to verify Bedrock access.
- `docs/local-dev-env.md` — running your local gates on Bedrock with the same overlay files.
- `docs/llm-example-configs.md` — the per-provider `[llm.model_classes]` tables and the measured
  inference-profile id constraints.
