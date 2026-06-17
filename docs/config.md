# rebar configuration — design of record (ADR) + reference

> Status: **accepted** (config-refinement epic `a621`). This is the design the
> implementation tasks build to, and the canonical reference for rebar's config
> surface once they land. Grounded in a survey of how actively-maintained Python
> CLI/dev-tools (ruff, black, mypy, pytest, pip, uv, poetry, coverage) handle
> configuration, adapted to rebar's hard constraints.

## Decision summary

1. **Format — TOML.** Config lives under **`[tool.rebar]` in `pyproject.toml`**, or
   in a standalone **`rebar.toml`**, parsed with the stdlib **`tomllib`**. This
   replaces the bespoke flat `key=value` `.rebar/config.conf` parser (which
   silently mishandles `=` in values, has no types/nesting, and drops unknown keys
   silently). TOML is the converged-on default for modern Python tooling (PEP
   518/621/680).
2. **Discovery — project-first, XDG-user fallback.** Resolution walks **up** from
   the cwd to the nearest project config (first of `rebar.toml`, a `pyproject.toml`
   containing `[tool.rebar]`, stopping at `.git`/filesystem root). If no project
   config is found, fall back to a **user-level** config at
   `$XDG_CONFIG_HOME/rebar/config.toml` (default `~/.config/rebar/config.toml`).
3. **Precedence (highest → lowest):**
   **CLI flag > `REBAR_<KEY>` env var > project config > user config > built-in
   defaults.** Documented and enforced by a single resolver.
4. **One typed source of truth.** A single stdlib `dataclass` Config (extending
   `src/rebar/config.py`) owns defaults, parsing, validation, and the layering
   above. Every config read routes through it; the two ad-hoc inline parsers
   (`_engine_support/lookups.py::_display_mode`, `_commands/txn.py` verify-gate
   loop) are retired.
5. **Settings vs. secrets.** Non-secret *settings* live in the config file (with a
   `REBAR_<KEY>` env override). *Secrets* live **only** in the environment / a
   gitignored `.env`, never the committed config.
6. **Loud, not silent.** Unknown keys **warn** (typo guard) — the bespoke parser
   dropped them silently. The **verify gate stays fail-closed**: a present-but-
   unreadable `verify.*` config requires a signature to close; an absent config
   leaves the gate off.

## Hard constraints (rebar-specific; deviations from the broader survey)

- **Core is stdlib-only** (`pyproject.toml` `dependencies = []`). So **no
  `pydantic-settings`** (use a stdlib `dataclass` + hand-rolled validation) and
  **no `platformdirs`** (resolve XDG by hand via `$XDG_CONFIG_HOME` / `~/.config`).
- **`tomllib` needs no fallback.** The runtime floor is already
  `requires-python = ">=3.11"`, so `tomllib` is in the stdlib. (The ruff
  `target-version = "py310"` is a *lint* target only — unrelated to runtime.)
- **Config is working-tree content, not events.** Config files (`pyproject.toml` /
  `rebar.toml` / `.rebar/config.conf`) are ordinary repo content on the working
  branch — **not** events on the `tickets` orphan branch. There is no event-log
  migration and config is not auto-pushed/merged by the store; back-compat is
  plain file compatibility.

## Precedence resolution

For any key, the first layer that provides a value wins:

```
1. explicit CLI flag                 (e.g. --no-pull, --output)
   + `rebar -c SECTION.KEY=VALUE …`  (git -c style, repeatable, before the subcommand)
2. REBAR_<KEY> environment variable  (dots → underscores, uppercased)
3. project config  (rebar.toml | [tool.rebar] in pyproject.toml, nearest up-tree)
4. user config     ($XDG_CONFIG_HOME/rebar/config.toml | ~/.config/rebar/config.toml)
5. built-in default
```

The generic `rebar -c SECTION.KEY=VALUE <subcommand>` override populates the
highest-precedence **`cli`** layer for the whole invocation (every consumer — the
verify gate, push/pull policy, display mode, …). `rebar config -c sync.push=off`
shows the resolved value tagged `[cli]`. A malformed `-c` pair (missing `=` or a
non-dotted key) is a clean error, not a traceback.

`$REBAR_CONFIG` (a file path) short-circuits discovery and names the project
config explicitly (matches `RUFF_CONFIG` / `PIP_CONFIG_FILE` / `UV_CONFIG_FILE`).
Env var naming: a dotted config key `x.y_z` is overridden by `REBAR_X_Y_Z`.

## Config-key inventory (settings → config file)

```toml
[tool.rebar]
# verification gate
verify.require_signature_for_close = false   # alias: verify.require_verdict_for_close

# tickets / display / maintenance
ticket.display_mode  = "auto"
compact.threshold    = 10

# sync (git-backed store)
sync.push = "always"   # always | async | off
sync.pull = "on"       # on | off

# MCP server gates
mcp.readonly         = false
mcp.allow_llm        = false
mcp.allow_jira_sync  = false   # was: allow live (applying) Jira writes

# LLM framework (optional [agents] extra)
llm.model         = "claude-opus-4-8"
llm.model_provider = ""         # inferred from model when empty
llm.base_url      = ""          # OpenAI-compatible endpoint
llm.max_tokens    = 8000
llm.max_steps     = 25          # max agent loop steps (~2 per tool call); raise if "exceeded step budget"
llm.timeout       = 600         # wall-clock seconds
llm.mcp_servers   = {}          # TOML table (retires the JSON-in-env footgun)

# Jira reconciler
jira.url     = ""
jira.user    = ""
jira.project = ""

# reconciler tunables (advanced — sensible defaults, rarely needed)
reconciler.jira_cli_timeout       = 0     # acli (Atlassian CLI) call timeout
reconciler.lock_max_retries       = 5     # advisory-lock acquisition retries
reconciler.deletion_probe_limit   = 20    # GET probes to confirm a Jira issue is really deleted
reconciler.id_guard_bypass_unsafe = false # TEMPORARY bypass of the rebar-id write guard — do NOT leave on

# scratch space
scratch.base_dir = ""   # default <repo>/.rebar/scratch
```

### Secrets — environment / `.env` only (never the config file)

`REBAR_SIGNING_KEY`, `REBAR_LLM_API_KEY`, `JIRA_API_TOKEN`, `LANGFLOW_API_KEY`,
and the SDK-standard `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`LANGFUSE_SECRET_KEY`. (`LANGFUSE_*` / `LANGFLOW_URL`/`_FLOW_ID` are read by their
own SDKs and keep their standard names.)

### Removed / derived (not config keys)

- **Removed:** `paths.*`, `planning.external_dependency_block_enabled` (deleted),
  and the legacy `rebar_id_guard_mode` flat key (→ `reconciler.id_guard_bypass_unsafe`).
- **Derived, not configured:** the LLM runner (Langflow is used when
  `LANGFLOW_URL`+`LANGFLOW_FLOW_ID` are set, else in-process langgraph; `fake` is a
  test-only injection; `deepagents` is gated by `REBAR_LLM_EXPERIMENTAL_HARNESS`).
- **Runtime-only (env, not a config key):** `REBAR_LLM_REPO_PATH` — which repo the
  review agent's read-only file tools see (default: the repo root). It is an
  invocation-specific runtime override, so it stays an env var and is **not** a
  persistent `[tool.rebar]` setting.

## Back-compat

The legacy flat `.rebar/config.conf` keeps being read **identically for ≥1
release**; legacy key names are aliased (e.g. `verify.require_verdict_for_close`).
Unknown keys **warn** (not fail) during the deprecation window; hard-error only
after. Renamed env vars (`REBAR_PUSH`→`REBAR_SYNC_PUSH`, `TICKETS_TRACKER_DIR`→
`REBAR_TRACKER_DIR`, …) keep their old names as deprecated aliases — see the
env-var standardization story `60ce`.

## Transparency

`rebar config` (a.k.a. `--show-config`) prints the resolved values and **which
layer** each came from (the ruff `--show-settings` / pip `config debug` pattern).
