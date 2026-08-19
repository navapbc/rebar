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

- **Core stays minimal** — only three runtime dependencies: `pyyaml` (the workflow
  DSL loader), `jsonschema` (the schema-registry + contract validator), and
  `referencing` (its `$ref` resolver) (`pyproject.toml`
  `dependencies = ["pyyaml>=6", "jsonschema>=4.18", "referencing>=0.30"]`); the engine
  core and reconciler are otherwise stdlib-only, and all LLM/MCP/eval/tracing functionality
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
verify.verify_window_headroom      = 0.8     # plan-review Pass-2 verify: fraction of the verifier
                                             # model window a single verify request may use before
                                             # the findings are split into multiple calls (0.1–1.0)
verify.max_ticket_description_chars = 8000   # blocking plan-review and completion admission limit;
                                             # 8,000 is allowed, 8,001 blocks. Positive integer. Also
                                             # drives the create/edit save-time warning.
# Progressive drift-refresh of drifted findings during plan review is now
# always-on (unconditional; no config toggle).
verify.require_completion_verification_for_close = false  # gate work-ticket close on a PASS completion
                                             # verdict (signed onto the ticket); fail-closed. ON for
                                             # this project's rebar.toml.
verify.auto_resume_max             = 2       # bounded auto-resume for the completion close gate
                                             # (ticket b5f8): when a close FAILs on pure evidence-
                                             # search exhaustion (every unmet criterion carries the
                                             # framework-set `evidence_sufficient: false` marker —
                                             # nothing positively refuted), the gate re-runs
                                             # `verify_completion` itself; the cross-run verdict
                                             # cache seeds prior validated PASSes so each re-run
                                             # spends its budget on the formerly-insufficient
                                             # criteria. At most this many resumptions per close
                                             # invocation, and only while the prior attempt strictly
                                             # increased the cache-credited PASS count (zero progress
                                             # stops early). Integer >= 0; 0 disables auto-resume.
                                             # Per-attempt trail (attempt number, cache-credited
                                             # count, remaining unmet) rides the close output and the
                                             # durable COMPLETION_VERDICT sidecar record.
verify.require_plan_review_for_claim = false # gate claim on a successful (non-BLOCK) plan review attestation
verify.suggest_duplicate_tickets   = false   # ADVISORY store-wide duplicate detection during
                                             # `review-plan`: emits up to three suggested
                                             # `duplicates`/`supersedes`/`depends_on` links in a
                                             # separate `overlap[]` verdict key (ready-to-run
                                             # `rebar link` commands). Three stages — the query
                                             # ticket's digest via `enrich`, BM25F retrieval over
                                             # the WHOLE tracker with the query's own graph
                                             # excluded, then a bounded pairwise LLM judge. It is
                                             # advisory and NEVER blocks a claim or a close, and
                                             # never affects the plan-review verdict. Cost scales
                                             # with TRACKER SIZE, not with the reviewed ticket, so
                                             # it grows as the store grows. Algorithm tunables are
                                             # the `[tool.rebar.llm] overlap_*` keys (`overlap_k`,
                                             # `overlap_drain`, `overlap_conf_threshold`, …). env
                                             # REBAR_VERIFY_SUGGEST_DUPLICATE_TICKETS; the former
                                             # name `verify.overlap_enabled` is a permanent alias.
                                             # See docs/adr/0037-cross-ticket-overlap.md.
verify.require_ticket_for_commit   = false   # CI Verified gate: every commit to main must reference a rebar
                                             # ticket that RESOLVES in the store (rebar-ticket: <id> trailer or a
                                             # leading <id>:; alias/full/short/Jira). env
                                             # REBAR_VERIFY_REQUIRE_TICKET_FOR_COMMIT. See docs/commit-ticket-trailer.md
# Convergent plan-edit re-review (the rising floor: a re-review of an edited plan
# whose CODE is unchanged drops only NOVEL low-priority findings) is now always-on
# and unconditional; the evidence gate is always-on too. These tuning params remain:
verify.remediation_window_minutes  = 60      # remediation freshness window: a re-review is eligible
                                             # only if the last review of any kind was within this many
                                             # minutes (measured from it, reset on each review). Min 1.
verify.novelty_drop_threshold      = 0.7     # T_novel: a finding is droppable only if its novelty >= this
verify.novelty_priority_floor      = 0.4     # rising floor: drop a novel finding only if priority < this
                                             # (scalar ≈ corpus p40 impact; see the distribution script)

# tickets / display / maintenance
ticket.display_mode      = "auto"
ticket.default_assignee  = ""     # assignee `claim` uses when --assignee is omitted (env REBAR_DEFAULT_ASSIGNEE)
ticket_clarity.threshold = 5      # clarity-check pass threshold (env REBAR_TICKET_CLARITY_THRESHOLD)
compact.threshold        = 10     # env REBAR_COMPACT_THRESHOLD (alias: COMPACT_THRESHOLD)
compact.trigger          = "async"  # async | always | off (env REBAR_COMPACT_TRIGGER)
                        # The OPERATION-LINKED compaction trigger. Compaction does not run on
                        # the close path any more (it held the store write lock for minutes),
                        # and the scheduled sweep that replaced it needs CI or cron — which a
                        # library/CLI adopter does not have. So a close TRIGGERS compaction
                        # without performing it: after the store lock is released it makes two
                        # O(1) checks and, if either fires, detaches a worker that runs
                        # `compact-all` out of band. "always" runs that sweep INLINE instead
                        # (tests/CI, where a detached child would outlive the assertion);
                        # "off" disables it for operators who drive compaction themselves.
                        # A v1 no-op on Windows, mirroring the enrichment drain.
compact.trigger_interval_s = 21600  # env REBAR_COMPACT_TRIGGER_INTERVAL_S
                        # How stale the last-sweep stamp may get before a close detaches a
                        # worker even when the ticket it just wrote needs no folding. Without
                        # this the floor has a hole: a store whose closed tickets happen never
                        # to be foldable would never fold the ones that are. Default 6 h,
                        # matching the scheduled sweep's cadence. 0 disables the staleness arm.

# sync (git-backed store)
sync.push   = "always"  # always | async | off   (env REBAR_SYNC_PUSH)
sync.pull   = "on"      # on | off               (env REBAR_SYNC_PULL; alias REBAR_NO_SYNC)
sync.remote = "origin"  # git remote the tickets branch syncs to — push/fetch/reconcile, the
                        # fsck PUSH_PENDING check, and attested ticket-store materialization
                        # (env REBAR_SYNC_REMOTE). Set it for split residency: e.g. the tickets
                        # branch's source of truth on origin=GitHub while code review lives on a
                        # separate `gerrit` remote. Validated as a git remote name (rejects
                        # spaces / `:` / `~` / `/` / control; dots + non-leading hyphens allowed).
                        # The named remote's URL may be an `s3://bucket/prefix` remote served by
                        # the OPTIONAL `git-remote-s3` helper (`pip install 'nava-rebar[s3]'`),
                        # keeping ticket history entirely out of any git host — see
                        # `s3-backend.md`. This is opt-in; core rebar never requires boto3/AWS.
                        # Every write pushes to this remote, so make sure the tickets branch
                        # triggers NO CI workflow there — see `concurrency.md`, "Outbound —
                        # push (on every write)".

# MCP server gates
mcp.readonly         = false
mcp.allow_llm        = false
mcp.allow_jira_sync  = false   # live (applying) Jira writes (env REBAR_MCP_ALLOW_JIRA_SYNC)

# audit web UI (optional, read-only)
ui.enabled = false   # gates `rebar audit serve` — the disabled-by-default, loopback-bound
                     # read-only audit web UI (env REBAR_UI_ENABLED). When false, no web
                     # dependency is imported and `serve` refuses to start; enabling it also
                     # requires the `nava-rebar[ui]` extra.

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

The default description limit is calibrated just above the historical p99 (7,500 characters):
8,000 affected 18 of 2,202 work tickets (0.82%) while capturing the tail with materially slower
plan reviews and less reliable completion verification. This is admission control, not content
transformation: rebar does not truncate, summarize, or elide the ticket to fit. Reduce the
description, usually by moving independent work into child tickets. A human operator can still use
the existing lifecycle `--force=<reason>` escape hatch to claim or close without the corresponding
attestation; force does not make the oversized description pass either review gate.

**Save-time warning.** The same key drives an early heads-up so the limit is not discovered only
at `review-plan` time: when a `create` or `edit` writes a description longer than
`verify.max_ticket_description_chars` **and** the plan-review start-work gate applies to that
ticket (`verify.require_plan_review_for_claim` is on and the type is not gate-exempt), rebar warns
that claiming the ticket will need a passing review which refuses admission until the description
shrinks. It is a **warning, never a rejection** — the write has already committed and is unaltered,
and nothing is emitted when the gate is off or the description is within the cap. Each surface uses
its own channel: the **CLI** prints it to stderr (stdout stays pure in text and json modes, exit
code unchanged), the **library** logs it on the `rebar` logger (and `edit_ticket` additionally
returns it; `create_ticket(return_alias=True)` carries it as `description_warning`), and **MCP**
returns it as the `description_warning` result field, mirroring `push_status`.

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

### Code-health analyzers (`code_health.*`)

Code-health analysis has no on/off switch — `rebar metrics` always evaluates its structural
metrics. These keys only *tune* that analysis. Configure it in standalone `rebar.toml` with a
`[code_health]` table, or in `pyproject.toml` with `[tool.rebar.code_health]`; the keys and
values are otherwise identical:

```toml
# rebar.toml
[code_health]
scan_roots = ["src", "web"]           # list[str]; default []
include_extensions = ["py"]            # list[str]; default [] (every type scc recognises)
size_cap = 800                          # optional int; default unset/None
size_near_fraction = 0.15              # float; default 0.1
```

```toml
# pyproject.toml
[tool.rebar.code_health]
scan_roots = ["src", "web"]
include_extensions = ["py"]
size_cap = 800
size_near_fraction = 0.15
```

`scan_roots` is `list[str]` and defaults to `[]`; `include_extensions` is `list[str]` and
defaults to `[]`, meaning **every file type scc recognises** — the analyzer is polyglot and
ships no hardcoded language filter; `size_cap` is an optional `int` and defaults to
unset/`None`; and `size_near_fraction` is a `float` and defaults to `0.1`. TOML has no null
value, so omit `size_cap` unless setting an integer. Setting `include_extensions` (bare
extensions, no leading dot — e.g. `["py"]`, `["go", "ts"]`) narrows the module-size metric to
the file types a project's own size policy governs, so the metric describes the same files the
project's size gate enforces rather than every file in the scan roots. There is no per-language
analyzer *selection* key: `git_metrics` composes the scc, lizard, and jscpd adapters directly.
Install the required tools separately as described in
[user-guide.md](user-guide.md) (§ Metrics).

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
rich_text_cutover      = "off" # rich-text wire per client: off|cloud|dc|both. Ships OFF; set back to "off" to roll back. env REBAR_RECONCILER_RICH_TEXT_CUTOVER
dc_pandoc_timeout_s    = 10.0  # wall-clock ceiling (s) on ONE pandoc call in the DC wiki renderer; on expiry that unit falls back to raw Markdown. env REBAR_RECONCILER_DC_PANDOC_TIMEOUT_S
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
url            = ""      # env JIRA_URL  (must be https unless allow_insecure=true)
user           = ""      # env JIRA_USER
project        = ""      # env JIRA_PROJECT  (the reconciler substitutes "DIG" when empty on CREATE)
allow_insecure = false   # env REBAR_JIRA_ALLOW_INSECURE — allow a cleartext http:// url
```

A non-https `jira.url` is REJECTED at config load (an `InsecureUrlError`, naming the
cleartext-credential risk) unless `allow_insecure = true`, which downgrades it to a
warning — the parity of `reconciler.allow_insecure` for the Cloud transport. It governs
the URL **scheme only** and never relaxes certificate verification. Intended for a
loopback/trusted test instance; the `rebar bridge setup` wizard is https-only and will
not persist a cleartext url.

The SECRET `JIRA_API_TOKEN` stays env-only — never a config key (see Secrets).

**`jira.project` is the seed default, not the only project a store syncs.** A store
records its full set of tracker projects in a committed mapping —
`.bridge_state/projects.json` on the tickets branch — that is a property of the **store**,
not of any one repo (see [ADR 0097](adr/0097-many-to-many-tracker-projects.md)). Manage it
with `rebar bridge projects {list,set,remove}` (see the [user guide](user-guide.md#jira)).
The mapping's shape is `{"version": 1, "legacy_default": "REB", "projects": {"REB":
{"repos": ["rebar"]}}}`; its `projects` key set IS the sync list, and `legacy_default` is
the project that pre-mapping tickets (those with no `bridge_project` field) resolve to. On a
store initialized before the mapping existed, the `projects-seed` ensure unit seeds a mapping
whose `legacy_default` is `jira.project` (or `DIG` when unset) and whose `projects` set is
**empty** at the next `init`/`fsck --repair` (see [migrations.md](migrations.md)); the sync
list stays empty until an operator adds a project with `rebar bridge projects set`.
`jira.project` therefore configures the legacy default, while the projects a store actively
syncs are added through the mapping rather than through config.
Each project's `repos` entry records which repositories that project's tickets belong to.

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
model          = "claude-opus-4-8"   # top-level model (config/CLI only — the bare
                                     # REBAR_LLM_MODEL env was removed); prefer the
                                     # [tool.rebar.llm.model_classes] per-class slots
model_provider = ""                  # env REBAR_LLM_MODEL_PROVIDER (inferred from the model name when empty)
base_url       = ""                  # env REBAR_LLM_BASE_URL (OpenAI-compatible endpoint)
parse_failure_artifact_dir = ""      # env REBAR_LLM_PARSE_FAILURE_ARTIFACT_DIR (opt-in raw-reply capture; unset = off)
max_tokens     = 16000               # env REBAR_LLM_MAX_TOKENS
max_steps      = 50                  # env REBAR_LLM_MAX_STEPS; ~2 steps per tool call
timeout        = 600                 # env REBAR_LLM_TIMEOUT (wall-clock s)
llm_retry_max_attempts = 4           # env REBAR_LLM_RETRY_MAX_ATTEMPTS; transport retries per call (<=1 disables → fail-fast)
llm_retry_max_wait_s   = 60          # env REBAR_LLM_RETRY_MAX_WAIT_S; caps the Retry-After / backoff wait
llm_tool_timeout_s     = 120         # env REBAR_LLM_TOOL_TIMEOUT_S; per-tool timeout (bounds async/MCP tools)
mcp_servers    = {}                  # env REBAR_LLM_MCP_SERVERS (JSON); a TOML inline table in-file
```

Transient-failure retry (`llm_retry_*`) is owned at the httpx transport layer for Anthropic
calls (the SDK's own retries are disabled, `max_retries=0`): a `{429,529,5xx}` / timeout /
network blip is re-sent below the agent loop, so completed tool calls are never re-executed.
`Retry-After` is honored (capped at `llm_retry_max_wait_s`), else exponential backoff. Set
`llm_retry_max_attempts = 1` to disable retry (fail-fast back-out, no code revert). See
[ADR 0037](adr/0087-transport-retry.md). On the Bedrock path the same knob is wired into the
botocore client Config as `retries={"max_attempts": N, "mode": "adaptive"}` (total attempts,
matching tenacity's counting) and `timeout` becomes the client's read + connect timeout;
`llm_retry_max_wait_s` has no botocore equivalent and applies to the Anthropic path only.

Liveness is activity-based, not a total-runtime cap: the per-request read timeout (reuses
`timeout` above) bounds a hung model, and `llm_tool_timeout_s` bounds a hung ASYNC/MCP tool
(a no-op for sync in-process tools, which are bounded by the derived step caps). No hard
total-runtime timeout truncates a healthy long run. The async stream-event idle-watchdog is
deferred pending an async-runner migration (see the liveness ADR).

**Derived step caps.** The per-run step budget is DERIVED from `max_steps` (env
`REBAR_LLM_MAX_STEPS`), not a hardcoded 50: `request_limit = max(1, ceil(min_steps/2))`
and `tool_calls_limit = max(8, min_steps)`. The gate VERIFIER ops apply a review floor
(`min_steps = 120`), so their concrete defaults are **`request_limit = 60`,
`tool_calls_limit = 120`** — tune via `REBAR_LLM_MAX_STEPS`.

**`llm.parse_failure_artifact_dir`** (env `REBAR_LLM_PARSE_FAILURE_ARTIFACT_DIR`) — opt-in,
LOCAL capture of the raw model reply when the prompted structured-output reask loop exhausts
its bounded retries and is about to raise. **Default: unset (`None`) = OFF** — nothing is
written and the failure path is byte-for-byte unchanged. When set to a directory, the FINAL
parse failure writes ONE JSON artifact there (the raw reply plus `model`, `contract`,
`attempts`, and `timestamp` metadata) for replay/debugging, and names the artifact path in the
raised error. The write is **best-effort** (any I/O error is swallowed and never masks the
original parse error) and **self-limiting** — the directory is rotated to keep the newest 20
files, evicting the oldest.

### LLM failure taxonomy (the resolution-disposition vocabulary)

When an LLM gate call fails, rebar classifies it into a **closed 8-class disposition**
(`rebar.llm.failure.ResolutionClass`) that says *what a human/agent should do next*. The
disposition is persisted on a degraded verdict as `coverage.resolution_class` (+ a
`retryable` bool + a sanitized `diagnostic`) and drives the CLI exit code — the two
**retryable** classes exit **11** ("transient — retry", see
[exit-codes.md](exit-codes.md) + [ADR 0040](adr/0040-exit-11-block-but-retryable.md)); the
rest map to the gate's existing INDETERMINATE exit. The diagnostic is redaction-sanitized
before it is ever persisted ([ADR 0041](adr/0041-llm-diagnostic-sanitization.md)).

| Class | Retryable | Meaning / typical trigger |
|---|:--:|---|
| `WAIT_AND_RETRY` | ✅ | Provider overload / rate-limit (429/529) — wait for the backoff window, then retry. |
| `RETRY_NOW` | ✅ | Transient connection blip (network error) — retry immediately. |
| `INCREASE_PROVIDER_LIMITS` | | Provider usage/quota ceiling hit — raise the limit or wait for the reset. |
| `CHANGE_SETTINGS` | | A configured bound was exceeded (tokens / steps / timeout) — adjust it and retry. |
| `CHANGE_INPUT` | | Input rejected (too large / context-length / malformed) — reduce or fix it. |
| `CHANGE_PROVIDER_OR_MODEL` | | Model/provider unavailable, refused, or content-filtered — switch model/provider. |
| `FIX_AGENT_DESIGN` | | Agent-construction bug (no tools / bad output contract) — fix the op wiring. |
| `NEEDS_INVESTIGATION` | | Unclassified failure — inspect the sanitized diagnostic. |

The classifier (`classify_llm_failure`) is pure, total, and never raises: an unmatched
failure maps to `NEEDS_INVESTIGATION`. It reads an exception from any runner failure seam
(or a `finish_reason` carried on the context) and unwraps a `tenacity.RetryError` to the
underlying cause, so an exhausted-retry timeout still classifies `WAIT_AND_RETRY`.

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
free_watermark_bytes = 2147483648     # reclaim when free disk < this many bytes (2 GiB; env REBAR_GATE_FREE_WATERMARK_BYTES)
free_watermark_pct   = 0              # ALSO reclaim when free disk < this % of the volume; 0 = off (env REBAR_GATE_FREE_WATERMARK_PCT)
grace_seconds        = 120            # never evict an entry used within this window (env REBAR_GATE_GRACE_SECONDS)
max_age_seconds      = 604800         # cold-trim entries older than this (7 days; env REBAR_GATE_MAX_AGE_SECONDS)
reverify_seconds     = 0              # periodic integrity reverify period; 0 = off (env REBAR_GATE_REVERIFY_SECONDS)
interval_seconds     = 300            # janitor background pass cadence (env REBAR_GATE_JANITOR_INTERVAL_SECONDS)
```

The two watermark terms are combined by taking the **larger**, so `free_watermark_pct` adds
volume-awareness on top of the absolute floor (which is disk-size-blind: 2 GiB free is 93% used
on a 30 GiB root volume). It is whole percentage points as an **integer**, not a fraction. Once a
reclamation pass starts it runs on to a target 5 points higher — a fixed internal margin
(`RECLAIM_TARGET_MARGIN_PCT`), not a config key or env var. See
[repo-snapshot-gates.md](repo-snapshot-gates.md) for sizing guidance.

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

### `REBAR_OPERATION_SNAPSHOT_SHADOW` — temporary shadow-mode switch (RP-04, removed in S7)

A **temporary** operational env var (not a `[tool.rebar]` config key). While the RP-04
per-operation configuration authority (`rebar.config.OperationSnapshot`, see
[reuse-surface.md](reuse-surface.md#6-the-operation-snapshot--rebarconfigoperationsnapshot-rp-04))
is wired in **shadow mode** — composed once per operation and logged as a REDACTED
diagnostic, but NOT controlling execution — this switch gates that shadow construction.

- **Default: enabled.** Unset ⇒ shadow snapshots are composed (the default). The
  canonical true spellings (`true`/`1`/`yes`/`on`) also enable it.
- **Rollback:** set it to a canonical false spelling (`false`/`0`/`no`/`off`/empty) to
  disable shadow construction entirely — the safe kill-switch if the diagnostic ever
  misbehaves. Because shadow mode is diagnostic-only and self-guarded (any
  non-`ConfigError` failure is caught and the legacy operation continues untouched),
  disabling it changes nothing an operation depends on.
- **Lifecycle:** this switch and all shadow wiring are **removed in S7**, once a real
  behaviour-bearing consumer is cut over to the snapshot. Do not build durable config
  on it.

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
`REBAR_UNSAFE_ID_GUARD_BYPASS` (raise→false/warn→true),
`REBAR_VERIFY_OVERLAP_ENABLED`→`REBAR_VERIFY_SUGGEST_DUPLICATE_TICKETS`. The same rename
is a permanent **config-key** alias too: `verify.overlap_enabled`→
`verify.suggest_duplicate_tickets` (when both are set the canonical key wins). Also removed (no alias):
`PROJECT_ROOT` (use `REBAR_ROOT`), `REBAR_LLM_RUNNER` (runner is derived), and the
dead `TICKET_CMD`/`REBAR_TICKET_CLI`/`TICKET_WORDLIST_PATH`/`TICKET_SYNC_CMD`/
`_REBAR_GC_AUTO_ZERO`/`REBAR_FSCK_NO_MUTATE` internals. See the env-var
standardization story `60ce`.

### Fail-loud tombstone registry for removed inputs (story 36c7)

A **removed** env var / TOML key / legacy file is not the same as an unknown key. Unknown
keys keep the forward-compat policy above (warn, or error only under
`REBAR_CONFIG_UNKNOWN_KEYS=error`). But a *retired-but-still-set* input that used to affect
**store location, write/sync gates, auth, security, or lifecycle policy** must not be
silently ignored — silently dropping it reverts the operator's intent to a default, which is
unsafe. rebar keeps a **tombstone registry** (`rebar._deprecations._TOMBSTONE_REGISTRY`,
distinct from the alias registry) that classifies each removed input:

- **`error` (load-bearing) → FAIL LOUD.** rebar raises a targeted migration error naming
  the old name + replacement + removed-in and exits **non-zero** — never a raw traceback.
  Implemented as `RemovedInputError`, which subclasses `BaseException` (not `Exception`)
  deliberately, so no `except ConfigError` / `except Exception` fallback in the
  config→tracker→MCP path can swallow it into a silent default. Error-class tombstones:
  `TICKETS_TRACKER_DIR` (use `REBAR_TRACKER_DIR`), `REBAR_MCP_ALLOW_RECONCILE_LIVE`
  (use `REBAR_MCP_ALLOW_JIRA_SYNC`), `REBAR_LLM_MAX_ITERS` (use `REBAR_LLM_MAX_STEPS`),
  the config key `verify.require_verdict_for_close`
  (use `verify.require_completion_verification_for_close`), and the flat
  `.rebar/config.conf` reader (use `rebar.toml` / a `[tool.rebar]` pyproject table).
- **`warn` (operationally inert) → WARN and continue (exit 0).** Renamed/dropped tunables
  with no behavioural bite: `REBAR_PUSH` (use `REBAR_SYNC_PUSH`),
  `REBAR_RECONCILER_LOCK_MAX_RETRIES` / `REBAR_RECONCILER_LOCK_RETRY_BUDGET` (dropped),
  `REBAR_CODE_HEALTH_ANALYZERS` / `REBAR_CODE_HEALTH_ENABLED` (dropped), and the config keys
  `reconciler.lock_backend` / `reconciler.lock_max_retries` (the ref lock is the only
  backend), `code_health.analyzers` (per-language analyzer selection was never wired) and
  `code_health.enabled` (it was never read, so it never switched anything off).

**`rebar config validate`** is a non-raising sweep that reports **every** tombstoned input
(and its replacement + removed-in) currently set in the environment / parsed config / as the
legacy file, plus every invalid known core typed value across user, project, environment, and
CLI layers. It applies the same coercion rules as the live loader but aggregates failures
instead of aborting on the first one. Unknown keys retain their forward-compatibility policy,
and optional-layer sections such as `llm` and `snapshot` remain owned by those layers. The
command exits **non-zero iff an error-class tombstone or invalid typed value is present** (a
clean environment exits 0). It does not mount or initialize the ticket store, because store
discovery itself may depend on the invalid input being audited. Use it to audit a config
without aborting on the first problem:

```console
$ rebar config validate
rebar config validate: OK — no removed or invalid typed inputs are set.
```

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
