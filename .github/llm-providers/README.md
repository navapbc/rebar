# `REBAR_LLM_CONFIG_FILE` overlays — one per live provider

Each `*.toml` here is a **layered config-file overlay** for `REBAR_LLM_CONFIG_FILE`: it sets
**only** `[llm.model_classes]`, so pointing the variable at one repoints the three model classes
(`trivial` / `standard` / `frontier`) at that provider and **changes nothing else** — the pointer
deep-merges over the discovered config rather than replacing it
(`src/rebar/_config_sources.py`, `docs/local-dev-env.md`).

These files exist so the provider is a **declared, reviewable artefact** instead of a heredoc
inside a CI step. `.github/workflows/external-integration.yml`'s `external-llm` job carries one
matrix arm per file and sets `REBAR_LLM_CONFIG_FILE` to it; `tests/unit/test_ci_provider_matrix.py`
parses both the workflow and these files and fails if an arm's file is missing, sets anything
outside `[llm.model_classes]`, or fails to actually repoint every class.

The deprecated bare `REBAR_LLM_MODEL` is deliberately **not** used: it cannot express a per-class
model, so it could not select a provider for all three classes at once (ADR 0057).

## Local use

The same files work on a workstation — nothing here is CI-specific:

```sh
export REBAR_LLM_CONFIG_FILE="$PWD/.github/llm-providers/bedrock.toml"   # opt in
unset  REBAR_LLM_CONFIG_FILE                                            # revert
```

See `docs/local-dev-env.md` §"Running your local gates on AWS Bedrock instead of direct
Anthropic" for the credential/region prerequisites, and `docs/ci-provider-matrix.md` for the
matrix's cost and cadence decision.

## Model ids are provider-specific and verified, not guessed

- **Bedrock** accepts *inference-profile* ids only (the `us.` / `global.` prefix). A bare
  on-demand `anthropic.claude-*` id raises `ValidationException`. The ids here are the ones
  MEASURED to invoke in account `896586841071` / `us-east-1` (`docs/llm-example-configs.md` §2).
- **OpenAI** ids were verified against the repository's own `OPENAI_API_KEY` with a read-only
  `GET https://api.openai.com/v1/models` on 2026-08-03: `gpt-5.4`, `gpt-5.4-mini` and
  `gpt-5.4-nano` are all present on that account.
