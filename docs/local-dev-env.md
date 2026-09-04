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

## Prerequisite — provision the venv on the Python CI tests

**Create the venv with `make venv`, not `python3 -m venv`.** `make venv` asks uv for the
interpreter version this project pins and **fails loudly** if it cannot get one, rather than
silently building on whatever the host's ambient `python3` resolves to.

That fallback was a real bug (a5f5). `make worktree` used to provision with `python3 -m venv`;
on a machine whose `python3` had moved to 3.14 while CI tested 3.11–3.13, every worktree the
repo's own one-command setup produced ran an interpreter CI never exercised — and because
`requires-python` is only `>=3.11`, `uv sync --locked` accepted it without a word. The result
is local failures CI cannot reproduce, which teaches everyone to discount local failures in
general; and its mirror image, a change that breaks on 3.11 but passes on the developer's 3.14
and only fails after push.

The value is single-sourced in **`.github/python-version.txt`** and read by two consumers that
must agree: the `venv` target in the `Makefile`, and `tests/unit/test_worktree_python_pin.py`,
which fails if the pin ever leaves the CI matrix in `.github/workflows/_build-and-test.yml` or
disagrees with the `actions/setup-python` pins. The full **tested** range is wider than the pin
— the matrix runs 3.11, 3.12 and 3.13 on Linux — so any of those is a legitimate interpreter to
develop on; the pin just picks the one a fresh venv gets by default, matching the version the
lint/type-check lane runs.

```sh
make venv                    # .venv on the pinned interpreter (uv fetches it if needed)
uv python install 3.12       # only if make venv reports it cannot find that version
```

## Prerequisite — uv is pinned, and uv enforces the pin on you

`uv` itself is pinned to an exact version by **`[tool.uv] required-version` in
`pyproject.toml`** — one line, the single source of truth for CI and for your laptop alike. If
your local `uv` is a different version, **every** `uv` command in this repository stops with:

```
error: Required uv version `==0.12.7` does not match the running version `0.7.18`.
Update `uv` by running `uv self update 0.12.7`.
```

That error is **expected and self-describing, not a broken checkout** — run the command it
names. It is the same class of guard as the interpreter pin above: `make venv` refuses to build
on an unpinned Python for the same reason this refuses to build on an unpinned uv, because a
local toolchain that silently differs from CI's teaches you to discount local failures.

The pin also makes CI's uv deterministic. `astral-sh/setup-uv` is SHA-pinned at all 25 call
sites, but with no version resolvable from the checkout it fell back to fetching a remote
manifest from `raw.githubusercontent.com` to decide which uv to install — so the version could
change between runs, `release.yml` included, with no repository change at all. An exact `==` pin
is resolved locally with no network call, and CI logs `Found version for uv in
.../pyproject.toml` instead of the fallback.

It does **not** yet remove the network dependency itself: the action fetches that same manifest a
second time, in `downloadVersion` -> `getArtifact`, to obtain the download URL, and that call is
unconditional whenever uv is not already in the runner tool cache. So the `##[error]fetch failed`
failure mode (bug `56b7-b21a-c8ab-4afc`; run 33214025855 died that way in 21 seconds) is still
reachable — tracked as bug `5caa-4b63-7ea2-4e71`.

The `==` form is load-bearing: a range such as `>=0.12.7` reads as pinned but sends the action
back down the manifest-fetch path. `scripts/check_uv_pin.py` (run by `make lint`) fails the
build if the pin is removed, loosened to a range, shadowed by a root `uv.toml`, or overridden
by a per-call-site `version:` input — so **upgrading uv is a deliberate one-line change** to
`pyproject.toml`, and nothing else needs to move.

## TL;DR (canonical setup)

```sh
# from the repo root
make venv                                       # .venv on the CI-pinned Python (.github/python-version.txt)
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
> freshly-fetched `origin/main`, then provisions its `.venv` on the **pinned** interpreter
> (`make venv`, see above) and runs the canonical `make install` inside it — the one-command
> form of the manual "fresh worktree + local venv" sequence this repo mandates. Then `cd ../<branch> && source .venv/bin/activate` and
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

### Mutation testing runs sandboxed (`REBAR_MUTATION_ALLOW_UNSANDBOXED`)

`scripts/mutation_gate.py` runs both shard-test-executing subprocesses — the baseline
`pytest` and the `mutmut run` mutant test-run — inside an OS sandbox that denies
filesystem writes outside the scratch tree and the pytest basetemp. The venv is
deliberately EXCLUDED from that allow-list: a writable venv would let a mutant drop
executable code into site-packages for a later un-sandboxed phase to import. macOS uses
Seatbelt (`sandbox-exec`); Linux uses `bwrap`.

This exists because mutation testing executes *deliberately broken* code. On
2026-08-26 a mutation removed a guard from a script performing real deletion, a test
exec'd it with an empty path variable, and the glob expanded to `rm -rf /*` —
destroying `/opt/homebrew` and every Homebrew-installed app in `/Applications`. No
mutation tool in any language sandboxes runtime effects; they isolate only the source
tree, which does nothing once the mutant calls `rm -rf`.

With no mechanism installed the run **aborts** — everywhere, including CI. That is
deliberate: a silent unsandboxed fallback is indistinguishable from a sandboxed run.
Ambient `CI` used to waive the sandbox on its own; it no longer does (`f11d-f8fd`),
because `CI` is inheritable and trivially set, so a workstation with `CI=1` exported got
the same silent waiver. There is now exactly one way to run unsandboxed, and it has to
be named:

```sh
REBAR_MUTATION_ALLOW_UNSANDBOXED=1 python scripts/mutation_gate.py ...
```

It logs a WARNING when set. **Understand the risk before using it** — a mutant that
reaches a destructive code path can delete real files anywhere your user can write.
Prefer installing `bwrap` (`apt install bubblewrap`) over setting this.

### The commit gate needs the dev tools on `PATH` (activate the venv before committing)

The hooks run `make lint` (ruff, shellcheck) and `make typecheck` (mypy), which invoke the
**bare** `ruff` / `shellcheck` / `mypy` resolved from `PATH` — the same commands CI runs, so
the hook, `make`, and CI never drift. That means the shell you `git commit` from must have the project venv **active**
(or the `[dev]` tools otherwise on `PATH`). `make install` into an activated venv puts them
there; the canonical setup above (`source .venv/bin/activate`) satisfies this.

- **Symptom when the venv is NOT active:** a hook fails with `make: mypy: No such file or
  directory` (or `ruff`), or `check_shellcheck` reports shellcheck missing — even though your
  code is clean. It is an environment problem, not a code problem. (A split env where only *one* tool leaked onto the global `PATH` makes this
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
a resolvable region**.

**`ANTHROPIC_API_KEY` is still expected to be set, and to be VALID.** It is not consulted for the
*primary* Bedrock call, but direct Anthropic is the project's **approved fallback arm** (operator
decision, ticket `2876-7958-8dee-4882`), so that key is what a Bedrock outage or throttle is meant
to fall back onto. Two things follow. First, the note in
`.github/llm-providers/bedrock.toml` that the Bedrock arm "deliberately blanks
`ANTHROPIC_API_KEY`" scopes to **the CI provider matrix only** — it stops an arm named `bedrock`
from being silently served by Anthropic; it is not a statement about your machine. Second, a
*stale or revoked* key is worse than an absent one: the fallback is attempted and fails with a
`401 authentication_error`, which reads as an LLM outage rather than as your credential. Check it
in one call before you blame the gate:

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://api.anthropic.com/v1/models \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H 'anthropic-version: 2023-06-01'   # 200 = usable
```

Note that the `fallback` chain itself is **not yet enabled** in `rebar.toml`: turning it on makes
the runner intersect capabilities across the chain, which would silently move every standard-class
call from native to prompted structured output. `rebar.toml` records both blockers inline. And
when it is enabled, a fallback rescues **availability** failures only — a credential failure
deliberately does *not* fail over (`model_classes.should_fall_back`), so a chain will never paper
over missing AWS credentials; fix those directly.

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

The bare `REBAR_LLM_MODEL` was **removed** (pre-1.0 breaking pass #3) and now fails loud: it
fanned one value out to all three classes and collapsed the per-pass frontier/standard split the
gates depend on.

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
- **A region must resolve.** rebar's own chain is `REBAR_LLM_BEDROCK_REGION` (rebar's knob,
  recorded with its source in the verdict's provider provenance) > `AWS_DEFAULT_REGION` >
  `AWS_REGION` > boto3's profile resolution — plain `AWS_REGION` works.
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
make check     # lint + typecheck — the tight loop (~40 s); NOT the pre-push gate
make verify    # THE PRE-PUSH GATE: lint + typecheck + the default suite (~22 min)
make test      # default test suite (excludes integration + external), -n 4 --dist worksteal
make format    # the ONLY target that rewrites files
```

**`make verify` is what must pass before `git push gerrit`, and it costs minutes, not
seconds.** `make check` and `make lint` are check-only gates over the *source text*; a large
family of this repo's invariants — the ratchets' baseline-vs-tree comparisons, the public-API
surface census, CI-workflow parity, generated-artifact and docs drift, whole-tree AST policy
scans — is enforced by **pytest** instead, and none of it is reachable from `make lint`. Three
changes were pushed red by exactly those tests on 2026-09-04 after their authors ran `make
lint` and `make typecheck` and saw green (bug `1035-bed7-c855-4732`). Budget for `make verify`
deliberately: it is cheaper than a 15–20 minute `Verified -1` round trip, and it is the only
local command that can honestly be called "I verified this".

Measured on a six-performance-core host under load: lint ~27 s, typecheck ~10 s, and 19229
tests in 22 min 13 s at the default `PYTEST_WORKERS=4`. Raise it for a bigger box
(`make test PYTEST_WORKERS=8`); do **not** reach for `-n auto`, which resolves to the logical
CPU count and over-subscribes badly — on that same host `auto` (18 workers) was still at 7%
after eight minutes, because much of this suite forks git/bash/pytest subprocesses of its own.

### The per-test hang budget (300 s) and how to override it

`[tool.pytest.ini_options]` sets `timeout = 300` and `timeout_method = "thread"`, so
**`make test` and a bare local `pytest` carry the same per-test budget CI does**. Before this
the budget lived only on the CI command lines, so a deadlocked test hung a local run
indefinitely and the CI-only guard could rot with no local signal.

The value is sized to **dwarf** the slowest legitimate test, not to sit just above it. On
the gating `ubuntu-latest, py3.13` CI leg — the only one adding `--cov=rebar` — about
fourteen whole-tree-scanning gate tests legitimately cost 18.7 s to 29.93 s of `call` time
under `-n 4 --dist worksteal`; a local unit-tier run cannot see that (no coverage tracing,
no scripts tier, faster cores). A budget tight enough to clip them does **not** report
`Failed: Timeout`: because `timeout_method = "thread"`, pytest-timeout expires a test with
`os._exit(1)`, which kills the whole xdist worker (`node down: Not properly terminated`).
The budget covers **fixture setup and teardown as well as the test body** — the ini
deliberately does *not* set `timeout_func_only` (bug `797b-bbc4-01cf-42d5`): that key exempted
fixture phases, which is exactly where the original incident hung (a teardown awaiting
`queue.join()` under a 1200 s drain, bug `89d5-61da-b621-47f8`). pytest-timeout's upstream
default is `False`, and its README treats `func_only` as a last-resort workaround.

A test that must legitimately run longer — including one whose **fixtures** are legitimately
slow, since fixture time is charged to the test — overrides the ini with a marker **and a
one-line comment naming why** — silent exemptions are not acceptable:

```python
# timeout: drives a real 45 s subprocess handshake; there is no faster oracle.
@pytest.mark.timeout(90)
def test_something_genuinely_slow() -> None:
    ...
```

Both halves are pinned by `tests/unit/test_timeout_budget.py`: that the marker still beats
the ini, and — in a subprocess, since a permanently-red test cannot be committed — that the
ini alone really does expire an over-budget test. The two `_build-and-test.yml` lanes carry
**no** `--timeout`/`--timeout-method` flags at all (held flag-free by
`tests/unit/test_ci_workflow_parity.py`), so the ini is their single source: pytest-timeout's
precedence is CLI > env > ini, and the Gerrit Verified gate runs the workflow files from
trusted `main`, so a CLI flag would permanently shadow any patchset's ini change (bug
`3fa7-94ba-42aa-4623`, ticket `5a30-d423-b1f5-4e33`). The live-service, clean-venv and eval
lanes keep their own, larger, calibrated budgets.

### Running the tests CI runs (the optional-extra surface)

`make install` deliberately stays lean, so ~38 tests gated on
`pytest.importorskip("fastapi")` / `("jinja2")` — the review-bot receiver, the opcert
service app, the audit UI, and the path-injection + token-redaction security guards —
**skip** in that env. CI does not: its pytest lane installs `dev + reviewbot + ui` and sets
`REBAR_REQUIRE_EXTRAS=1`, which turns any such skip into a hard error (see
`tests/_extra_guard.py`). That env var is what makes a lost extra reddening instead of
silent — a whole surface once vanished from CI for months because nothing reported it
(bug `599e-77da-29dd-482d`).

To reproduce the CI selection locally:

```sh
uv sync --locked --extra dev --extra reviewbot --extra ui
REBAR_REQUIRE_EXTRAS=1 pytest -m "not integration and not external"
```

Nothing these extras unlock needs network or cloud credentials; the suite's own
network guard still applies.

### Validating the whole tree locally (and whether detached runs are trustworthy)

A serial local run of the full suite takes hours on a dev machine and exceeds most agent
tool timeouts, so the tree is usually covered either in chunks or by detaching the run.

**Detached runs ARE trustworthy — but only if nothing else touches the checkout while one
is in flight.** The suite is not sensitive to being detached as such: an absent controlling
terminal, `stdin` at `/dev/null`, and a different process session change nothing. What
breaks a detached run is that detaching it is precisely what frees you to keep working in
the same worktree, and several guards compare whole-checkout state before and after **each
test**. A concurrent commit, rebase, branch switch, or stray file therefore fails whichever
test happened to straddle it — an arbitrary, innocent one, different on each run:

* `_no_repo_root_leaks` (`tests/conftest.py`) — a new top-level entry in the checkout.
  It reports only; it no longer removes what it finds, because it cannot tell your write
  from a leaking test's (bug `746c-185a-0e48-4b83`). A genuine leak is therefore yours to
  clean up, and the failure message names each entry.
* `_no_repo_commits` (`tests/conftest.py`) — "The repo HEAD moved during this test (X -> Y)".
* the session working-tree backstop (`tests/conftest.py::pytest_sessionfinish`) — diffs
  `git status --porcelain` across the **whole session** and fails the run when any new dirty
  entry appeared ("REPO ISOLATION FAILURE: new changes appeared in the checkout during this
  test run"). Unlike the two per-test guards it fires **once, at session end**, rather than
  blaming an arbitrary straddling test — the run tail shows a session-level failure, not an
  innocent test reported as errored.

The two per-test guards fire in **teardown**, so the run reports the test as *passed* AND raises
an error: a
tail reading `N passed, 1 error` is one run reporting both, not a contradiction. A killed
child process (a reaped run) shows up separately, as a harness probe that produced no
output; those diagnostics now report the child's `returncode`, so a `-9`/`SIGKILL` marks an
external kill rather than a product failure.

So, in order of preference:

1. **Run it from a worktree you are not editing.** `make worktree name=<something>` and
   leave it alone until it finishes. This is the whole fix — with no concurrent writer,
   none of the guards above can misfire.
2. **Use the CI selection and flags,** which are far faster. The per-test hang guard now
   comes from the ini (see "The per-test hang budget" above), so a bare `pytest` already has
   one; `pytest-xdist` is still **not** enabled by `addopts`, so a bare `pytest` is serial:

   ```sh
   pytest -m "not integration and not external" -q \
     -n "$(sysctl -n hw.perflevel0.logicalcpu 2>/dev/null || nproc)" --dist worksteal \
     -p no:cacheprovider --basetemp="$(mktemp -d)"
   ```

   Passing `--timeout=` on the command line OVERRIDES the ini for the whole run — useful when
   bisecting a genuine hang, but do not leave it in a script: it silently un-guards every test.

3. **If you must detach, do not work in that checkout until it exits** — not even a
   `git commit`, and not a `rebar` command either: tracker writes land under the checkout
   (`.rebar/`, `.tickets-*`) and read as new entries to the leak guard, failing whichever
   test straddles them. Running `rebar` against a worktree while its own suite runs is
   **not supported**. Park edits, and tracker work, in a different worktree.

Do **not** conclude that an error from a detached run is spurious. Read it: if it names a
repo-state guard or a `returncode` that is negative, it is an artifact of a concurrent
writer or a reap and the guidance above prevents it; anything else is a real failure and a
flaky test is a bug to root-cause, never a retry (see `CONTRIBUTING.md` §6). Background:
bug `f0fb-de7a-b315-4508`.
