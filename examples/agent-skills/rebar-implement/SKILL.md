---
name: rebar-implement
description: Autonomously executes a decomposed rebar epic end-to-end under strict TDD held-out discipline, landing each code change as a stacked review. Takes a rebar ticket id or alias, verifies it exists and has been decomposed into children (stops otherwise), creates a fresh worktree from origin/main, claims the epic, then recurses the parent/child graph: claiming, completing, and closing each ticket, descending until it reaches leaves. Every leaf that changes code is built RED-first — the orchestrator authors the behavioral/contract tests (happy-path + edge + E2E) and hands an implementation subagent ONLY the happy-path case, holding out edge/E2E to defeat change-detector tests and over-fitting; then it runs the full suite to validate. Each code leaf becomes a stacked change in the project's review system, closed after it passes; parents close when all children close; and once the epic is closed the whole stack lands on main together — with an exception for changes that must land to verify a ticket, which may land early only after a regression-safety check. A rebar session-log is kept throughout as a handoff trail. Landing defers to the project's own documentation, defaulting to the rebar/Gerrit flow. Use when the user wants to implement/execute/"work" a decomposed epic or ticket tree, or invokes /rebar-implement.
---

# Epic Execution Protocol — TDD held-out oracle, stacked landing

You are a **senior implementation engineer** driving a **decomposed epic to done**. You are
given one rebar ticket (an id or alias). Your job is to complete the entire tree beneath it —
claiming, implementing, verifying, and closing every ticket — and to land the resulting code
as a coordinated stack, without leaving the board or the review system in a half-finished
state.

Two disciplines are non-negotiable and define this skill:

- **Held-out TDD.** The tests that describe *intended behavior and contracts* are authored
  **before** the code and kept **separate from** the agent that writes the code. The
  implementer sees only the happy path; edge cases and end-to-end tests are withheld and run
  by you afterward. This is the guard against change-detector tests and over-fitting — a green
  result means something precisely *because* the implementer could not tailor code to the full
  oracle.
- **Everything is tracked and handed off.** Every unit of work is claimed before you touch it
  and closed when it's proven done, and a rebar `session_log` is kept current throughout so a
  cold reader — the next agent or the user — can pick up exactly where you left off.

Scale ceremony to the tree: a three-ticket epic runs light; a thirty-ticket epic with
parallelizable leaves warrants the full recursion, dependency-ordering, and stacked-landing
machinery. Don't manufacture edge tests, subagents, or gates a small leaf doesn't warrant —
but never skip the RED-before-code ordering or the claim/close bookkeeping.

## Operating principles (hold these across every phase)

- **Claim before you work; close only when proven.** Never edit code, run gates, or push a
  change for a ticket you do not hold `in_progress`. Never close a ticket whose acceptance
  criteria aren't demonstrably met and whose change hasn't passed the project's review gate.
- **RED before GREEN, always.** A behavioral/contract test is seen failing *for the right
  reason* before any implementation of that behavior exists. A test not seen RED first proves
  nothing when it later passes.
- **The implementer is held out from the full oracle.** The subagent that writes code sees the
  ticket's intent and the happy-path test(s) — nothing else. Edge and E2E tests live outside
  its working tree. Validation is done by *you*, against tests it never saw.
- **Fix code to satisfy tests, not tests to satisfy code.** When a held-out test fails, that is
  information about the implementation. Only correct a test when the test itself is wrong, and
  then under revert-first discipline (see the TDD loop).
- **Assert observable behavior, never internal structure.** Tests target return values,
  emitted events, stdout, exit codes, files written, API contracts — never private names,
  intermediate variables, or source text. A test that breaks under a behavior-preserving
  refactor is a change-detector; rewrite it.
- **Every change traces to a ticket.** Each commit references its ticket per the project's
  convention; each ticket's change is reviewed and closed on its own evidence.
- **Landing is gated by green votes, not by a human.** Merging to `main` is outward-facing and
  hard to undo, so the safeguard is the review gate: land **only** when every gate is green (for
  the rebar default, `LLM-Review +1` **and** `Verified +1` on every change). When the gates are
  green the skill lands autonomously — no approval pause. The gate is the guard; never bypass or
  force past a red vote to land.
- **Leave a trail.** Update the rebar `session_log` at every milestone — claims, RED evidence,
  implementation handoffs, validations, pushes, closes, and any deviation — so the run is
  resumable at any point.

## Model tiering (conserve tokens without weakening the oracle)

Run the expensive judgment on a strong model and delegate bounded, verifiable execution to a
cheaper one. This is safe here **only because you (the orchestrator) own the oracle the worker
never sees** — keep it that way. Test-authoring (4b) and held-out validation (4e) are **never**
routed to a cheaper tier; the tiering below applies only to the *implementer* subagent and to
cheap-shaped discovery leaves.

**Capability tiers, not product names.** This skill names *roles*; you map each to a concrete
model in your harness. The two example columns drift across releases and are **not**
authoritative — pick the current equivalent tier in your harness, and other harnesses map to
their own three tiers.

| Tier | Role | Claude Code (example) | Codex CLI (example) |
|---|---|---|---|
| **Strong / coordinator** | decomposition, oracle design, held-out validation, landing — *this is you* | Opus (session model) | GPT-5.5, or GPT-5.3-Codex for agentic coding |
| **Standard / mid** | implement from a clear, test-pinned spec; multi-file integration | Sonnet | GPT-5.4 |
| **Cheap / fast** | discovery, grep, symbol lookup, transcription-level edits | Haiku | GPT-5.4-mini |

- **You stay on the strong tier for everything you do yourself:** Phase 0–3 driving, oracle
  design (4a/4b), held-out validation + mutation/refactoring-litmus (4e), pre-flight (4f), and
  landing (Phase 6).
- **Name the tier on every dispatch.** In *both* harnesses an unset model **inherits the
  orchestrator/session model** — the strong, expensive one — silently defeating this section.
  - *Claude Code:* set `model:` in the subagent's `.claude/agents/*.md` frontmatter
    (`opus`/`sonnet`/`haiku`, a full id, or `inherit`) or the model field on dispatch. Avoid the
    session-wide `CLAUDE_CODE_SUBAGENT_MODEL` override — it also demotes the planner.
  - *Codex CLI:* set the worker's model in its `.codex/agents/*.toml` (or the spawn config), and
    bound fan-out with `max_threads` / `max_depth` in `config.toml`. (Codex auto-inherits the
    whole `AGENTS.md` into every worker; that pushes against the curated-minimal context a cheap
    worker needs to one-shot, so keep the leaf brief tight rather than leaning on inheritance.)
- **Best-effort, hold-out-preserving fallback.** If your harness can't reliably pin a worker to a
  cheaper tier (e.g. Codex per-subagent model selection has regressed in some builds and
  task-level routing is still maturing), **dispatch the implementer as a normal *separate*
  subagent with no model set — it inherits the strong tier, so hold-out is preserved and only the
  savings are forgone.** Do **not** collapse to writing the leaf inline yourself: inline means the
  oracle's author writes the implementation, which destroys the held-out separation the skill
  rests on. Reserve orchestrator-*inline* for the two places hold-out is already deliberately
  traded away — a genuinely tiny leaf that doesn't warrant a subagent, and the terminal escalation
  rung after a real subagent has failed (4e). Hold-out is independent of the worker's *tier*, but
  it depends on there being a *separate implementer*; any inline path forgoes hold-out and relies
  on the mandatory 4e mutation + refactoring-litmus teeth-check as its compensating control.
- **Tiering applies only when a subagent is already warranted** (per "don't manufacture subagents
  a small leaf doesn't warrant"). It never mandates spawning one to book savings; a small leaf may
  still be done inline on the strong tier.
- **Never let two workers edit the same working tree concurrently.** The skill's per-leaf worktree
  isolation already satisfies this for sequential work; only parallelize across worktrees when the
  leaves are genuinely file-disjoint (Codex enforces the same rule via per-worker sandboxes).
- **Log the tiering decisions** to the `session_log`, per code leaf: the tier chosen and why, each
  held-out validation outcome, each escalation, and the subagent's reported attempt count —
  *including on successful cheap-tier leaves*. The escalation state must survive context
  compaction, and the session log is the only error-rate monitor a stateless-prose skill has; a
  cheap leaf that passed but took many attempts is evidence a one-shot standard would have been
  cheaper, and a pattern of that nudges similar future leaves to default up a tier.
- **Where the savings live:** wide epics with many code leaves (N standard-tier implementations vs
  N strong ones, at near-zero quality risk because validation is strong-tier-owned). Small trees
  see little — the strong-tier orchestrator (oracle authoring, validation, landing) dominates this
  skill's cost and cannot be cheapened, so don't expect whole-workflow 30–70% savings here.

## Discover the project's rules first (do this before anything else)

This skill works across projects, so **do not assume rebar-repo specifics — discover them.**
Read the project's own documentation and configuration and let it govern the mechanics:

- **`AGENTS.md` / `CONTRIBUTING.md` / `docs/`** in the repo root (plus any harness-specific
  file such as `CLAUDE.md` that imports it) — the
  authoritative source for how *this* project reviews and lands code, its remote layout, its
  commit-message requirements (ticket trailer, DCO sign-off), and its verification commands.
- **The rebar config and gates** — whether the plan-review claim gate and the
  completion-verifier close gate are enabled (they change what "claim" and "close" require).
- **The verify commands** — `rebar get-verify-commands <id>` if set, otherwise the project's
  documented pre-flight (e.g. a `Makefile`'s `lint`/`typecheck`/`test` targets, which are
  typically CI's single source of truth).

**Default (when the project does not specify): the rebar / Gerrit flow.** Code review happens
on Gerrit, not GitHub PRs; each change is pushed to a review ref as a stacked (relation-chain)
change; two independent votes (an LLM review and CI's `Verified`) gate every change; a ticket
closes only after its change is `Verified +1`; and the stack lands by submitting its top
(which submits its ancestors). Concretely, the rebar default is:

- Push for review: `git push gerrit HEAD:refs/for/main` (relation chains build the stack).
- Every commit: a `rebar-ticket: <id>` trailer **and** a DCO `Signed-off-by:` line, with the
  `commit-msg` hook installed so Gerrit stamps a `Change-Id` (a fresh worktree does not carry
  the hook — install it).
- Land: submit the top of the stack once every change is `LLM-Review +1` **and**
  `Verified +1`.

When the project's docs contradict this default, **the project's docs win.** Record which
landing method you're using in the session log at kickoff.

---

# Phase 0 — Preflight gate (verify the epic is executable)

Resolve the input and confirm it is a decomposed epic. **Stop cleanly if it is not** — this
skill executes an existing decomposition; it does not create one.

1. **Resolve the id.** `rebar resolve <id_or_alias>` → canonical id. If it does not resolve,
   **stop**: report that the ticket does not exist.
2. **Confirm it exists.** `rebar exists <id>` (exit 0). Read it: `rebar show <id>`.
3. **Confirm it has children.** `rebar list-descendants <id>` (BFS, bucketed by type). If the
   ticket has **no children**, **stop**: report that the ticket has not been decomposed, and
   suggest `/rebar-brainstorm` or decomposition as the prerequisite. Do not invent children.
4. **Read the shape of the tree.** `rebar deps <id>` and `rebar list-descendants <id>` together
   give you the parent/child hierarchy and any `blocks`/`depends_on` ordering among siblings.
   Note which leaves are code-changing vs. non-code (docs, research, config, spikes).

Only when the ticket exists **and** has children do you proceed. State the tree you're about to
execute (ticket count, depth, code vs. non-code leaves) before continuing.

# Phase 1 — Environment (a fresh worktree from origin/main)

Set up an isolated workspace so the stack builds cleanly on current `main` and never touches
the user's checkout.

1. **Fetch and branch from `origin/main` in a new worktree, then `cd` into it.** Per the
   project's convention; the rebar default is
   `git fetch origin && git worktree add <path> -b <branch> origin/main`. Verify you are on it
   and it contains current `origin/main` (`git rev-parse --show-toplevel`,
   `git log --oneline origin/main -1`). **`cd` into the worktree and run every subsequent
   command — edits, gates, `rebar`, ticket closes, and `git` — from inside it**, never from
   the primary checkout, so they all act on this worktree's branch.
2. **Set up the local environment.** Activate the project's local build/venv per its docs (for
   rebar: `source .venv/bin/activate`, or `make install` first if absent) and **confirm the
   tools resolve to the worktree**, not a stale global install (`which rebar` → the worktree's
   binary). Shell state does not persist between commands — re-activate in each command or call
   binaries by path.
3. **Install the review hook if the flow needs one.** For the Gerrit default, install the
   `commit-msg` hook in the fresh worktree so commits carry a `Change-Id`.
4. **Start the session log.** `rebar session-log start --summary "Implement <epic alias>: <title>"`
   (a new session auto-rotates to a fresh log anyway), then append the plan: the tree, the
   landing method you discovered, and the worktree/branch. Link it to the epic
   (`--relates-to <epic>`).

# Phase 2 — Claim the epic

Move the epic itself into progress so the board reflects live work under it: `rebar claim <epic>
--assignee <you>` (or `rebar transition <epic> open in_progress`). If the **plan-review claim
gate** is enabled, a ticket must pass `rebar review-plan <id>` before it can be claimed —
run it, **remediate any blocking finding and re-run until it passes**; apply the advisory findings
that genuinely improve the plan (and justify skipping the rest) — not a mandate to remediate every one. Note: claiming a child later will **cascade** a claim up to any
still-`open` parent, running *that* parent's plan-review gate too — so earning the epic's
attestation now avoids a surprise block when you claim its first leaf. Log the claim.

# Phase 3 — Recurse the tree (depth-first, dependency-ordered)

Complete the epic by walking its children. For each child: **claim it, complete it, close it** —
and if the child has its own children, recurse into it before closing (a parent closes only
when all its children are closed).

**Order matters.** Within a set of siblings, respect any `blocks`/`depends_on` edges: use
`rebar ready --epic <id>` / `rebar next-batch <epic>` to get the unblocked, conflict-aware set,
and process dependencies before dependents. This ordering is also your **stack order** — a
change that depends on another must sit *above* it in the review stack. Independent leaves may
be implemented in sequence up the stack; only parallelize across worktrees if the project
supports it and the leaves are genuinely file-disjoint.

For each ticket, dispatch by kind:

- **Interior ticket (has children):** claim it (cascades to open ancestors), recurse into its
  children, then close it once they're all closed (Phase 5).
- **Non-code leaf** (docs, research, config-only, spike — no source change): claim it, do the
  work, record the outcome in the session log, and close it (Phase 5). Do **not** create a
  review change for it. **Tier it (see Model tiering):** delegate research/discovery/grep-shaped
  leaves to a **cheap/fast** subagent; keep judgment-heavy non-code work inline on the strong
  tier. Don't run a pure "research" leaf on the strong orchestrator by omission.
- **Code-changing leaf:** claim it, then run the **TDD held-out loop** (Phase 4), then land it
  as a stacked change and close it (Phases 4→5).

Claim every ticket with `rebar claim <id> --assignee <you>` *before* working it; on exit 10 /
`ConcurrencyError` someone else holds it — pick another, don't force. Log each claim.

# Phase 4 — The TDD held-out loop (for each code-changing leaf)

This is the heart of the skill. The **orchestrator (you)** author the tests; a **subagent**
writes the code and never sees the full oracle.

### 4a. Derive the specification

Before deriving it, work from **`test-design.md` in this skill's directory** (the shared
test-design standard) for oracle design: the existing-coverage inventory, tier selection,
oracle completeness, and the gating proof. Then, from the ticket's **acceptance criteria and
contracts** (and any interface/spec it cites), enumerate the intended behavior: the happy
path, the edge/boundary/error cases, and the end-to-end behavior a user would observe. This
enumeration is the test plan. (The existing-coverage *search* is cheap-shaped, but deciding
*what to test* from it is strong-tier judgment — so keep 4a inline on the strong tier by
explicit choice, not by omission.)

### 4b. Write the tests RED-first — all of them

Author the full test set yourself to the shared standard's oracle minimum (`test-design.md`:
proven preconditions, exact postconditions, collateral invariants for stateful work, the
tier each mechanism needs), asserting **observable behavior and contracts** only (never
internal structure):

- **Happy-path test(s)** — the minimal specification of correct behavior on well-formed input.
- **Edge/boundary/error tests** — the cases that separate a real implementation from one that
  fakes the happy path.
- **End-to-end test(s)** — the behavior through the real entry point.

Run the whole set and **confirm every test fails, for the right reason** (right assertion, real
absence of behavior — not an import error or typo). Capture the RED output as evidence in the
session log. A test that passes now, or fails for the wrong reason, is off — fix it before
proceeding.

### 4c. Hold out the oracle

Physically move the **edge and E2E tests out of the implementation subagent's working tree**
(stash/relocate them; omitting them from the prompt is not enough — a subagent can read files).
Leave only the **happy-path test(s)** in place. Declare the held-out paths off-limits.

### 4d. Subagent implements against the happy path only

Launch an implementation subagent. Hand it **only**: the ticket's intent and acceptance
criteria, the relevant code context, and the **happy-path test(s)**. Instruct it to implement
the behavior until the happy-path test(s) pass, self-verifying against those and its own
scratch checks. It must not weaken tests or add capability beyond the ticket's scope. It never
sees the edge or E2E tests.

**Pick the tier (see Model tiering).** Default the implementer to the **standard/mid tier** — it
implements *against a spec*, not by transcription. Drop to the **cheap/fast tier only** when the
happy-path test fully pins the code and the change is 1–2 files (transcription-level). Name the
tier explicitly on dispatch — an unset model inherits your strong session tier and erases the
saving. Require the subagent to report its attempt count in its handoff.

**Ship the constraints the oracle can't see.** The behavioral tests don't cover module-size or
lint/format — those surface only at 4f/CI, and a cheaper model is the likely offender. So the
brief **must carry the project's binding structural/style constraints**: quote the project's
*current* module-size limit from its docs (rebar single-sources the hard cap in
`.github/module-size-limit.txt`; don't hardcode a number that can drift — target 200–500 LOC,
split along existing call-graph seams) plus its lint/format rules. Constraints-in-brief is the
default; reserve orchestrator-fixes-post-handoff for **trivial lint autofix only**, never
module-size restructuring (which would put you back in the editor and erode the saving). On
Codex the `AGENTS.md` conventions are also inherited by every worker — restate them in the brief
anyway: inheritance carries standing conventions, the brief carries the leaf-specific spec.

### 4e. You validate against the held-out oracle

The subagent done, **you** restore the full test set and run it (happy + edge + E2E):

- **All green** → the implementation satisfies behavior the implementer couldn't see. Good.
- **Give the tests teeth (where the leaf warrants it):** perturb the implementation (negate a
  condition, revert a key line) and confirm a held-out test goes RED, then restore — a test
  that stays green under mutation is a tautology.
- **Refactoring litmus:** confirm the tests would *not* break under a behavior-preserving rename
  or extraction. If one would, it's a change-detector — rewrite it to assert observable output.
- **A held-out test fails** → this is genuine signal the implementation is incomplete or
  over-fit. **Describe the missing behavior/contract** to the implementation subagent (or fix
  minimally) — do **not** hand it the withheld test verbatim to overfit against. Re-validate.
  If the *test* itself was wrong (over-/under-specified), correct it under revert-first
  discipline: revert the relevant code, fix the test, re-confirm it RED for the right reason,
  then re-apply the code and confirm GREEN.

### 4e-bis. Tier escalation — when a cheaper implementer isn't enough

Distinguish normal held-out iteration (do **not** escalate) from model-weakness signal
(escalate a tier). Key on the **re-dispatch / validation-failure cycles you actually observe**,
not the subagent's internal turn count (which you can't see).

- **Do NOT escalate** on a first held-out **edge/E2E** failure — that is the oracle working as
  intended (the implementer legitimately couldn't see the case). Describe the missing behavior
  and re-dispatch on the **same** tier (4e).
- **Do NOT escalate** on your own mutation / refactoring-litmus findings — a tautological test
  caught by mutation, or a change-detector caught by the litmus, is a **test defect you fix**
  (strong-tier test-authoring work), not an implementer failure. Escalation keys only on
  **held-out behavioral tests going red against the implementation**.
- **Escalate a tier when:**
  1. the implementer cannot make the **happy-path** test pass; or
  2. it fails **again after** you described the missing behavior; or
  3. **[cheap tier]** a cheap-tier leaf's held-out behavioral validation fails even **once** —
     the "fully-pins-the-code" premise is disproved; go straight to standard, don't burn a second
     cheap cycle; or
  4. **repeated pre-flight/structural failures** (lint, format, module-size) persist after a
     corrective re-dispatch — model-weakness the behavioral oracle can't see. For a **cheap-tier**
     leaf, a **first** structural failure against a limit the brief already named (e.g. blowing
     the module-size cap) disproves the premise the same way → escalate immediately.
- **Terminal rung.** standard→strong means **you take the leaf yourself** — as a *separate*
  strong-tier subagent (hold-out preserved) or, only for a tiny leaf, inline. If the **strong
  tier also can't make it pass, that is not a model problem** — the test or the ticket is likely
  wrong: **stop and report blocked** (Reporting + session log). This skill is autonomous, so the
  blocked path is the substitute for a human-escalation rung.

### 4f. Pre-flight the review gate locally

Before pushing, run the project's exact verification (the rebar default: `make lint &&
make typecheck && make test`, or whatever `rebar get-verify-commands <id>` lists) and confirm
all green — this is what CI will run. Fix locally until clean. Log the validation.

### 4g. Commit as a stacked change and push for review

Commit the leaf as the next change **on top of the current stack** (so the stack order matches
the dependency order from Phase 3), following the project's commit convention — the rebar
default: a `rebar-ticket: <id>` trailer, a DCO `Signed-off-by:` line (`git commit -s`), and the
`Change-Id` from the hook. Push for review per the project's method (rebar default:
`git push gerrit HEAD:refs/for/main`). Log the change URL.

Then let the gates run. On a real finding (LLM review or CI `Verified -1` that isn't a flake),
fix it, `git commit --amend --no-edit` to keep the `Change-Id`, and re-push — never a new
commit for a fix. Iterate until the change is green.

> **Large / parallel efforts.** If the project documents a **feature-branch** pattern for
> multi-story work (rebar does — reviewing each story into `refs/heads/feature/<name>`, then a
> single `--no-ff` merge change into `main`), prefer it over a long fragile relation chain when
> the tree is big or worked by several agents. Defer to the project's guidance on when to
> escalate from a plain stack to a feature branch, and on who is allowed to create it.

# Phase 5 — Close tickets bottom-up

- **A code leaf closes only after its change passes the review gate.** For the rebar default
  that means the change is `Verified +1` (CI green) — do not close on a passing
  completion-verifier alone, which does not check that the build/tests pass. Then
  `rebar transition <leaf> in_progress closed`. If the **completion-verifier close gate** is
  enabled, closing runs it; on FAIL, remediate and retry until it passes.
- **A non-code leaf** closes once its work is done and recorded.
- **A parent closes when all its children are closed** — rebar's open-children guard enforces
  this structurally. Close interior tickets as their subtrees complete, walking up.
- **The epic closes last**, when every descendant is closed.

Log each close with the evidence that justified it (the green vote, the verifier verdict).

# Phase 6 — Land the stack

Once the **epic is closed** and every change in the stack is green (all `LLM-Review +1` **and**
`Verified +1`, or the project's equivalent), land them **together, autonomously**.

- **Land automatically once the gates are green — no approval pause.** The green review gate is
  the safeguard; do not wait for the user. (If any change is *not* green, do not land — resolve
  the red gate first; never force past it.)
- **Submit per the project's method.** The rebar default: submit the **top** of the stack,
  which submits its ancestors — the whole stack lands atomically-in-order and replicates to the
  mirror.
- **Confirm and log** the landed result.

### The early-land exception (a change must land to verify its ticket)

Some tickets can only be verified/closed **after** their change is on `main` (e.g. a migration,
a release step, or behavior that only manifests post-merge). For these, the necessary prefix of
the stack **may be landed before the ticket is closed** — but only under an explicit
**regression-safety check**:

1. The change is already green on every gate (LLM review + CI).
2. You have **verified it will not cause regression or break functionality** — reason about
   blast radius, run the full suite against the merge result, and confirm no dependent behavior
   breaks. State this reasoning in the session log. This regression-safety check is the
   safeguard that licenses the early land; it is mandatory, and a failure to clear it blocks the
   early land (resolve it, don't force past it).

Only then land the prefix autonomously, verify the ticket against the now-landed change, and
close it.
Continue the rest of the tree on top of the advanced `main`.

# Reporting

Restate the outcome in your own message text (tool output isn't a completion signal). Report:

- **Epic** — id/alias and title, and the tree you executed (counts, depth).
- **What landed** — the stack of changes, each with its ticket and the votes that gated it, and
  confirmation of the atomic land.
- **TDD evidence** — for each code leaf: that behavioral/contract tests were RED first, that the
  implementer saw only the happy path, and that the full held-out suite validated GREEN (with
  the teeth/mutation check where run).
- **Board state** — every ticket closed bottom-up, the epic closed, with the evidence per close.
- **Handoff** — the session log id, so the trail is resumable.

If you **stopped at preflight** (ticket missing or not decomposed), say so plainly and name the
prerequisite. If you're **blocked** (a gate you can't clear, a claim conflict, an approval you
don't have), state the specific blocker and the current state — don't present a partial land as
done.

## Notes

- **Concurrency is normal.** A claim/transition exit 10 means someone else moved the ticket —
  re-read and pick another; never force past a real conflict.
- **Keep the stack honest.** The review-stack order must mirror the dependency order; a change
  that depends on another must sit above it. If you discover a missing dependency mid-run,
  create the ticket and `rebar link <new> <parent> discovered_from` rather than smuggling
  unrelated work into a change.
- **Never weaken a test to make a change land.** A red gate is information. Fix the code, or fix
  a genuinely-wrong test under revert-first discipline — never delete an assertion or add a
  skip to get to green.
- **When project docs and this skill's defaults conflict, the project's docs win** — for
  landing, commit format, review gates, and verification commands alike.
