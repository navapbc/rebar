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

## TL;DR (canonical setup)

```sh
# from the repo root
uv venv .venv && source .venv/bin/activate     # or: python -m venv .venv && source .venv/bin/activate
make install                                    # editable '.[dev]' install + the pre-commit hook (the commit gate)
export ANTHROPIC_API_KEY=sk-ant-...             # required for the LLM ops (review-plan, verify-completion)
```

> **Starting a NEW worktree? One command does the whole setup.** `make worktree name=<branch>`
> creates a fresh worktree at `../<branch>` (override with `dir=<path>`) branched from a
> freshly-fetched `origin/main`, then provisions its `.venv` and runs the canonical
> `make install` above inside it — the one-command form of the manual "fresh worktree + local
> venv" sequence this repo mandates. Then `cd ../<branch> && source .venv/bin/activate` and
> export your `ANTHROPIC_API_KEY`.

> **Signing your ticket writes (per-clone identity).** Every clone that writes non-exempt
> tickets should own its **own** identity + SSH signing key (never the shared bot). One-time
> setup — create/own an identity ticket, set the current-identity pointer, and point
> `identity.signing_key` (or `REBAR_IDENTITY_SIGNING_KEY`) at your **per-machine, uncommitted**
> SSH private key — is documented in [`identity.md`](identity.md) under "Setting up signing in
> a local dev / agent clone". The key never leaves your machine; only your public key lives in
> the store.

**Use `make install` — it is the one canonical setup path.** It runs `pip install -e
'.[dev]'` (which pulls `nava-rebar[agents]` plus the lint/type/test tooling, so the
editable install is a complete env for everything below) **and** wires the pre-commit hook
via `make hooks`, which is what makes lint/format run on every `git commit`. The Makefile
is the single source of truth for lint/format/type/test — see `make help`.

> **Why not just `pip install -e '.[dev]'`?** A bare editable install does **not** wire the
> commit hook — `git` hooks are opt-in per clone and no `pip`/`uv` install step runs
> `pre-commit install`. Skip the hook and lint/format errors sail through `git commit` and
> are only caught later by CI (the slow gate). If you must run the install step by hand,
> follow it with the hook step and verify it:
>
> ```sh
> pip install -e '.[dev]'     # or: uv pip install -e '.[dev]'
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
- **`ANTHROPIC_API_KEY`** in the environment (the calls are live + billable).

When the gate is enabled but a dependency is missing, the review currently degrades to a
deterministic-floor-only result instead of failing loudly — a known defect (bug
`fuel-posse-ball`). Until it's fixed, treat any `review-plan` output with
`coverage.llm_ran == false` as **not a real review**, regardless of the `PASS` verdict.

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

## Day-to-day gates

```sh
make check     # lint + typecheck (check-only, never mutates)
make test      # default test suite (excludes integration + external)
make format    # the ONLY target that rewrites files
```
