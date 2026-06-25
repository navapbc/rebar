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
uv pip install -e '.[dev]'                      # or: make install   (also installs the pre-commit hook)
export ANTHROPIC_API_KEY=sk-ant-...             # required for the LLM ops (review-plan, verify-completion)
```

`.[dev]` pulls `nava-rebar[agents]` plus the lint/type/test tooling, so the editable
install is a complete env for everything below. `make install` runs the same `pip install
-e '.[dev]'` and additionally installs the pre-commit hook (the Makefile is the single
source of truth for lint/format/type/test — see `make help`).

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
above remains the recommended day-to-day setup.

## Day-to-day gates

```sh
make check     # lint + typecheck (check-only, never mutates)
make test      # default test suite (excludes integration + external)
make format    # the ONLY target that rewrites files
```
