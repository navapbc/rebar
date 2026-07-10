# rebar configuration — design of record (ADR) + reference

> Status: **accepted** (config-refinement epic `a621`). This is the design the
> implementation tasks build to, and the canonical reference for rebar's config
> surface once they land. Grounded in a survey of how actively-maintained Python
> CLI/dev-tools (ruff, black, mypy, pytest, pip, uv, poetry, coverage) handle
> configuration, adapted to rebar's hard constraints.

## Decision summary

1. **Format — TOML.** Config lives under **`[tool.rebar]` in `pyproject.toml`**, or
   in a standalone **`rebar.toml`**, parsed with the stdlib **`tomllib`**. This
   replaced the bespoke flat `key=value` `.rebar/config.conf` parser (removed
   pre-1.0 — DE7 — which
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
  `rebar.toml`) are ordinary repo content on the working
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
verify.require_signature_for_close = false
verify.verify_window_headroom      = 0.8     # plan-review Pass-2 verify: fraction of the verifier
                                             # model window a single verify request may use before
                                             # the findings are split into multiple calls (0.1–1.0)
verify.progressive_drift_refresh   = false   # progressively refresh drifted findings during plan review
verify.require_completion_verification_for_close = false  # gate work-ticket close on a PASS completion
                                             # verdict (signed onto the ticket); fail-closed. ON for
                                             # this project's rebar.toml. Alternative to the signature gate.
verify.require_plan_review_for_claim = false # gate claim on a successful (non-BLOCK) plan review attestation
verify.require_ticket_for_commit   = false   # CI Verified gate: every commit to main must reference a rebar
                                             # ticket that RESOLVES in the store (rebar-ticket: <id> trailer or a
                                             # leading <id>:; alias/full/short/Jira). env
                                             # REBAR_VERIFY_REQUIRE_TICKET_FOR_COMMIT. See docs/commit-ticket-trailer.md
verify.remediation_mode            = false   # convergent plan-edit re-review (epic 7d43): when on, a
                                             # re-review of an edited plan whose CODE is unchanged may
                                             # drop only NOVEL low-priority findings (the rising floor).
                                             # Default OFF (expand-contract rollout); off restores a
                                             # byte-identical full review (the back-out).
verify.remediation_window_minutes  = 60      # remediation-mode freshness window: a re-review is eligible
                                             # only if the last review of any kind was within this many
                                             # minutes (measured from it, reset on each review). Min 1.
verify.novelty_drop_threshold      = 0.7     # T_novel: a finding is droppable only if its novelty >= this
verify.novelty_priority_floor      = 0.4     # rising floor: drop a novel finding only if priority < this
                                             # (scalar ≈ corpus p40 impact; see the distribution script)
verify.novelty_drop_active         = false   # EVIDENCE GATE: the rising floor stays inert until flipped
                                             # true (manually, after the discriminates_novelty eval clears)

# tickets / display / maintenance
ticket.display_mode      = "auto"
ticket.default_assignee  = ""     # assignee `claim` uses when --assignee is omitted (env REBAR_DEFAULT_ASSIGNEE)
ticket_clarity.threshold = 5      # clarity-check pass threshold (env REBAR_TICKET_CLARITY_THRESHOLD)
compact.threshold        = 10     # env REBAR_COMPACT_THRESHOLD (alias: COMPACT_THRESHOLD)

# sync (git-backed store)
sync.push   = "always"  # always | async | off   (env REBAR_SYNC_PUSH)
sync.pull   = "on"      # on | off               (env REBAR_SYNC_PULL; alias REBAR_NO_SYNC)
sync.remote = "origin"  # git remote the tickets branch syncs to — push/fetch/reconcile, the
                        # fsck PUSH_PENDING check, and attested ticket-store materialization
                        # (env REBAR_SYNC_REMOTE). Set it for split residency: e.g. the tickets
                        # branch's source of truth on origin=GitHub while code review lives on a
                        # separate `gerrit` remote. Validated as a git remote name (rejects
                        # spaces / `:` / `~` / `/` / control; dots + non-leading hyphens allowed).

# MCP server gates
mcp.readonly         = false
mcp.allow_llm        = false
mcp.allow_jira_sync  = false   # live (applying) Jira writes (env REBAR_MCP_ALLOW_JIRA_SYNC)

# scratch space
scratch.base_dir = ""   # default <repo>/.rebar/scratch (env REBAR_SCRATCH_BASE_DIR; alias SCRATCH_BASE_DIR)

# ticket store (worktree/symlink dir + orphan branch) — both default to today's values
tracker.dir    = ".tickets-tracker"  # env REBAR_TRACKER_DIR; a bare
                                      # relative name (the repo-root symlink + gitignore entry) or an
                                      # absolute path to relocate the store (EV-3b). Validated: no
                                      # empty / `..` traversal / control chars.
tracker.branch = "tickets"           # env REBAR_TRACKER_BRANCH; the orphan branch the event log lives
                                      # on (+ its origin/<branch> ref). Validated as a git ref:
                                      # rejects spaces, `..`, leading `-`, ~^:?*[\ / control, trailing
                                      # `/` or `.lock`.

# idempotent ensure-registry pending-hint (epic odd-vortex-elbow; see docs/migrations.md)
ensure.hint_interval_secs = 86400    # min seconds between write-path "store is behind the ensure
                                      # registry — run `rebar fsck --repair`" nudges, per store, per
                                      # process (rate-limit; env REBAR_ENSURE_HINT_INTERVAL_SECS). Min 0.
ensure.hint_enabled       = true     # kill-switch: false silences the nudge entirely
                                      # (env REBAR_ENSURE_HINT_ENABLED)
```

> **Resolution change (tracker.dir).** `tracker_dir()` (and the new `tickets_branch()`) now
> resolve through the full precedence chain (`-c` flag > `REBAR_<KEY>` env > project > user >
> default), not the env-only path used historically. `REBAR_TRACKER_DIR` is
> the canonical env override (the removed `TICKETS_TRACKER_DIR` alias is no longer honored — DE7).
>
> **Set at `init`, not auto-migrated.** Both values are read at `rebar init` and on every
> read/write thereafter. Changing `tracker.dir`/`tracker.branch` on an **already-initialized**
> repo does **not** migrate the existing store — it orphans the old branch/dir (the old data is
> left intact but unreferenced). Renaming an existing store is a separate migration and is out
> of scope; `rebar fsck` WARNs when the configured branch/dir does not match what is actually
> mounted so the divergence is observable.

### Reconciler + Jira tunables — config-file wired (consumed via `load_config`)

`reconciler.*` and `jira.*` are settable in `[tool.rebar.reconciler]` /
`[tool.rebar.jira]` (or `rebar.toml` `[reconciler]`/`[jira]`), reported by `rebar config`, and **consumed** by the Jira
reconciler — the file value is overridden by the env var, then by
`rebar -c SECTION.KEY=VALUE`. Their env overrides keep the ERGONOMIC / Atlassian-
standard names, which deliberately differ from the auto-derived
`REBAR_<SECTION>_<KEY>` (a per-key canonical-env-name map in `config.py`):

```toml
[tool.rebar.reconciler]   # advanced; sensible defaults, rarely needed
jira_cli_timeout       = 0     # acli call timeout (s); 0 ⇒ the 120s default. env REBAR_JIRA_CLI_TIMEOUT (alias REBAR_ACLI_TIMEOUT)
# pass-lock/phase-gate backend: the self-healing refs/reconciler/* CAS lock is the ONLY backend.
# The `lock_backend` key + its legacy accepted-but-ignored "file" value were removed pre-1.0
# (ticket unclear-verymad-sablefish); a still-present key is ignored as unknown. See ADR 0031.
lock_lease_secs        = 120   # ref-lock lease (s); the heartbeat renews at max(1, lease // 3).
deletion_probe_limit   = 20    # GET probes to confirm a deletion. env REBAR_RECONCILER_DELETION_PROBE_LIMIT (alias RECONCILER_ABSENT_GET_BUDGET)
# Removed in the dust-troth-naval epic: `lock_max_retries` (+ env REBAR_RECONCILER_LOCK_MAX_RETRIES /
# REBAR_RECONCILER_LOCK_RETRY_BUDGET) — it tuned the b859 outer-retry loop, now superseded by the
# self-healing ref lock. A still-present key is ignored with a one-time deprecation warning (not a load error).
id_guard_bypass_unsafe = false # TEMPORARY bypass of the rebar-id write guard — do NOT leave on; fail-CLOSED.
                               # env REBAR_UNSAFE_ID_GUARD_BYPASS; permanent alias REBAR_ID_GUARD_MODE env
                               # (value-flip: warn→true/bypass, raise→false/guard). The legacy flat
                               # `rebar_id_guard_mode` config key is no longer honored (removed pre-1.0).

[tool.rebar.jira]   # Atlassian-standard, UNPREFIXED env names
url     = ""   # env JIRA_URL
user    = ""   # env JIRA_USER
project = ""   # env JIRA_PROJECT  (the reconciler substitutes "DIG" when empty on CREATE)
```

The SECRET `JIRA_API_TOKEN` stays env-only — never a config key (see Secrets).

**Env-only reconciler flag — silent-no-op canary** (not a config-file key; epic
f89d, story 2359). `REBAR_RECONCILER_FAIL_SILENT_NOOP` (default off ⇒ **warn-first**):
an outbound update whose sub-ops are *computed but none applied*
(`computed > 0 && applied == 0` — the bug-3f04 link-drop mode; `computed` is counted
**post-dedup** so an idempotent re-sync never trips it) is always surfaced on the batch
outcome (`silent_noop` + `links_applied`/`comments_applied`/`labels_applied`) and
`WARNING`-logged. Set to `1` to **promote** it to a hard per-mutation failure;
promotion and reversion are a pure flag flip — no other code change. (It is a
*total* per-kind no-op detector: a partial drop — e.g. 1 of 2 links applied — does
not fire; the simple `applied == 0` invariant is the contract.)

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
max_tokens     = 16000               # env REBAR_LLM_MAX_TOKENS
max_steps      = 50                  # env REBAR_LLM_MAX_STEPS; ~2 steps per tool call
timeout        = 600                 # env REBAR_LLM_TIMEOUT (wall-clock s)
llm_retry_max_attempts = 4           # env REBAR_LLM_RETRY_MAX_ATTEMPTS; transport retries per call (<=1 disables → fail-fast)
llm_retry_max_wait_s   = 60          # env REBAR_LLM_RETRY_MAX_WAIT_S; caps the Retry-After / backoff wait
mcp_servers    = {}                  # env REBAR_LLM_MCP_SERVERS (JSON); a TOML inline table in-file
```

Transient-failure retry (`llm_retry_*`) is owned at the httpx transport layer for Anthropic
calls (the SDK's own retries are disabled, `max_retries=0`): a `{429,529,5xx}` / timeout /
network blip is re-sent below the agent loop, so completed tool calls are never re-executed.
`Retry-After` is honored (capped at `llm_retry_max_wait_s`), else exponential backoff. Set
`llm_retry_max_attempts = 1` to disable retry (fail-fast back-out, no code revert). See
[ADR 0037](adr/0037-transport-retry.md).

Env-only (NOT `[tool.rebar.llm]` keys): the secret `REBAR_LLM_API_KEY`; the
runtime-only `REBAR_LLM_REPO_PATH` (which repo the review agent's read-only file
tools see — an invocation-specific override, default the repo root); and the
DERIVED runner — the in-process, provider-agnostic `pydantic_ai` runtime (`fake` is
a library-arg-only test seam).

> **`REBAR_LLM_REPO_PATH` precedence vs. the gate code root.** When a code-reading gate
> runs in `attested` mode it sets a context-local snapshot read root (see `[snapshot]`
> below) that takes precedence over `REBAR_LLM_REPO_PATH`, so the gate reads the pinned
> snapshot, not whatever `REBAR_LLM_REPO_PATH`/the checkout points at. With no gate active,
> resolution is unchanged: `REBAR_LLM_REPO_PATH` env > the resolved repo root.

### Repo-snapshot gates (`[snapshot]`) — optional, env-first; see `repo-snapshot-gates.md`

The code-reading gates (`review_plan` / `verify_completion` / `review` / `review-code` /
`scan-spec`, and `run_workflow` agent steps) read a **pinned-SHA snapshot** of the repo
(attested), not the server's mutable checkout. `snapshot` is a *reserved* section (the core
loader recognises `[snapshot]`/`[tool.rebar.snapshot]` and never warns/rejects it) resolved
**env-first** by `rebar._snapshot`: `REBAR_GATE_*` env > the `[snapshot]` table > built-in
default. Full behavior, the HMAC trust model, and the EFS/NFS `flock` caveat are in
[repo-snapshot-gates.md](repo-snapshot-gates.md).

```toml
[snapshot]
ref                  = "origin/main"  # default ref to verify   (env REBAR_GATE_REF)
source               = "attested"     # attested | local        (env REBAR_GATE_SOURCE)
free_watermark_bytes = 2147483648     # reclaim when free disk < this (2 GiB; env REBAR_GATE_FREE_WATERMARK_BYTES)
grace_seconds        = 120            # never evict an entry used within this window (env REBAR_GATE_GRACE_SECONDS)
max_age_seconds      = 604800         # cold-trim entries older than this (7 days; env REBAR_GATE_MAX_AGE_SECONDS)
reverify_seconds     = 0              # periodic integrity reverify period; 0 = off (env REBAR_GATE_REVERIFY_SECONDS)
interval_seconds     = 300            # janitor background pass cadence (env REBAR_GATE_JANITOR_INTERVAL_SECONDS)
```

Env-only (NOT `[snapshot]` keys): `REBAR_GATE_TMPDIR` (the snapshot store's base directory;
default the system temp dir — never a hardcoded `/tmp`) and `REBAR_GATE_ALLOW_UNGATED`
(audited escape hatch for the agentic-op safeguard). CLI surfaces: `--ref` / `--source` on
each of the five code-reading commands (one-to-one with the MCP tools' `ref`/`source` args).

### Secrets — environment / `.env` only (never the config file)

`REBAR_SIGNING_KEY`, `REBAR_LLM_API_KEY`, `JIRA_API_TOKEN`,
and the SDK-standard `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`LANGFUSE_SECRET_KEY`. (`LANGFUSE_*` is read by its own SDK and keeps its
standard names.)

### Removed / derived (not config keys)

- **Removed:** `paths.*`, `planning.external_dependency_block_enabled` (deleted),
  and the legacy `rebar_id_guard_mode` flat key (→ `reconciler.id_guard_bypass_unsafe`).
- **Derived, not configured:** the LLM runner (the in-process, provider-agnostic
  `pydantic_ai` runtime; `fake` is a test-only injection).
- **Runtime-only (env, not a config key):** `REBAR_LLM_REPO_PATH` — which repo the
  review agent's read-only file tools see (default: the repo root). It is an
  invocation-specific runtime override, so it stays an env var and is **not** a
  persistent `[tool.rebar]` setting.

## `ticket.default_assignee` — applied at CLAIM, not at create

`ticket.default_assignee` (default `""`) is the assignee `rebar claim` uses **when no
`--assignee` is given**. It is applied at **claim time, not when a ticket is created** —
a freshly `create`d ticket stays unassigned; the default lands only when the ticket is
claimed (and is written into the claim's `EDIT` event, exactly as an explicit
`--assignee` would be). Semantics:

- **Omitted** `--assignee` (CLI) / `assignee=None` (library) → the configured default is used.
- **Explicit** `--assignee X` always wins; an explicit `--assignee ""` **clears** the
  assignee and does **not** fall back to the default.
- The fallback is resolved before the parent-first cascade, so claiming a child also
  applies the default to any open parent it cascades into.

Set it in `[ticket]`/`[tool.rebar.ticket]` or via the env var `REBAR_DEFAULT_ASSIGNEE`
(env > file). Use a **Jira-resolvable identity** (email or accountId) — the value is a
local string that the reconciler resolves to a Jira user at sync time, so a bare,
ambiguous handle (e.g. `joe`) is left unassigned rather than mis-assigned (bug 544e).
It is distinct from `jira.user` (the reconciler's API auth user), though the two are
commonly the same person.

## Back-compat

`ticket_clarity.threshold` (clarity-check pass threshold, default 5) is a typed key —
settable in `[tool.rebar.ticket_clarity]`/`rebar.toml`, via `REBAR_TICKET_CLARITY_THRESHOLD`;
it appears in `rebar config`.
Unknown keys **warn** (not fail) during the deprecation window and hard-error under
`REBAR_CONFIG_UNKNOWN_KEYS=error`. The PERMANENT ergonomic env renames keep their old
names as aliases (with a warning): `REBAR_NO_SYNC`→`REBAR_SYNC_PULL`
(negative→positive flip), `COMPACT_THRESHOLD`→`REBAR_COMPACT_THRESHOLD`,
`SCRATCH_BASE_DIR`→`REBAR_SCRATCH_BASE_DIR`, `REBAR_ACLI_TIMEOUT`→`REBAR_JIRA_CLI_TIMEOUT`,
`RECONCILER_ABSENT_GET_BUDGET`→`REBAR_RECONCILER_DELETION_PROBE_LIMIT`,
`REBAR_ID_GUARD_MODE`→
`REBAR_UNSAFE_ID_GUARD_BYPASS` (raise→false/warn→true). **Removed pre-1.0 (DE7 — no
longer honored):** the flat `.rebar/config.conf` reader (use `rebar.toml` / a
`[tool.rebar]` pyproject table), the `verify.require_verdict_for_close` config alias
(use `verify.require_signature_for_close`), and the env aliases `REBAR_PUSH` (use
`REBAR_SYNC_PUSH`), `TICKETS_TRACKER_DIR` (use `REBAR_TRACKER_DIR`), and
`REBAR_MCP_ALLOW_RECONCILE_LIVE` (use `REBAR_MCP_ALLOW_JIRA_SYNC`). **Also removed pre-1.0
(ticket unclear-verymad-sablefish):** the env alias `REBAR_LLM_MAX_ITERS` (use
`REBAR_LLM_MAX_STEPS`) and the `reconciler.lock_backend` config key (the ref lock is the
only backend). Also removed (no alias):
`PROJECT_ROOT` (use `REBAR_ROOT`), `REBAR_LLM_RUNNER` (runner is derived), and the
dead `TICKET_CMD`/`REBAR_TICKET_CLI`/`TICKET_WORDLIST_PATH`/`TICKET_SYNC_CMD`/
`_REBAR_GC_AUTO_ZERO`/`REBAR_FSCK_NO_MUTATE` internals. See the env-var
standardization story `60ce`.

**Session provenance (one shared resolver).** rebar records "which coding-agent
session emitted an event" via ONE shared resolver
(`rebar._commands.session_id.resolve_session_id`, epic crust-fetch-stump). Its ordered,
data-driven var list resolves with precedence (first NON-EMPTY wins):
`REBAR_SESSION_ID` → `CLAUDE_CODE_SESSION_ID` → `OPENCODE_SESSION_ID` → `SESSION_ID` → `None`.
`REBAR_SESSION_ID` is the explicit, rebar-owned override (authoritative — e.g. hook-injected);
the native harness vars follow in popularity order (`CLAUDE_CODE_SESSION_ID`, then the OSS
`OPENCODE_SESSION_ID` shipped by OpenCode); the ambient, externally-set (e.g. CI/agent)
`SESSION_ID` is last. **Codex is not listed** — it exposes no supported readable session var,
so it is covered by its SessionStart shim exporting `REBAR_SESSION_ID` instead. An empty /
whitespace-only value is treated as **absent** (skipped). The resolver **never returns git
HEAD** (a HEAD changes on every commit within one session, so it is not a session id). A
wrongly-named / absent var simply falls through — never an error.

**Harness provenance (`AI_AGENT`) + remote session (`CLAUDE_CODE_REMOTE_SESSION_ID`).** A
claim also records, when present, an opaque harness-provenance tag from the rebar-owned
`AI_AGENT` var — the harness base name `claude-code` / `opencode` / `codex` / `cursor`,
OPTIONALLY suffixed with `_<version>` (e.g. `claude-code_1.2.3`) when the shim can discover one,
populated by the per-harness shims → `state["claim_harness"]`; and the secondary
`CLAUDE_CODE_REMOTE_SESSION_ID`
→ `state["claim_remote_session"]`. Both are opaque, read verbatim, local-only (never synced to
Jira).

This unifies two formerly divergent chains and is **additive "support both"**, *not* a
deprecating rename: ambient `SESSION_ID` remains permanently valid, so there is **no
deprecation warning** — unlike the renamed keys above. **Precedence-inversion note:** the
`session_log` current-log fingerprint formerly put `CLAUDE_CODE_SESSION_ID` before
`REBAR_SESSION_ID`; the unified contract puts `REBAR_SESSION_ID` first, an intentional
change that only differs when BOTH are set. The FORCE_CLOSE audit comment consumes the
shared resolver, then applies a LOCAL cosmetic fallback to short git HEAD then `"unknown"`
so the comment is always a non-empty string (that fallback is the call site's, not the
resolver's). Decided on tickets `83f2` / `6014`; see `60ce`.

## Transparency

`rebar config` (a.k.a. `--show-config`) prints the resolved values and **which
layer** each came from (the ruff `--show-settings` / pip `config debug` pattern).
