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
the required attestation — rather than working unclaimed.

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
- **Gate protocols** — the plan-review claim gate and the completion-verifier close gate
  (both **on** for this project), their attestation model, and how to remediate →
  `docs/plan-review-gate.md`.
- **Quality gates + ticket template + project-supplied criteria** — the per-ticket structural
  gates, the per-type description template, and the `.rebar/criteria_routing.json` overlay →
  `docs/plan-review-criteria-guide.md` (and `rebar explain <criterion-id>`).
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

## Module-size policy (when editing rebar itself)

rebar is built to be edited by agents that load a unit whole. **Target 200–500 LOC per file;
soft cap 800.** When a unit grows past the cap, split it **only along call-graph seams that
already exist** (extract a cluster of functions that already call each other) — never
mechanically to hit a number, and **never create files < 100 LOC** by splitting. Prefer
**deleting** oversized bash via the bash→Python strangler-fig migration over carving it into
more bash. Over-cap offenders and their remedies are tabulated in `docs/architecture.md`, and
a CI **module-size gate** fails the build when a *new* file exceeds the soft cap and is not in
`.github/module-size-allowlist.txt`, so the over-cap set cannot silently grow.

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
read-only mirror that rejects direct pushes and PR merges. The full recipe — Gerrit access
setup, feature branches for multi-story work, conflict handling — is in
[CONTRIBUTING.md](CONTRIBUTING.md); the agent-actionable rules:

- **Work in a fresh worktree branched from current `origin/main`**, not the main checkout
  (`git fetch origin && git worktree add ../<name> -b <branch> origin/main`; or
  `make worktree name=<branch>` to also provision the venv). `cd` into it and run every
  subsequent command — edits, gates, `rebar`, the ticket close, and `git` — from inside it.
- **Two remotes (split residency):** `origin` → GitHub (the code mirror **and** the `tickets`
  branch's source of truth = the `sync.remote` rebar auto-pushes ticket events to); `gerrit` →
  the code-review remote. **Code review goes to `gerrit`; ticket events go to `origin`.**
- **Every commit needs** a `rebar-ticket: <id>` trailer (or a leading `<id>:` subject) so CI's
  `Verified` gate accepts it, **and** a DCO sign-off — exactly
  `Signed-off-by: Joe Oakhart <joeoakhart+bot@navapbc.com>` (add with `git commit -s`). A fresh
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
