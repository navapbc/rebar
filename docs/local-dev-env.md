# Local dev environment — running the **repo** version of rebar

When you work *on* rebar (editing `src/rebar`, running the plan-review/LLM gates, or
testing config behavior), you must run the **repo checkout's** rebar, not a globally
installed one. A global build is a *frozen snapshot* and will silently diverge from the
working tree in two ways that matter:

- **Config keys it predates are ignored.** rebar warns `unknown key '<k>' ignored
  (typo?)` and drops the key. A build older than, say, `verify.require_plan_review_for_claim`
  will **silently not enforce the plan-review claim gate** even though `rebar.toml` enables
  it — you get green claims with no review.
- **Optional extras may be missing.** Without the `[agents]` extra the LLM tiers can't
  run, so `review-plan` / `verify-completion` degrade (see
  [plan-review-gate.md](plan-review-gate.md) and [llm-framework.md](llm-framework.md)).

## Prerequisite — Git ≥ 2.38

**rebar declares a hard Git floor of 2.38 for development and CI.** The two-clone
convergence regressions (`tests/integration/test_concurrency_regression.py`) merge two
independently written tracker histories with `git merge-tree --write-tree`, a mode Git
gained in 2.38.

The floor is **enforced, not skipped**. On an older client `pytest` refuses to start and
prints the required version and the remedy — deliberately, because a regression that
quietly does not run reads as coverage while providing none. Check yours with
`git --version`; on macOS the Xcode command-line Git can lag, so prefer `brew install git`,
and on Debian/Ubuntu use the git-core PPA or a backports build.

The value is single-sourced in **`.github/git-version-floor.txt`** and read by three
consumers that must agree: `tests/conftest.py` (the suite preflight), the
`Git version floor gate` step in `.github/workflows/_build-and-test.yml`, and
`tests/unit/test_git_version_floor.py` (which fails if any consumer drifts, and fails
if a merge-tree regression ever acquires skip machinery).

## TL;DR (canonical setup)

```sh
# from the repo root
make install                                    # uv sync --locked (dev extra) + the pre-commit hook (the commit gate)
source .venv/bin/activate
export AWS_PROFILE=...                          # the LLM ops (review-plan, verify-completion) default to Bedrock
export AWS_DEFAULT_REGION=us-east-1             # a region must resolve; Bedrock has no default
```

> **The LLM gates default to AWS Bedrock on this project**, so they need AWS credentials and a
> region rather than `ANTHROPIC_API_KEY`. No Bedrock access? One-line opt-out:
> `export REBAR_LLM_CONFIG_FILE="$PWD/.github/llm-providers/anthropic.toml"` and export
> `ANTHROPIC_API_KEY` instead — see "Running your local gates on AWS Bedrock" below.

> **Starting a NEW worktree? One command does the whole setup.** `make worktree name=<branch>`
> creates a fresh worktree at `../<branch>` (override with `dir=<path>`) branched from a
> freshly-fetched `origin/main`, then provisions its `.venv` and runs the canonical
> `make install` above inside it — the one-command form of the manual "fresh worktree + local
> venv" sequence this repo mandates. Then `cd ../<branch> && source .venv/bin/activate` and
> export your provider credentials (AWS by default — see the note above).

> **Signing your ticket writes (per-clone identity).** Every clone that writes non-exempt
> tickets should own its **own** identity + SSH signing key (never the shared bot). One-time
> setup — create/own an identity ticket, set the current-identity pointer, and point
> `identity.signing_key` (or `REBAR_IDENTITY_SIGNING_KEY`) at your **per-machine, uncommitted**
> SSH private key — is documented in [`identity.md`](identity.md) under "Setting up signing in
> a local dev / agent clone". The key never leaves your machine; only your public key lives in
> the store.

**Use `make install` — it is the one canonical setup path.** It runs `uv sync --locked
--extra dev --extra metrics`, installing the repo's env **through the committed `uv.lock`** (so every
checkout gets the same verified-importable dependency set — the unlocked path once resolved
an import-broken `pydantic-ai-slim`/`anthropic` pair; the dev extra pulls
`nava-rebar[agents,metrics]` plus the lint/type/test tooling), **and** wires the pre-commit hook
via `make hooks`, which is what makes lint/format run on every `git commit`. The Makefile
is the single source of truth for lint/format/type/test — see `make help`.

> **Why not just `uv sync` / `pip install -e '.[dev]'`?** A bare install does **not** wire the
> commit hook — `git` hooks are opt-in per clone and no `pip`/`uv` install step runs
> `pre-commit install`. Skip the hook and lint/format errors sail through `git commit` and
> are only caught later by CI (the slow gate). If you must run the install step by hand,
> follow it with the hook step and verify it:
>
> ```sh
> uv sync --locked --extra dev --extra metrics  # the canonical locked install (pip -e '.[dev,metrics]' is the unlocked fallback)
> make hooks                   # installs + VERIFIES the pre-commit hook; re-runnable anytime
> ```
>
> `make hooks` also handles the common `core.hooksPath` snag: `pre-commit install` fails
> with `Cowardly refusing to install hooks with core.hooksPath set` when that config is
> present. The target unsets it automatically when it is the redundant default
> (`.git/hooks`), and otherwise stops with the exact `git config --unset-all
> core.hooksPath` command (note: it may be set **globally** — unset at that scope).

### Verify the commit gate is active

```sh
test -f "$(git rev-parse --git-common-dir)/hooks/pre-commit" \
  && echo "commit gate: ON" || echo "commit gate: OFF — run 'make hooks'"
```

`make hooks` prints `✓ commit gate active: …` on success and exits non-zero (loudly) if the
hook did not land — so the gate is never silently absent.

### The commit gate needs the dev tools on `PATH` (activate the venv before committing)

The hooks run `make lint` (ruff) and `make typecheck` (mypy), which invoke the **bare**
`ruff` / `mypy` resolved from `PATH` — the same commands CI runs, so the hook, `make`, and CI
never drift. That means the shell you `git commit` from must have the project venv **active**
(or the `[dev]` tools otherwise on `PATH`). `make install` into an activated venv puts them
there; the canonical setup above (`source .venv/bin/activate`) satisfies this.

- **Symptom when the venv is NOT active:** a hook fails with `make: mypy: No such file or
  directory` (or `ruff`) — even though your code is clean. It is an environment problem, not a
  code problem. (A split env where only *one* tool leaked onto the global `PATH` makes this
  look especially confusing: `make lint` passes but `make typecheck` fails, or vice-versa.)
- **Fix:** `source .venv/bin/activate` before committing, or run the commit with the venv bin
  prepended: `PATH="$PWD/.venv/bin:$PATH" git commit …`.

This is a **developer-environment** note only — it concerns committing changes *to* rebar. It
has no bearing on installing or running rebar as a tool; end users never run the commit gate.

## Verify you're on the repo build

```sh
which rebar
# .../.venv/bin/rebar         -> repo (good)
# ~/.local/.../pipx/.../rebar -> GLOBAL build shadowing the repo; activate the venv

rebar show <any-ticket> 2>&1 | grep -i 'unknown key'
# no output  -> the build recognizes the current config schema (good)
# a warning  -> the build is older than that key and is NOT enforcing it
```

If a global `rebar` keeps winning on `PATH`, invoke the module form explicitly so the repo
package is used: `python -m rebar <args>` (with the venv active).

### Stale reducer cache in a mixed-build checkout

Each ticket dir caches its reduced state in a `.cache.json`, keyed by a content hash that
folds in a **reducer-cache version**. When a projection changes, that version is bumped so
older caches miss and are recompiled — but **only builds that carry the bump know the new
version**. In a *mixed-build* checkout (a repo `.venv` alongside a global `pipx` build, an
MCP server, or a git hook running a different build), an older build sharing the same
`.tickets-tracker` can write a cache under the *old* version that a newer build then serves,
so a ticket reads back **missing newer state** (e.g. a signed `plan-review`/`completion`
attestation reads as absent, wrongly blocking `claim`).

**Workaround:** run a **single build** against the store (activate the repo venv — see
"Verify you're on the repo build" above — and don't let a stale global build touch the same
`.tickets-tracker`). If a ticket already has a stale cache, **delete that ticket's
`.cache.json`** (`rm .tickets-tracker/<id>/.cache.json`) and re-read it — the next reduce
recompiles from the events. Keeping every build on the same version (upgrade the global
build, or use `python -m rebar` from the repo) prevents it recurring.

## What the LLM ops need

`review-plan`, `verify-completion`, and the other `rebar.llm` operations require, in
addition to the editable install:

- the **`[agents]`** extra — `pydantic-ai-slim[anthropic]`, `json-repair`, `pydantic`
  (included in `[dev]`);
- the core deps `pyyaml`, `jsonschema`, `referencing` (declared in `[project.dependencies]`,
  installed automatically by any `pip install -e .`);
- **credentials for the provider this project defaults to, which is AWS Bedrock, not direct
  Anthropic** — working AWS credentials on the ambient chain plus a resolvable region (the
  calls are live + billable). See the section below, including the one-line opt-out back to
  `ANTHROPIC_API_KEY` if you do not have Bedrock access.

When the gate is enabled but a dependency is missing, the review currently degrades to a
deterministic-floor-only result instead of failing loudly — a known defect (bug
`fuel-posse-ball`). Until it's fixed, treat any `review-plan` output with
`coverage.llm_ran == false` as **not a real review**, regardless of the `PASS` verdict.

### Running your local gates on AWS Bedrock (the project default) and how to opt out

**This project's local LLM gates default to AWS Bedrock, not direct Anthropic.** `rebar.toml`
— the project's authoritative config — carries an `[llm]` table naming Bedrock inference
profiles for all three model classes, so a clean checkout with no `REBAR_LLM_*` variable set
runs `review-plan` / `review-code` / `verify-completion` on Bedrock. Nothing to opt in to.

That is deliberate (bug `d2ce-36f5-fd08-4e40`, epic `061c-ecd1`): the path rebar is developed
against should be the path rebar's users run, and the local gates are the gates a developer and
every coding agent hit constantly. **The cost, accepted knowingly:** your local gates now need
**working AWS credentials** on the ambient chain (`AWS_PROFILE`, env keys, instance role) **and
a resolvable region**. `ANTHROPIC_API_KEY` is not consulted on this path.

**If you do not have Bedrock access (or are offline), take the opt-out — one variable.**
`REBAR_LLM_CONFIG_FILE` outranks the discovered project config, and the checkout ships the
overlay to point it at. These are the same committed overlays CI's provider matrix runs on
(`docs/ci-provider-matrix.md`), so the opt-out path is exercised, not hypothetical:

```sh
export REBAR_LLM_CONFIG_FILE="$PWD/.github/llm-providers/anthropic.toml"   # opt OUT to Anthropic
unset  REBAR_LLM_CONFIG_FILE                                              # back to the Bedrock default
```

With that exported you are back on direct Anthropic and need `ANTHROPIC_API_KEY` instead of AWS
credentials. `openai.toml` is available the same way; `bedrock.toml` is what the project default
mirrors, so pointing at it explicitly is a no-op you never need.

Or write your own overlay — anywhere readable; `~/.config/rebar/my-provider.toml` keeps it out
of the checkout, and the table is `[llm.model_classes]` (not `[tool.rebar.llm…]`, which is the
`pyproject.toml` spelling). Set `[llm] model` alongside it: `cfg.model` is a second resolution
path the class table cannot reach, and leaving it unset falls back to a bare literal that infers
provider `anthropic`, so an overlay that sets only the classes still leaks direct-Anthropic
calls from any op that resolves `cfg.model`.

```toml
# ~/.config/rebar/my-provider.toml
[llm]
model = "bedrock:us.anthropic.claude-opus-4-8"

[llm.model_classes]
frontier = { model = "bedrock:us.anthropic.claude-opus-4-8" }
standard = { model = "bedrock:us.anthropic.claude-sonnet-4-6" }
trivial  = { model = "bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0" }
```

Do **not** reach for the deprecated bare `REBAR_LLM_MODEL`: it fans one value out to all three
classes and collapses the per-pass frontier/standard split the gates depend on.

Confirm which way you are pointed without spending a token — this reads config only:

```sh
python -c "from rebar.llm.model_classes import resolve_model_string as r; \
print([ (c, r(c)) for c in ('trivial','standard','frontier') ])"
# project default : [('trivial', 'bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0'), …]
# opted out       : [('trivial', 'anthropic:claude-haiku-4-5'), …]
```

Things that bite on the Bedrock default:

- **Only inference-profile ids work.** A bare on-demand id (`anthropic.claude-sonnet-4-6`)
  is not invokable and returns a `ValidationException` telling you to use an inference
  profile. Use the `us.`/`global.` prefixed form, and take the id verbatim from
  `aws bedrock list-inference-profiles --region <region>` — the profile ids do **not** all
  carry a `-v1:0` suffix, and an id that does not exist fails only at call time.
- **Credentials are ambient, never rebar-managed** — the AWS chain (`AWS_PROFILE`, env keys,
  instance role). `ANTHROPIC_API_KEY` is not consulted on this path.
- **A region must resolve.** `REBAR_LLM_BEDROCK_REGION` is rebar's own knob (and so is
  visible to the verdict's provider provenance); otherwise boto3's resolution applies.
  Nothing resolving at all is a hard error, not a silent default.
- `pip install 'pydantic-ai-slim[bedrock]'` if the provider package is missing; the error
  names the package.
- **Prompt-cache lifetime is the same on both paths, so caching needs no per-provider
  tuning.** Bedrock expresses a breakpoint as a `cachePoint` block whose `ttl` is
  *optional and defaults to 5 minutes* — the same default Anthropic's `cache_control`
  applies — so rebar emitting no explicit TTL yields a 5-minute lifetime on either
  provider rather than an unbounded or absent one. Measured on Bedrock, caching engages on
  all three gates: `review-plan` read 226,668 cached tokens across 18 of 27 calls,
  `review-code` 18,292, and `verify-completion` 45,122. Non-zero cache **reads** are what
  carry the signal; a write-only tally would prove nothing. The 1-hour TTL that Bedrock
  offers for some Claude models is opt-in and unused here, so it is not a difference
  either. Untested edge: a gap longer than 5 minutes *between* calls, which no gate run
  exercises because each completes well inside one TTL window.

## No-install alternative (run repo code without an editable install)

If you can't or don't want to install rebar into the env (e.g. to avoid writing
`*.egg-info` into the working tree), run the repo code directly off `src` while borrowing
the runtime deps from any env that has them:

```sh
PYTHONPATH=src python -m rebar <args>
```

This executes the **working-tree** `src/rebar` (so it reflects un-committed edits with no
reinstall). The interpreter just needs the deps importable — point at a venv that has the
`[agents]` + core deps installed (`pydantic-ai-slim[anthropic]`, `json-repair`, `pydantic`,
`pyyaml`, `jsonschema`, `referencing`). This is handy for one-off runs; the editable venv
above remains the recommended day-to-day setup. Note this path installs nothing, so it does
**not** wire the commit gate — if you intend to commit from this checkout, run `make hooks`
once (see above).

## Claude Code: keep MCP tool schemas resident (`ENABLE_TOOL_SEARCH`)

The tracked `.claude/settings.json` sets `env.ENABLE_TOOL_SEARCH = "false"`, which keeps
every connected MCP server's tool schemas loaded up front instead of deferring them behind
a `ToolSearch` round-trip. This repo's guidance directs agents to prefer Serena's symbol
tools (`find_referencing_symbols` etc.) over `grep`, but deferral adds an extra step only to
those tools, not to `grep` via Bash — that friction gradient pushes agents to `grep`
regardless of what the docs say, so we remove it. Measured on the authoring host (a trivial
headless `claude -p` run, prompt-prefix tokens = input + cache_read + cache_creation): with
all 4 connected MCP servers, deferral OFF costs 100,813 tokens vs 36,961 with it ON — a
**+63,852** token tax per fresh prefix; the floor (Serena alone) is **+25,454**. The exact
tax scales with how many MCP servers are connected. The lever is all-or-nothing — there is
no per-server or per-tool control, so you cannot keep just Serena's three navigation tools
resident and defer the rest. To **revert**, delete the
`ENABLE_TOOL_SEARCH` key from `.claude/settings.json`. Project settings resolve to the main
checkout and are shared by worktrees, so this takes effect starting the next session in
the repo.

The other half of this story: a `PreToolUse` hook
(`scripts/hooks/serena_grep_reminder.py`, wired in the same `.claude/settings.json`) reminds
an agent about Serena's symbol tools whenever a `Bash` command invokes `grep`/`rg`/`egrep`/
`fgrep`, since launch-time guidance decays over a session. See `docs/code-navigation.md` for
the rationale.

## Code-health analyzers (`rebar metrics`) — one Python, two optional external CLIs

`rebar metrics`' **code-health lens** is backed by three separate analyzers. Only one of them
is a Python package, so only one can ride the venv. (What the metrics *mean*, and the
`unavailable` contract in general, are in
[`user-guide.md`](user-guide.md#code-health-analyzer-installation-and-fallback); this section is
just the contributor-side install.)

| Analyzer | Ecosystem | Powers | Comes from |
|---|---|---|---|
| **lizard** | Python | `complexity_summary` | `make install` (the `metrics` extra — nothing to do) |
| **scc** | Go | `module_size_distribution` | **you install it** (optional) |
| **jscpd** | Node | `duplication_summary` | **you install it** (optional) |

**lizard needs no action.** `make install` runs `uv sync --locked --extra dev --extra metrics`,
so every venv it provisions — including one made by `make worktree` — has it.

**scc and jscpd are optional.** They are ordinary CLIs on `PATH`, not Python packages, so no
`pip`/`uv` extra can deliver them. Install them only if you want the size and duplication
metrics locally:

```sh
# scc (Go) — pick whichever suits your platform
go install github.com/boyter/scc/v3@latest        # any platform with a Go toolchain (needs Go >= 1.25)
brew install scc                                  # macOS / Linuxbrew
sudo snap install scc                             # Linux (snap); MacPorts/Fedora COPR also package it
scoop install scc                                 # Windows (or: winget install --id benboyter.scc)
# no toolchain? grab a prebuilt binary from https://github.com/boyter/scc/releases and put it on PATH

# jscpd (Node) — needs a Node.js toolchain
npm install -g jscpd
```

Verify with `command -v scc` and `command -v jscpd`.

> **Not installing them is a supported configuration — it degrades, it does not fail.** When an
> analyzer is missing, only *its own* metric reports the `unavailable` state, naming the reason
> (`"scc executable is unavailable"`, `"jscpd executable not found"`); `rebar metrics` still
> exits **0** and every other metric still reports normally. Nothing else is affected either:
> `make install`, `make lint`, `make typecheck`, `make check` and the test suite neither invoke
> nor require these binaries. So a contributor who never installs scc or jscpd can run the full
> gate set and land changes exactly as normal.

> **Known gap:** even with `scc` installed, `module_size_distribution` currently reports a
> confident zero — the adapter omits `scc`'s `--by-file` flag. Tracked as bug
> `c5b3-1b8a-08dd-40af`. Until that lands, treat the scc-backed size metrics as not yet
> trustworthy; the lizard and jscpd lenses are unaffected.

## Day-to-day gates

```sh
make check     # lint + typecheck (check-only, never mutates)
make test      # default test suite (excludes integration + external)
make format    # the ONLY target that rewrites files
```
