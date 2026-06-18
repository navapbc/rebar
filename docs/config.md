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
   `~/.config` is used on **all** platforms incl. macOS — deliberately *not*
   `~/Library/Application Support` (the predictable dev-tool convention, matching
   ruff/black/mypy). Per the XDG spec a non-absolute `XDG_CONFIG_HOME` is ignored.
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
   dropped them silently — and flip to a hard error under
   `REBAR_CONFIG_UNKNOWN_KEYS=error` (the post-deprecation cutover). An invalid
   *value* always raises `ConfigError` at load (fail-fast). The **verify gate stays
   fail-closed**: a present-but-unreadable `verify.*` config requires a signature to
   close; an absent config leaves the gate off.

## Hard constraints (rebar-specific; deviations from the broader survey)

- **Core stays minimal** — the ONLY runtime dependency is `pyyaml`, the workflow
  DSL loader (`pyproject.toml` `dependencies = ["pyyaml>=6"]`); the engine core and
  reconciler are otherwise stdlib-only, and all LLM/MCP/eval/tracing functionality
  is behind optional extras. So for *config* specifically: **no `pydantic-settings`**
  (use a stdlib `dataclass` + hand-rolled validation) and **no `platformdirs`**
  (resolve XDG by hand via `$XDG_CONFIG_HOME` / `~/.config`).
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

## Config-key inventory

### Config-file keys (fully wired: `[tool.rebar]`/`rebar.toml` → typed Config → consumer)

These are settable in the config file, overridden by `REBAR_<SECTION>_<KEY>` env, then
by `rebar -c SECTION.KEY=VALUE`. Each is consumed by routing through `load_config`.

```toml
[tool.rebar]
# verification gate
verify.require_signature_for_close = false   # alias: verify.require_verdict_for_close

# tickets / display / maintenance
ticket.display_mode      = "auto"
ticket_clarity.threshold = 5      # clarity-check pass threshold (env REBAR_TICKET_CLARITY_THRESHOLD)
compact.threshold        = 10     # env REBAR_COMPACT_THRESHOLD (alias: COMPACT_THRESHOLD)

# sync (git-backed store)
sync.push = "always"   # always | async | off   (env REBAR_SYNC_PUSH; alias REBAR_PUSH)
sync.pull = "on"       # on | off               (env REBAR_SYNC_PULL; alias REBAR_NO_SYNC)

# MCP server gates
mcp.readonly         = false
mcp.allow_llm        = false
mcp.allow_jira_sync  = false   # live (applying) Jira writes (alias env REBAR_MCP_ALLOW_RECONCILE_LIVE)

# scratch space
scratch.base_dir = ""   # default <repo>/.rebar/scratch (env REBAR_SCRATCH_BASE_DIR; alias SCRATCH_BASE_DIR)
```

### Reconciler + Jira tunables — config-file wired (consumed via `load_config`)

`reconciler.*` and `jira.*` are settable in `[tool.rebar.reconciler]` /
`[tool.rebar.jira]` (or `rebar.toml` `[reconciler]`/`[jira]`, or the legacy
`.rebar/config.conf`), reported by `rebar config`, and **consumed** by the Jira
reconciler — the file value is overridden by the env var, then by
`rebar -c SECTION.KEY=VALUE`. Their env overrides keep the ERGONOMIC / Atlassian-
standard names, which deliberately differ from the auto-derived
`REBAR_<SECTION>_<KEY>` (a per-key canonical-env-name map in `config.py`):

```toml
[tool.rebar.reconciler]   # advanced; sensible defaults, rarely needed
jira_cli_timeout       = 0     # acli call timeout (s); 0 ⇒ the 120s default. env REBAR_JIRA_CLI_TIMEOUT (alias REBAR_ACLI_TIMEOUT)
lock_max_retries       = 5     # advisory-lock outer retries.    env REBAR_RECONCILER_LOCK_MAX_RETRIES (alias REBAR_RECONCILER_LOCK_RETRY_BUDGET)
deletion_probe_limit   = 20    # GET probes to confirm a deletion. env REBAR_RECONCILER_DELETION_PROBE_LIMIT (alias RECONCILER_ABSENT_GET_BUDGET)
id_guard_bypass_unsafe = false # TEMPORARY bypass of the rebar-id write guard — do NOT leave on; fail-CLOSED.
                               # env REBAR_UNSAFE_ID_GUARD_BYPASS; deprecated REBAR_ID_GUARD_MODE env + legacy flat
                               # `rebar_id_guard_mode` key (value-flip: warn→true/bypass, raise→false/guard)

[tool.rebar.jira]   # Atlassian-standard, UNPREFIXED env names
url     = ""   # env JIRA_URL
user    = ""   # env JIRA_USER
project = ""   # env JIRA_PROJECT  (the reconciler substitutes "DIG" when empty on CREATE)
```

The SECRET `JIRA_API_TOKEN` stays env-only — never a config key (see Secrets).

### LLM framework (`llm.*`) — optional `[agents]` extra, `[tool.rebar.llm]`

`llm.*` is resolved by the optional `rebar.llm` layer (`LLMConfig.from_env`), NOT
the stdlib-core typed Config — so importing `rebar.llm` never pulls the agents
stack into core, and `llm.*` is **not** reported by `rebar config`. It is a
*reserved* section: the core loader recognises `[tool.rebar.llm]` and never warns
on it (nor rejects it under `REBAR_CONFIG_UNKNOWN_KEYS=error`), but does not parse
it into `Config`. The non-secret knobs are settable in the file and resolved
`rebar -c llm.KEY=VALUE` > `REBAR_LLM_<KEY>` env > config file > default:

```toml
[tool.rebar.llm]
model          = "claude-opus-4-8"   # env REBAR_LLM_MODEL
model_provider = ""                  # env REBAR_LLM_MODEL_PROVIDER (inferred from the model name when empty)
base_url       = ""                  # env REBAR_LLM_BASE_URL (OpenAI-compatible endpoint)
max_tokens     = 8000                # env REBAR_LLM_MAX_TOKENS
max_steps      = 25                  # env REBAR_LLM_MAX_STEPS (alias REBAR_LLM_MAX_ITERS); ~2 steps per tool call
timeout        = 600                 # env REBAR_LLM_TIMEOUT (wall-clock s)
mcp_servers    = {}                  # env REBAR_LLM_MCP_SERVERS (JSON); a TOML inline table in-file
```

Env-only (NOT `[tool.rebar.llm]` keys): the secret `REBAR_LLM_API_KEY`; the
runtime-only `REBAR_LLM_REPO_PATH` (which repo the review agent's read-only file
tools see — an invocation-specific override, default the repo root); and the
DERIVED runner — `REBAR_LLM_EXPERIMENTAL_HARNESS=deepagents` opts into the
experimental harness, else in-process `langgraph` (`fake` is a library-arg-only
test seam).

### Secrets — environment / `.env` only (never the config file)

`REBAR_SIGNING_KEY`, `REBAR_LLM_API_KEY`, `JIRA_API_TOKEN`,
and the SDK-standard `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`LANGFUSE_SECRET_KEY`. (`LANGFUSE_*` is read by its own SDK and keeps its
standard names.)

### Removed / derived (not config keys)

- **Removed:** `paths.*`, `planning.external_dependency_block_enabled` (deleted),
  and the legacy `rebar_id_guard_mode` flat key (→ `reconciler.id_guard_bypass_unsafe`).
- **Derived, not configured:** the LLM runner (in-process langgraph by default;
  `fake` is a test-only injection; `deepagents` is gated by
  `REBAR_LLM_EXPERIMENTAL_HARNESS`).
- **Runtime-only (env, not a config key):** `REBAR_LLM_REPO_PATH` — which repo the
  review agent's read-only file tools see (default: the repo root). It is an
  invocation-specific runtime override, so it stays an env var and is **not** a
  persistent `[tool.rebar]` setting.

## Back-compat

The legacy flat `.rebar/config.conf` keeps being read **identically for ≥1
release**; legacy key names are aliased (e.g. `verify.require_verdict_for_close`).
`ticket_clarity.threshold` (clarity-check pass threshold, default 5) is a typed key —
settable in `[tool.rebar.ticket_clarity]`/`rebar.toml`, via `REBAR_TICKET_CLARITY_THRESHOLD`,
or the legacy flat `ticket_clarity.threshold` (the section name matches, so it reads
with no alias); it appears in `rebar config`.
Unknown keys **warn** (not fail) during the deprecation window and hard-error under
`REBAR_CONFIG_UNKNOWN_KEYS=error`. Renamed env vars keep their old names as
deprecated aliases (with a warning): `REBAR_PUSH`→`REBAR_SYNC_PUSH`,
`REBAR_NO_SYNC`→`REBAR_SYNC_PULL` (negative→positive flip), `COMPACT_THRESHOLD`→
`REBAR_COMPACT_THRESHOLD`, `SCRATCH_BASE_DIR`→`REBAR_SCRATCH_BASE_DIR`,
`REBAR_MCP_ALLOW_RECONCILE_LIVE`→`REBAR_MCP_ALLOW_JIRA_SYNC`, `TICKETS_TRACKER_DIR`→
`REBAR_TRACKER_DIR`, `REBAR_ACLI_TIMEOUT`→`REBAR_JIRA_CLI_TIMEOUT`,
`REBAR_RECONCILER_LOCK_RETRY_BUDGET`→`REBAR_RECONCILER_LOCK_MAX_RETRIES`,
`RECONCILER_ABSENT_GET_BUDGET`→`REBAR_RECONCILER_DELETION_PROBE_LIMIT`,
`REBAR_LLM_MAX_ITERS`→`REBAR_LLM_MAX_STEPS`, `REBAR_ID_GUARD_MODE`→
`REBAR_UNSAFE_ID_GUARD_BYPASS` (raise→false/warn→true). Removed (no alias):
`PROJECT_ROOT` (use `REBAR_ROOT`), `REBAR_LLM_RUNNER` (runner is derived), and the
dead `TICKET_CMD`/`REBAR_TICKET_CLI`/`TICKET_WORDLIST_PATH`/`TICKET_SYNC_CMD`/
`_REBAR_GC_AUTO_ZERO`/`REBAR_FSCK_NO_MUTATE` internals. See the env-var
standardization story `60ce`.

## Transparency

`rebar config` (a.k.a. `--show-config`) prints the resolved values and **which
layer** each came from (the ruff `--show-settings` / pip `config debug` pattern).
