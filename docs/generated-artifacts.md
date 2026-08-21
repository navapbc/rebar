# Generated artifacts

This catalog identifies checked-in generated files and hand-authored files whose parity is enforced. Correct a generated file through the source and regeneration command listed below. Ticket `forworn-zanyish-narwhale` records the adoption history.

### Generated files

Each generated file carries a banner near the top or a top-level `_generated_by` key for formats without comment syntax.

| File | Derived from | Regenerate with | Enforcing CI gate |
|---|---|---|---|
| `docs/cli-reference.md` | The immutable CLI route registry (`rebar._cli._registry.ROUTES`) and committed package-help bytes (`rebar._cli._help`). Help-backed subcommands embed pinned help. Intercept commands render from their route `parser_factory`. | `python scripts/gen_cli_reference.py` | CLI-reference drift gate |
| `docs/config-reference.md` | the typed config schema (`rebar._config_schema._SECTION_CLASSES`) plus the `cfg`-kind deprecations/tombstones in `rebar._deprecations` | `python scripts/gen_config_reference.py` | Config-reference drift gate |
| `docs/security.md` | the adapter send-credential name registry (`rebar._child_env._ADAPTER_SECRET_NAMES`) | `python scripts/gen_config_reference.py` | Config-reference drift gate |
| `docs/env-vars.md` | an AST scan of `src/rebar/**/*.py` for env reads, plus `rebar._deprecations.REGISTRY` and `rebar.mcp_server.MCP_ENV_VARS` | `python scripts/gen_env_registry.py` | Env-var registry drift gate |
| `docs/mcp-reference.md` | the MCP server's own tool registrars and their docstrings | `python scripts/gen_mcp_reference.py` | MCP-reference drift gate |
| `docs/plan-review-criteria-guide.md` | the merged criteria registry (`rebar.llm.plan_review.registry.load_criteria`) | `python -m rebar.llm.plan_review.registry regenerate-criteria-guide` | Criteria-routing parity gate |
| `src/rebar/types.py` | the canonical JSON Schemas in `src/rebar/schemas/*.schema.json` | `python -m rebar.schemas.gen_types` | Public-types drift gate |
| `src/rebar/llm/reviewers/index.json` | the packaged prompt front-matter in `src/rebar/llm/reviewers/*.md` | `python -m rebar.llm.prompting.prompts regenerate-index` | Prompt-index drift gate |
| `src/rebar/_guides/criterion-pins.json` | digests of the criteria cited by the `src/rebar/_guides/*.md` prose guides | `python -m rebar.llm.plan_review.guide_parity regenerate` | Criteria-routing parity gate |

### Hand-authored parity-gated files

These files are hand-authored. Their parity gates compare them with an identified source of truth. Edit them only when the corresponding contract changes.

| File | Checked against | Check with | Enforcing CI gate |
|---|---|---|---|
| `server.json` | The `packages[0].environmentVariables` block mirrors `rebar.mcp_server.MCP_ENV_VARS`. The remaining content is hand-authored. | `python scripts/check_server_manifest.py` | server.json env-contract drift gate |
| `src/rebar/llm/plan_review/criteria_routing.json` | The canonical criteria vocabulary. Thresholds and `applies_at` require author judgment because no derivation source exists. | `python -m rebar.llm.plan_review.registry validate-routing` | Criteria-routing parity gate |
| `src/rebar/llm/reviewers/*.md` | The prompt body is authored freely. The front matter must be a fixed point of `write_front_matter()`. Hand-wrapped YAML can be semantically correct while failing the gate. | `pytest tests/unit/test_prompt_front_matter.py` | pytest (`test_built_in_prompt_round_trips_canonically`) |

`tests/unit/test_generated_surface_markers.py` pins both tables to their source registries and verifies each generated marker.
