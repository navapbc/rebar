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
above remains the recommended day-to-day setup. Note this path installs nothing, so it does
**not** wire the commit gate — if you intend to commit from this checkout, run `make hooks`
once (see above).

## Day-to-day gates

```sh
make check     # lint + typecheck (check-only, never mutates)
make test      # default test suite (excludes integration + external)
make format    # the ONLY target that rewrites files
```
