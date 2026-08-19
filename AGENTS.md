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
bootstrap from `docs/local-dev-env.md` (`make venv`, activate it, then `make install`), or
create the worktree with `make worktree name=<branch>`, which provisions it. Use `make venv`
rather than a bare `python3 -m venv` — it pins the interpreter to the version CI tests
(`.github/python-version.txt`) instead of inheriting the host's ambient `python3`.
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

**Choose ticket identifiers by context.** After discovering or resolving a ticket, use its
exact human-friendly **alias** in subsequent tracker commands (`show`, `comment`, `edit`,
`claim`, `transition`, and links); do not keep copying a short hexadecimal prefix. Reserve the
full canonical four-quad ticket ID for identity-sensitive artifacts, especially
`rebar-ticket:` trailers, signatures/attestations, and durable cross-system references. The
resolver's acceptance of aliases and short forms in some identity-sensitive paths is
compatibility behavior, not the authoring policy.

The forms to WRITE, in order of preference: the **alias** (`postwar-bardic-walleye`), the
**8-digit two-quad** short id (`c50e-7326`), the **full canonical id**
(`c50e-7326-9cac-45e4`), and **Jira/bridge ids** (`REB-310`). A bare **4-digit single-quad**
fragment (`c50e`) is **deprecated** as a reference form — in a store of any size those quads
collide, so they resolve to nothing and make ordinary prose that happens to contain a hex
fragment look like a ticket citation. Existing 4-digit references keep resolving; that is
compatibility only, so do not introduce new ones.

Before starting, `search`/`list` for an existing ticket; if none fits, `create` one and
capture the plan (and its acceptance criteria) in the description. As you work, write
progress, decisions, and emergent findings back as `comment`s on the ticket (and `create` +
`link … discovered_from` for new work you uncover), so the plan and its trail live in the
store — durable, shared on every write, visible to other agents — rather than in ephemeral
TODOs or commit messages alone. Close with `transition <id> in_progress closed` when the
acceptance criteria are met.

**CLAIM BEFORE YOU WORK — always.** Every unit of work must have a ticket that YOU hold
`in_progress` *before* you touch code, run gates, or push a change for it. Run
`claim <id>` (which atomically moves `open → in_progress` and sets the assignee to the
configured `ticket.default_assignee`) as the FIRST step of working a ticket — **omit
`--assignee`** so that default applies; only pass one when you mean to override it, and then
only a **Jira-resolvable** identity (email or accountId) — a **bare handle** like `RebarBotNava`
cannot be resolved to a Jira user and the next reconcile silently clears it
(`docs/config.md#ticketdefault_assignee`). Never edit against an `open` ticket, and
never leave active work under a ticket still marked `open`. Claim at the level you are working
(the story/task/bug you implement), and when you begin executing an **epic**, move the epic
itself to `in_progress` too. If you cannot claim (a `ConcurrencyError`/exit 10 means someone
else holds it, or a gate blocks the claim), resolve that FIRST — pick another ticket, or earn
the required attestation — rather than working unclaimed. `--force[=<reason>]` bypasses any
enabled start-work gate (plan-review or whatever gate is configured, present or future) — treat
it as an escape hatch for a human operator's judgment call, not a routine agent move. It is
available on **every** surface — library, CLI, and MCP (`transition_ticket` / `claim_ticket`
take a reason-bearing `force`) — and is audited not by withholding it from any surface but by
the **absence of a signed attestation**: a forced op records no certification, so a project
that must keep force from circumventing a gate enforces it by checking for that certification
in CI.

## The parallel-agent workflow

```
list / search ──▶ ready ──▶ next-batch ──▶ claim ──▶ (work) ──▶ transition closed
                                              │
                                   discovered new work? ──▶ create + link discovered_from
```

1. **Find work** — `search <query>` (full-text over titles/descriptions/comments/tags) or
   `list --status=open`; `ready` returns tickets whose blockers are all closed;
   `next-batch <epic>` returns a conflict-aware unblocked batch (uses recorded file-impact).
   **Plan-review in dependency order — never review a ticket and its dependencies in
   parallel.** A review pins its direct dependencies' (children/prerequisites') material, so a
   dependency changing after the review invalidates it (a fresh `review-plan` is then required;
   `sign-review` will not certify across a changed dependency). Review prerequisites/children
   before their dependents; prefer `next-batch` over ad-hoc parallel review — see
   `docs/plan-review-gate.md` §"Review dependencies FIRST". This is partly **enforced**:
   `review-plan` on a ticket that is not yet claimable — status `closed`/`idea`/`blocked`, or
   `open` but still blocked by an unclosed dependency — **fast-fails with no LLM** (unsigned
   `INDETERMINATE`, exit 2) unless you pass `--force`; close the prerequisites, then review.
2. **Grab work atomically** — `claim <id>`: moves an **open** ticket to
   `in_progress` and sets the assignee to the configured `ticket.default_assignee` in one step
   (omit `--assignee`; an explicit one wins over the default, and must be **Jira-resolvable** —
   email or accountId, never a **bare handle**, which reconcile silently clears —
   `docs/config.md#ticketdefault_assignee`). If another agent already claimed it you
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
- **Portability** — rebar must work across environments with **diverse CI providers or no
  CI provider**, so a capability whose only trigger is a specific CI system is not portable
  (an operation-linked or in-process fallback is required); this is enforced by the blocking
  `project.portability` plan-review criterion → `docs/plan-review-gate.md` (and
  `rebar explain project.portability`).
- **MCP tool set** — the read/write tool inventory and their `outputSchema`s →
  `docs/mcp-reference.md`.
- **Concurrency** — optimistic concurrency, the parent-first claim/transition cascade, and
  "the store shares every write immediately" (auto-commit + auto-push to `sync.remote`) →
  `docs/concurrency.md`.
- **Mutating the tracker** — the rule is **no AD-HOC raw git in the tickets tracker** (route
  writes through rebar; `git stash` there is banned outright because the stash stack is
  repo-global and shared by every worktree). When rebar itself cannot write the store, use the
  supported door `rebar tracker-maintenance` (backup ref before the first write, refusal on
  unpushed ticket commits, durable audit) and its `--force=<reason>` break-glass →
  `docs/concurrency.md` §"Mutating the tracker: no AD-HOC raw git".
- **Session logs** — the `session_log` type semantics and the `session-log` helper +
  auto-rotation → `docs/event-schema.md` and `docs/user-guide.md`.
- **LLM agent operations** — `review-plan`, `verify-completion`, `review-code`, `scan-spec` (the
  optional `[agents]` framework; the old single-pass `review` verb is deprecated and now forwards
  to `review-plan`) → `docs/llm-framework.md`.
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

A sibling gate ratchets **per-function complexity** the same way. `make lint` runs
`python scripts/check_complexity_baseline.py --check`, a **shrink-only** ratchet that freezes
each symbol's McCabe cyclomatic-complexity ceiling in `.github/complexity-baseline.json` and
**fails the build on any `new>0` or `increased>0`** — a newly-complex function, or an existing
one that got *more* complex, breaks CI (an unchanged or *lower* score is fine; a `stale` entry
that dropped below its ceiling is an allowed improvement). The per-branch threshold is
single-sourced as `max-complexity = 15` in `pyproject.toml`'s `[tool.ruff.lint.mccabe]`.
Adding CLI flags, guards, or branches to an already-near-ceiling function predictably trips it.
The fix is **behavior-preserving extraction** — pull a cluster of branches into a helper (along
existing call-graph seams, exactly as for the LOC cap) so each function scores under its
ceiling — **never** bumping the baseline: `--lock`/`--update-stale` are maintenance-only, not a
contributor's escape hatch. Read a function's current score with
`ruff check --select C901 <file>`, and check the whole ratchet with
`python scripts/check_complexity_baseline.py --check`.

## Navigating the codebase (when editing rebar itself)

This checkout has the **Serena** MCP server configured (LSP-backed, Pyright over `src/rebar`)
for *semantic* navigation — `find_symbol`, `find_referencing_symbols`, `get_symbols_overview`,
and symbol-precise edits (`replace_symbol_body`, `insert_after_symbol`). Serena and `grep` fail
in **opposite** directions, so this is a division of labour, not a preference:

| Need | Tool |
|---|---|
| who calls / imports a symbol | Serena `find_referencing_symbols` — semantic, no comment false positives |
| symbol named as a **string**: `monkeypatch.setattr`, `getattr`, `importlib` | `grep` — the LSP cannot resolve these, so Serena silently omits the site |
| calls on a **receiver** whose static type is `Any` — an unannotated parameter or an explicit `: Any` | `grep` — Pyright cannot bind the attribute, so Serena returns an **empty** result, not an error |
| a current line number | `grep` on the working tree — Serena's numbering is offset and its index can lag edits |

Cross-cutting change → do **both**: Serena for the reference set, then one `grep` for the
symbol's name **as a string**. Skipping that second step is what broke epic `061c` S1. Serena is
asymmetric — a non-**empty** result is trustworthy, an **empty** one is not unless every
**receiver** is typed, so confirm it with `grep` before concluding a symbol is unused. Evidence
and reproductions: `docs/code-navigation.md`; if Serena is absent, `claude mcp get serena`.

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
- **Every commit needs** a `rebar-ticket: <full-canonical-id>` trailer (or a leading
  `<full-canonical-id>:` subject) so CI's
  `Verified` gate accepts it (`rebar explain commit-trailer` for the exact format and accepted
  id forms; `rebar verify-commit-ticket` to check a commit locally), **and** a DCO sign-off.
  Before committing, verify
  `git config user.name` and `git config user.email` are set to **your own real, configured
  git identity** (not a placeholder), then add the sign-off with `git commit -s` — it stamps
  `Signed-off-by: <that name> <that email>`. A machine/operator that runs commits under a
  dedicated automation identity (e.g. a bot account) scopes that identity to its own
  machine-local config, never to this canonical guidance (see `rebar explain review` /
  `CONTRIBUTING.md` §"Sign your work (DCO)" for the full policy). If the `commit-msg` hook
  that stamps the `Change-Id` is missing, install it with **`make hooks`** — the one
  idempotent path, which puts the Gerrit stamper in the slot pre-commit chains to. Never
  download the hook straight onto `$(git rev-parse --git-path hooks/commit-msg)`: hooks
  live in the SHARED common dir, so from a linked worktree that overwrites the pre-commit
  wrapper for every worktree at once and silently breaks Change-Id stamping host-wide
  (bug 84aa).
- **Push for review:** `git push gerrit HEAD:refs/for/main` (the magic ref creates a Gerrit
  change; it does not touch `main`). Iterate on findings with `git commit --amend --no-edit`
  (keep the `Change-Id`) + re-push.
- **A change with known issues goes in WIP, not in a comment.** `…refs/for/main%wip` at push
  time, or `POST /a/changes/<n>/wip` on an existing change (`%ready` / `…/ready` to release);
  a hold recorded only on the ticket is advisory, and another session will submit past it →
  [CONTRIBUTING.md](CONTRIBUTING.md) §2d.
- **The gate:** a change is submittable only at **`LLM-Review +1` AND `Verified +1` AND no
  unresolved comments** — only the bots/admins cast either label, so you cannot self-approve.
  A `Verified -1` is a CI failure: open the run and read it. `recheck` only for a **provably
  environmental** fault (see the next bullet).
- **A flaky test is a BUG to root-cause, never a retry.** A test that fails intermittently gets
  debugged — reproduce it deterministically (the `/rebar-debug` workflow: hypothesis-driven, in
  its own worktree, a confirmed root cause before any fix) and fix the **class**, not the one
  run. Retrying until green slows development, wastes tokens, and erodes CI as a regression
  oracle. `recheck` is reserved for provably environmental faults — a TLS/promisor cert flake, a
  missing CI dispatch, a runner outage — and the recheck comment must state that reasoning →
  [CONTRIBUTING.md](CONTRIBUTING.md) §6.
- **Land it yourself with a plain Gerrit Submit** once both votes are green. `main` is
  Rebase-If-Necessary: Gerrit rebases + submits server-side, so you do **not** pre-rebase
  except on a textual conflict it cannot resolve. Do **not** close a ticket until its change
  is `Verified +1` — a passing completion-verifier is **not** a substitute for green CI.
- **Close AFTER the change merges — that ordering is the `Verified +1` policy above, not
  something the close gate enforces.** The close's completion precheck is narrower: when the
  ticket records `file_impact` (and the completion-verification close gate is on), it requires
  a commit reachable from your worktree's **current HEAD** carrying a `rebar-ticket:` trailer
  (or a leading `<id>:` subject) for the ticket **or any transitive descendant** — merge status
  is not checked, so a local unlanded commit satisfies it. If your history lacks that commit (a
  fresh worktree, or a checkout that never had it), it fails with "cannot confirm the work
  landed"; fast-forward first: from inside the worktree run
  `git fetch origin && git merge --ff-only origin/main` (or `git pull --ff-only`). To close a
  stacked story against its own commit while the worktree stays at the epic tip, use
  `rebar transition <id> in_progress closed --ref <sha>` — `--ref` retargets the completion
  verification and signature to that ref's tree; it does **not** narrow the referencing-commit
  scan — see `docs/plan-review-gate.md` §"Which commit the completion gate verifies — `--ref`".
  **NEVER**
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
created = rebar.create_ticket("task", "title", return_alias=True)  # -> {"id","alias"}
ticket = created["alias"]                 # use the alias for subsequent tracker commands
rebar.claim(ticket)                        # uses ticket.default_assignee; ConcurrencyError if taken
rebar.link(child_alias, parent_alias, "discovered_from")
rebar.transition(ticket, "in_progress", "closed")
```

The full library API and the reusable subsystems (signing, LLM runtime, prompt/contract,
output-schema seams) are documented in `docs/reuse-surface.md`.
