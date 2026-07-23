# rebar — agent & contributor guide

rebar is an event-sourced ticket system + Jira reconciler exposed as a Python library
(`import rebar`), a CLI (`rebar`), and an MCP server (`rebar-mcp`), all over one git-backed
store. This file is rebar's **canonical** guidance for coding agents and contributors. It is
`AGENTS.md` — the cross-vendor standard read natively by a growing set of agent harnesses
(Codex, Cursor, Copilot's coding agent, Zed, Amp, Jules, and others) — so one source serves
every tool. **Claude Code** reads it through a one-line `@AGENTS.md` import in `CLAUDE.md`
(proven in Claude Code 2.1.211); that is why the canonical content lives here, not in a
Claude-specific file. Keep this file **lean**: an `@`-import loads at launch and does not
reduce context, so already-documented topics are one-line pointers into `docs/`, not restated
sections.

For internals see `docs/architecture.md`, `docs/event-schema.md`, `docs/concurrency.md`, and
`docs/migrations.md` (the idempotent ensure-registry). The `docs/` index is `docs/README.md`.

**Bootstrap the env with `make install`** (not a bare `pip`/`uv pip install`) so the
pre-commit hook is wired — that hook is the commit gate that runs `make lint` (ruff check +
format-check) on every `git commit`. A bare editable install skips it; if you are in such a
checkout run `make hooks` once to (re)install and verify the hook. When developing rebar
itself — running the gates/LLM ops or testing config — run the **repo checkout's** build, not
a stale global install (which silently ignores newer config keys and may lack the `[agents]`
extra): see `docs/local-dev-env.md`.

**Codex environment rule:** use the current worktree's virtualenv for every development
command. A prior `source .venv/bin/activate` does not persist across separate Codex shell-tool
calls, so prepend it explicitly, for example
`env PATH="$PWD/.venv/bin:$PATH" make lint` and
`env PATH="$PWD/.venv/bin:$PATH" make typecheck`. If `.venv` is absent, run the canonical
bootstrap from `docs/local-dev-env.md` (`python3 -m venv .venv`, activate it, then
`make install`), or create the worktree with `make worktree name=<branch>`, which provisions it.
Before reporting a lint/typecheck failure, rerun with the worktree `.venv/bin` first on `PATH`;
ambient Ruff or a missing ambient mypy is an environment error, not repository evidence. This
is the non-interactive Codex equivalent of the activated repo-venv shell used by Claude Code.

**Codex Gerrit workflow rule:** before treating the multi-story feature-branch path as a hard
prerequisite, inspect recent merged Gerrit history and confirm that the configured identity has
`feature-branch-driver` rights. If branch creation is unavailable or rejected, that is not an
implementation blocker: follow the ticket dependency order and send each independently
reviewable change through `git push gerrit HEAD:refs/for/main`, waiting for `LLM-Review +1` and
`Verified +1` before Submit and before advancing to a dependent change. Authorized drivers may
still use the server-side feature-branch flow; see ADR 0025, including its post-ADR-0047 note
that the Rebase-If-Necessary interaction needs human review.

For authenticated Gerrit `/a/` REST calls, non-interactive Codex sessions must reuse the
checkout's configured Git credential helper rather than assume a separate curl credential
store:

```sh
gerrit_credential=$(printf 'protocol=https\nhost=rebar.solutions.navateam.com\n\n' | git credential fill)
gerrit_user=$(printf '%s\n' "$gerrit_credential" | sed -n 's/^username=//p')
gerrit_password=$(printf '%s\n' "$gerrit_credential" | sed -n 's/^password=//p')
curl --fail --silent --show-error --user "$gerrit_user:$gerrit_password" \
  https://rebar.solutions.navateam.com/a/changes/
unset gerrit_credential gerrit_user gerrit_password
```

Never echo or log the credential response or password, and keep shell tracing disabled while
using them. A `curl --netrc` request returning `401` only shows that `.netrc` is absent or does
not contain the working Gerrit credential; unless `.netrc` was explicitly configured, that
response does not invalidate credentials already proven by Git over HTTPS.

## Record your work in rebar, not in scratch notes

Before starting, `search`/`list` for an existing ticket; if none fits, `create` one and
capture the plan (and its acceptance criteria) in the description. As you work, write
progress, decisions, and emergent findings back as `comment`s on the ticket (and `create` +
`link … discovered_from` for new work you uncover), so the plan and its trail live in the
store — durable, shared on every write, visible to other agents — rather than in ephemeral
TODOs or commit messages alone. Close with `transition <id> in_progress closed` when the
acceptance criteria are met.

**CLAIM BEFORE YOU WORK — always.** Every unit of work must have a ticket that YOU hold
`in_progress` *before* you touch code, run gates, or push a change for it. Run
`claim <id> --assignee <you>` (which atomically moves `open → in_progress` and sets the
assignee) as the FIRST step of working a ticket — never edit against an `open` ticket, and
never leave active work under a ticket still marked `open`. Claim at the level you are working
(the story/task/bug you implement), and when you begin executing an **epic**, move the epic
itself to `in_progress` too. If you cannot claim (a `ConcurrencyError`/exit 10 means someone
else holds it, or a gate blocks the claim), resolve that FIRST — pick another ticket, or earn
the required attestation — rather than working unclaimed. `--force[=<reason>]` bypasses any
enabled start-work gate (plan-review or whatever gate is configured, present or future) — treat
it as an escape hatch for a human operator's judgment call, not a routine agent move; it is
CLI-only and not exposed over MCP.

## The parallel-agent workflow

```
list / search ──▶ ready ──▶ next-batch ──▶ claim ──▶ (work) ──▶ transition closed
                                              │
                                   discovered new work? ──▶ create + link discovered_from
```

1. **Find work** — `search <query>` (full-text over titles/descriptions/comments/tags) or
   `list --status=open`; `ready` returns tickets whose blockers are all closed;
   `next-batch <epic>` returns a conflict-aware unblocked batch (uses recorded file-impact).
2. **Grab work atomically** — `claim <id> --assignee <you>`: moves an **open** ticket to
   `in_progress` and sets the assignee in one step. If another agent already claimed it you
   get **ConcurrencyError / exit 10** — do not retry the same ticket; pick another. Never
   hand-roll claim as `transition`+`edit` (that races). A **parent-first cascade** pulls a
   still-`open` parent into progress first (see `docs/concurrency.md`).
3. **Record provenance** — when work surfaces new work, `create` the ticket and
   `link <new> <parent> discovered_from`.
4. **Finish** — `transition <id> in_progress closed` (optimistic-concurrency: pass the status
   you believe is current; a mismatch is exit 10). `reopen` moves a closed ticket back to open.

## Where to read (one-line pointers into `docs/`)

These topics have an authoritative home in `docs/`; read them there rather than expecting them
restated here:

- **Ticket model** — the `idea` status, parent/child hierarchy, the six link relations +
  blocking-link promotion, and tags (incl. `--set-tags` add-wins) → `docs/ticket-model.md`.
- **Writing a plan that PASSES the plan-review gate** — the author-facing on-ramp: the
  description template, the blocking checklist ("your plan must…"), and the revise→review→claim
  loop → start here: `rebar explain plan` (packaged; source `src/rebar/_guides/writing-a-passing-plan.md`).
- **Gate protocols** — the plan-review claim gate and the completion-verifier close gate
  (both **on** for this project), their attestation model, how to remediate, and — because a
  moving base ref silently makes an attestation stale — how to check currency cheaply with
  `rebar review-plan <id> --status` (read-only, no LLM) instead of re-running the review →
  `docs/plan-review-gate.md`.
- **Plan-review criteria reference** — the generated per-criterion registry (one section per
  criterion, the reviewer's detection detail), the per-ticket structural quality gates, and
  the `.rebar/criteria_routing.json` overlay → `docs/plan-review-criteria-guide.md` (and
  `rebar explain <criterion-id>`).
- **MCP tool set** — the read/write tool inventory and their `outputSchema`s →
  `docs/mcp-reference.md`.
- **Concurrency** — optimistic concurrency, the parent-first claim/transition cascade, and
  "the store shares every write immediately" (auto-commit + auto-push to `sync.remote`) →
  `docs/concurrency.md`.
- **Session logs** — the `session_log` type semantics and the `session-log` helper +
  auto-rotation → `docs/event-schema.md` and `docs/user-guide.md`.
- **LLM agent operations** — `review`, `verify-completion`, `review-code`, `scan-spec` (the
  optional `[agents]` framework) → `docs/llm-framework.md`.
- **Library / reuse surface** — the full library API and reusable subsystems →
  `docs/reuse-surface.md`.
- **Metrics** — the `rebar metrics` command (agent-process / code-health / delivery /
  gate-economics lenses, the `unavailable` state, source/confidence labels) →
  `docs/user-guide.md`; the `rebar.metrics` registry/reuse surface → `docs/reuse-surface.md`.
- **ChatGPT / connector-limited sessions** — detecting a checkout-less, tracker-less
  environment, the safe fallback ticket payload, and the sanctioned exceptional import path →
  `docs/chatgpt-agent-guide.md`.

## Module-size policy (when editing rebar itself)

rebar is built to be edited by agents that load a unit whole. **Target 200–500 LOC per file;
hard cap 800.** When a unit grows past the cap, split it **only along call-graph seams that
already exist** (extract a cluster of functions that already call each other) — never
mechanically to hit a number, and **never create files < 100 LOC** by splitting. Prefer
**deleting** oversized bash via the bash→Python strangler-fig migration over carving it into
more bash. The 800 cap is **absolute** — a CI **module-size gate** fails the build when ANY
`src/rebar` file exceeds it; there is no allowlist escape hatch (epic 716f drained and removed
it). The limit is single-sourced in `.github/module-size-limit.txt` and **locked**: changing
it requires an administrator to override the gate (a normal contributor change to the limit
fails CI).

## Navigating the codebase (when editing rebar itself)

This checkout has the **Serena** MCP server configured (LSP-backed, Pyright over `src/rebar`)
for *semantic* code navigation. **Prefer its symbol tools over `grep`** when finding or
following references — `find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, and
symbol-precise edits (`replace_symbol_body`, `insert_after_symbol`). It resolves "who calls /
imports this?" reliably (definitions + references, not text matches), which cross-cutting
refactors need. Serena's tools load at session start; if absent, verify with
`claude mcp get serena`. `grep`/the search tools remain the fallback when Serena is
unavailable or for non-symbol (text/comment/string) searches.

## Git workflow — land changes THROUGH GERRIT, not GitHub PRs

**Every change to `main` must pass two independent Gerrit gates before it can land — the
`LLM-Review` vote (the rebar review-bot's LLM code review) AND the `Verified` vote (CI:
build/test/lint/typecheck on GitHub Actions).** `main` flows through Gerrit; GitHub is a
read-only mirror that rejects direct pushes and PR merges. **For an author-facing on-ramp — the
commit checklist, what the `LLM-Review` reviewer scores (blocking vs advisory), how to
respond to each vote, and how to preview the review locally with `rebar review-code` before you
push (the code-review analog of `rebar review-plan`) — read the packaged code-review guide via
`rebar explain review` (source `src/rebar/_guides/passing-code-review.md`).**
The full recipe — Gerrit access setup, feature branches for multi-story work, conflict
handling — is in [CONTRIBUTING.md](CONTRIBUTING.md); the agent-actionable rules:

- **Work in a fresh worktree branched from current `origin/main`**, not the main checkout
  (`git fetch origin && git worktree add ../<name> -b <branch> origin/main`; or
  `make worktree name=<branch>` to also provision the venv). `cd` into it and run every
  subsequent command — edits, gates, `rebar`, the ticket close, and `git` — from inside it.
- **Two remotes (split residency):** `origin` → GitHub (the code mirror **and** the `tickets`
  branch's source of truth = the `sync.remote` rebar auto-pushes ticket events to); `gerrit` →
  the code-review remote. **Code review goes to `gerrit`; ticket events go to `origin`.**
- **Every commit needs** a `rebar-ticket: <id>` trailer (or a leading `<id>:` subject) so CI's
  `Verified` gate accepts it (`rebar explain commit-trailer` for the exact format and accepted
  id forms; `rebar verify-commit-ticket` to check a commit locally), **and** a DCO sign-off.
  Before committing, verify
  `git config user.name` and `git config user.email` are set to **your own real, configured
  git identity** (not a placeholder), then add the sign-off with `git commit -s` — it stamps
  `Signed-off-by: <that name> <that email>`. A machine/operator that runs commits under a
  dedicated automation identity (e.g. a bot account) scopes that identity to its own
  machine-local config, never to this canonical guidance (see `rebar explain review` /
  `CONTRIBUTING.md` §"Sign your work (DCO)" for the full policy). A fresh
  worktree lacks the `commit-msg` hook that stamps the `Change-Id` — install it:
  `curl -sLo "$(git rev-parse --git-path hooks/commit-msg)" https://rebar.solutions.navateam.com/tools/hooks/commit-msg && chmod +x "$(git rev-parse --git-path hooks/commit-msg)"`.
- **Push for review:** `git push gerrit HEAD:refs/for/main` (the magic ref creates a Gerrit
  change; it does not touch `main`). Iterate on findings with `git commit --amend --no-edit`
  (keep the `Change-Id`) + re-push.
- **The gate:** a change is submittable only at **`LLM-Review +1` AND `Verified +1` AND no
  unresolved comments** — only the bots/admins cast either label, so you cannot self-approve.
  A `Verified -1` is a CI failure (open the run; comment `recheck` if it is a flake).
- **Land it yourself with a plain Gerrit Submit** once both votes are green. `main` is
  Rebase-If-Necessary: Gerrit rebases + submits server-side, so you do **not** pre-rebase
  except on a textual conflict it cannot resolve. Do **not** close a ticket until its change
  is `Verified +1` — a passing completion-verifier is **not** a substitute for green CI.
- **After the change MERGES, fast-forward YOUR worktree before you close.** The close's
  completion precheck reads your worktree's git history and requires the merged
  `rebar-ticket: <id>` commit reachable from HEAD (else it fails with "cannot confirm the work
  landed"). So from inside the worktree, before `rebar transition <id> in_progress closed`, run
  `git fetch origin && git merge --ff-only origin/main` (or `git pull --ff-only`). **NEVER**
  fast-forward, reset, or stash the shared main checkout to satisfy a close — it may hold
  uncommitted operator WIP. Advance your own worktree (or a throwaway worktree at `origin/main`)
  instead.
- **Multi-story features → a server-side feature branch** (not one giant change or a fragile
  chain) — see [CONTRIBUTING.md](CONTRIBUTING.md) §4.

(This governs *code*. rebar's own **ticket events** on the `tickets` branch still
auto-commit/auto-push and do NOT go through Gerrit.)

## Library quick reference

```python
import rebar
tid = rebar.create_ticket("task", "title", return_alias=True)   # -> {"id","alias"}
rebar.claim(tid["id"], assignee="me")                            # raises ConcurrencyError if taken
rebar.link(child, parent, "discovered_from")
rebar.transition(tid["id"], "in_progress", "closed")
```

The full library API and the reusable subsystems (signing, LLM runtime, prompt/contract,
output-schema seams) are documented in `docs/reuse-surface.md`.
