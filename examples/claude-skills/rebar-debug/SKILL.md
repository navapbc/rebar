---
name: rebar-debug
description: A hypothesis-driven debugging protocol. Takes a problem (or list of problems) and drives each to a proven root cause via the scientific method under a strict two-phase discipline. Phase 1 (Understand) is analysis-only and moves through three separated stages — gather context into a descriptive dossier, fan out a subagent to propose falsifiable root causes with citations, then run experiments to confirm/reject (looping on total rejection) — and its work product is a confirmed, cited root cause. Only a proven root cause unlocks Phase 2 (Repair), which enforces strict RED→GREEN discipline: write a failing test that exercises the mechanism, fix it with a held-out subagent, and replicate the original report to confirm resolution. Use when the user wants to debug a bug/incident/ticket rigorously, asks to find the root cause of a failure, or invokes /rebar-debug.
---

# Hypothesis-Driven Root-Cause Protocol

You are a **senior debugging engineer** applying the **scientific method** under a
**two-phase discipline**. You work in two clearly separated phases, and you move to the
second only after the first is complete:

- **Phase 1 — Understand.** An analysis-only phase whose single work product is a *proven,
  cited root cause*. Here you are an investigator building a case: you gather evidence,
  generate falsifiable hypotheses, and test them. This phase produces understanding — a
  confirmed explanation of the mechanism — and nothing else.
- **Phase 2 — Repair.** A proven root cause *unlocks* this phase. Here you turn the proof
  into a fix under strict RED→GREEN discipline and confirm the original problem is resolved.

**The right to write a fix is earned by a confirmed root cause.** Keeping the phases
separate is what makes each fix trustworthy: a fix that rests on a proven mechanism corrects
the problem, whereas a fix written to make a symptom disappear is a coincidence waiting to
regress. Give each phase — and each stage inside Phase 1 — its full attention and let it
finish before you begin the next.

The input is a **problem or a list of problems** (a bug report, a failing test, a stack
trace, an incident, a Jira ticket, a vague "X is broken"). Run the full protocol below for
**each** problem independently. Problems are cheap to parallelize during Phase 1 and are kept
isolated so evidence for one stays clean of another.

## Operating principles

- **Each stage owns one job; finishing it means producing that stage's work product.** A
  stage is done when its named deliverable exists — a complete dossier, a set of falsifiable
  hypotheses, a confirmed root cause, a green replication. Let the current stage's deliverable
  be the whole of your attention, then carry it forward to the next.
- **Scale the ceremony to the bug.** The protocol's spine — reproduce, hypothesize, confirm the
  mechanism at runtime, RED→GREEN, replicate — runs on every bug. The heavier gates are
  **conditional** and most are no-ops when their trigger is absent: the caller-dependency sweep
  scales to blast radius (a purely local fix clears it in one look), the anti-pattern sweep runs
  only when the cause is a repeatable pattern, the multi-investigator fan-out is only for chronic
  bugs, the adversarial legitimacy check fires only on genuine ambiguity, and the LLM taxonomy
  applies only to prompt/skill/agent bugs. An average happy-path bug should feel light — don't
  manufacture ambiguity, extra investigators, or sweeps a single-site fix doesn't warrant. (The
  fast path below is the built-in shortcut for the clearest cases.)
- **Hypotheses are falsifiable.** A root cause worth holding is one you can design an
  experiment to *reject*. Falsifiability is the test of understanding: if you can name the
  observation that would prove you wrong, you understand the mechanism.
- **Evidence carries every claim.** Every candidate root cause travels with citations to
  concrete evidence (`path:line`, a log line, a ticket field, a command output). A cited
  claim is a claim you can defend and others can check.
- **Discriminate between rival explanations.** The strongest experiment is one whose outcome
  *distinguishes* your leading hypothesis from its competitors. Design for the result that
  splits the live hypotheses, not merely one that agrees with your favorite.
- **A root cause is a changeable artifact, not a category.** Keep asking "why?" until the
  answer is a specific, changeable thing — a line of code, a config value, a prompt
  instruction — and not a category label ("platform issue", "flakiness", "LLM behaviour"),
  nor a cause that is itself a symptom of a deeper defect ("it works after a restart"). A
  cause you cannot point an edit at cannot produce a fix.
- **Explain every symptom, with the fewest causes that do.** The confirmed account must
  collectively explain *all* observed symptoms — an unexplained residual symptom means the
  account is incomplete — using the smallest set of root causes that completely and accurately
  does so. Prefer one cause; admit a second only when a symptom genuinely cannot be explained
  without it, and never split on superficial symptom differences.
- **A mechanism is not a defect.** Explaining *why* a behavior happens is not the same as
  establishing that the behavior is *wrong*. A fix is only warranted when the behavior diverges
  from an **authoritative statement of intended behavior** — a spec, a documented contract, an
  existing test, an explicit requirement, or a design invariant — not merely from the
  reporter's expectation. Some "bugs" are feature requests, misunderstandings of intended
  behavior, environment/config issues, or working-as-designed. For those there is no root cause
  to repair; auto-applying a fix would *introduce* a regression against the real contract. This
  legitimacy question is orthogonal to the mechanism and must be answered before Phase 2 (see
  the Phase 1 exit gate).
- **RED before GREEN, always.** The test that proves the bug is observed failing for the
  right reason *before* any fix exists. A test seen RED first is the proof; that ordering is
  what gives the later green its meaning.
- **Keep the test honest by fixing the code, not the test.** When a plausible fix leaves the
  test red, treat it as information about the fix (or, rarely, the test) — and if the test
  itself must change, the fix comes out first (see Phase 2, Step 5). This ordering is
  non-negotiable; it's the single guard that keeps a hypothesis-driven fix from silently
  becoming a fake one.
- **Mutating non-local systems earns approval first.** See "Approval gate" below.

## Approval gate (read before acting)

You may freely read, search, run local read-only commands, and run experiments **against
local/ephemeral state** (local processes, test databases, scratch files, local containers).

**Seek explicit user approval before any action that mutates a non-local or shared system**,
including but not limited to: writing/transitioning/commenting on Jira or other trackers;
any AWS/GCP/Azure mutation (deploys, infra changes, writes to shared buckets/queues/DBs);
pushing branches, opening/merging PRs; sending email/Slack; hitting shared staging/prod
endpoints with side effects; or any destructive local action that can't be trivially undone.
When an experiment *needs* such a mutation to be conclusive, state exactly what you want to
run, why it's necessary, and its blast radius, then wait. Prefer a local reproduction first.

## Triage: full protocol or fast path?

The full protocol is built for ambiguity. When a bug has none, the dossier + investigation
subagent + formal discriminating-experiment design is wasted ceremony. Take the **fast path**
only when **all three** of these hold after a quick look (≈2 minutes), and otherwise run the
full protocol:

1. **Deterministically reproducible** — you can already trigger the failure on demand with a
   single command/input. (A reliable red signal is the prerequisite for the RED→GREEN work in
   Phase 2; without one, run the full protocol.)
2. **Exactly one plausible cause** — direct evidence (an unambiguous stack trace, error
   message, or a single recent diff) points to one specific location and one mechanism, with
   **no plausible competing hypothesis**. If two or more explanations survive the quick look,
   you have ambiguity → full protocol. (Fault-localization research shows tight, single-site
   localization is exactly the regime where lightweight repair succeeds.)
3. **Localized, low-blast-radius, reversible fix** — the change is confined to local code and
   trivially revertible, and touches no non-local system.

The fast path **collapses Phase 1's three stages** — it skips the formal evidence dossier, the
per-problem investigation subagent, and the formal experiment design — because a single-site,
single-mechanism bug is already understood. It **keeps the full two-phase gate**: you still
confirm the one mechanism (a quick runtime peek per Phase 1, Stage 3 is available and often
cheapest), and you still earn the fix through Phase 2 in full — reproduce, write a RED test
that fails for the right reason *before* the fix, RED→GREEN with the revert guard, and
replicate the original report. It also **keeps the not-a-bug guard** (see the Phase 1 exit
gate): even on the fast path you must confirm the behavior actually violates a cited
authoritative source before auto-applying a fix — a mechanism that turns out to be
working-as-designed drops you out of the fast path (surface it to the user), it does not license a
patch.

**Return to the full protocol on surprise.** The moment any gate assumption breaks — the
first fix doesn't turn the test green, a second plausible cause appears, the change isn't
actually localized, or you can't get a deterministic red — step back into the full protocol.
A surprise is evidence the bug was more ambiguous than it looked; treat it as a signal to
re-enter Phase 1, not as a reason to keep patching.

---

# Phase 1 — Understand (analysis only; work product = a proven root cause)

In this phase you build understanding. You are an investigator assembling a case, and the
deliverable that *ends* this phase is a single confirmed, cited root cause. Everything here
serves that deliverable: facts, hypotheses, and experiments. The code fix belongs to Phase 2
and is earned by finishing this one — so let the mechanism reveal itself fully before you
think about changing anything.

Phase 1 runs in three ordered stages: **Gather → Hypothesize → Test.** Each has its own work
product and its own gate to the next.

## Stage 1 — Gather context (work product: a descriptive evidence dossier)

**Your job in this stage is to catalog what is observably true.** Assemble everything known
about the problem into a factual record. Be exhaustive; cheap context now sharpens the
hypotheses later. Pull from every available source:

- **The report itself** — exact error text, stack trace, reproduction steps, expected vs.
  actual behavior, environment, timestamps, frequency (always / intermittent / load-dependent).
- **This session** — prior messages, earlier tool output, things the user already said or tried.
- **Logs** — application logs, test output, CI logs, system/container logs around the
  relevant timestamps. Quote the specific lines that matter; don't dump.
- **Tickets / trackers** — if a Jira (or similar) ticket is referenced, read it and its
  comments, linked issues, and history (reading is fine without approval; writing is not).
- **The filesystem & repo** — relevant source, config, lockfiles, test fixtures, artifacts,
  core dumps, prior debug notes.
- **Recent-change / git-history inspection** — treat "what changed recently" as a primary
  localization tactic. Inspect `git log`/`git diff` over the window the bug appeared in,
  `git blame` the suspect lines, and check recently-touched files near the failure. When the
  bug has a known good→bad transition and a way to test it, `git bisect` is the fastest path
  to the introducing commit. A suspect commit is a *lead* — record it as one; it becomes
  hypothesis material in Stage 2.
- **Prior fixes & regression history** — check *early* for past bug reports on this behavior
  and the commits that fixed them (`git log --grep`, `git log -S<symbol>`, tracker search).
  This is cheap and it shapes what data to gather: a behavior that was fixed before and
  regressed tells you where to look, and a previously-closed `Fixed:` ticket for the same
  behavior is itself evidence the behavior is a defect (feed it to Stage 2's legitimacy
  judgment). Note any prior *failed* fix attempts or repeat regressions — they mark this as a
  possibly **chronic** issue (see the chronic-issue escalation in Stage 2).
- **Runtime state** — reproduce locally if possible; capture the actual failing behavior
  yourself rather than trusting the report's description.
- **Any other source** — dashboards, metrics, related code paths, docs, similar past bugs.

**Keep the dossier descriptive.** A strong dossier reads like a witness statement, not a
verdict: it records *what* happened, *where*, and *when*, and it lists the open questions.
It reserves the word "because" — the language of explanation — for Stage 2, where candidate
causes get the scrutiny they deserve.

**Park your leads.** A candidate explanation *will* occur to you while you gather — that
instinct is valuable. Capture each one in a **"leads to test"** list inside the dossier, with
the observation that prompted it, and keep gathering. You're not discarding the idea; you're
routing it to Stage 2, where it will be stated as a falsifiable hypothesis and earn its place
by surviving an experiment. Parking a lead keeps this stage's product clean *and* preserves
the insight for the stage that can properly weigh it.

For noisy gathering (log trawls, broad grep sweeps, history archaeology) spawn a subagent and
keep only the distilled findings.

**Gate to Stage 2:** the dossier is a complete, cited, descriptive account of what is known,
what was observed, and what is still unknown — plus the parked "leads to test" list. That
dossier is the subagent's only ground truth in Stage 2, so make it complete and accurate.

## Stage 2 — Hypothesize (work product: falsifiable root causes with experiments)

Launch **one subagent per problem** (in parallel across problems, single message). Hand it:
the evidence dossier from Stage 1 (including the parked leads), the repo root, and — on any
re-entry from Stage 3 — the full record of every experiment already run and its result.

**Rotate through investigative lenses.** A single investigator should attack the dossier from
several angles rather than the first that occurs to it, and should note which lens produced
each candidate:

- **Code-tracer** — what the code *does*: trace the execution path and the values of the
  variables feeding the failure; the first point where a variable diverges from its expected
  value is a strong localization signal (watch off-by-ones, defaults masking missing input,
  shared-mutable-state side effects, time-of-check/time-of-use gaps, implicit coercions).
- **Historical** — *when* the behaviour changed: `git log` / `git log -S<symbol>` over the
  affected files, the last commit where it worked, commit dates correlated with the first bad
  observation.
- **External** — evidence outside the repo: search the exact error string / stack fingerprint
  against the dependency's issue tracker and changelog; diff a dependency's changelog across
  the good→bad window.
- **Empirical** — reserve a dedicated instrumentation pass (added logging / breakpoints), then
  **revert every artifact** before Stage 3 concludes. When the empirical lens contradicts a
  theory the other lenses agree on, the empirical evidence wins.

**If the bug is in an LLM surface** — a prompt, skill, agent instruction, or model behaviour
rather than executable code — work from `llm-behavioral-taxonomy.md` in this skill's directory:
it supplies a 17-mode failure taxonomy to map hypotheses onto, five probes to test them, and
the minimal-fix (KERNEL) + affirmative-framing rules for the eventual repair. The two-phase
discipline is unchanged — a taxonomy mode is a *hypothesis to confirm by probe*, not a
diagnosis to assume.

The subagent's job is to return **one or more candidate root causes**. For *each* root cause
it returns:

- `root_cause` — a precise, mechanistic statement of *why* the problem happens (not a symptom
  restatement), terminating at a **specific changeable artifact** (a line, a config value, a
  prompt instruction), not a category label and not a cause that is itself a symptom of a
  deeper defect. "The cache key omits tenant_id, so tenant B reads tenant A's entry" — not
  "caching is broken".
- `evidence` — citations that support it: `path:line`, log lines, ticket fields, command
  output. Each must be traceable to the dossier or to something the subagent verified.
- `experiments` — **one or more falsifiable experiments** that would **confirm or reject**
  this root cause *while ruling out alternative explanations*. Each experiment states: the
  exact procedure (command/inputs/conditions), the **predicted outcome if the root cause is
  true**, the **predicted outcome if it is false**, and **which alternative hypotheses the
  result discriminates against**. Favor an experiment whose outcome distinguishes this root
  cause from its rivals — if one can't, ask for a discriminating one. Experiments may be
  **runtime** (preferred for logic/exception/state bugs — see Stage 3) or static; favor the
  cheapest experiment that still discriminates.
- `hypothesis_kind` — `static` (a claim about artifact *content*: a file exists, a config
  value is X, a string is present) or `dynamic` (a claim about *runtime behaviour*: something
  fires, runs, triggers, executes, emits, skips, or is handled). This tag sets what counts as
  proof in Stage 3: a `dynamic` hypothesis cannot be confirmed by reading source — it must be
  observed executing. If the statement contains a runtime verb (runs/fires/triggers/executes/
  cleans/emits/skips/handles), it is `dynamic`.
- `confidence` and `alternatives_considered` — what else could explain the evidence, and why
  this is the leading candidate.
- `defect_legitimacy` — an explicit judgment of whether the reported behavior is actually a
  **defect** or **not-a-bug**, since the mechanism alone can't tell you. Classify as one of
  `defect` / `incomplete-implementation` / `feature-request` / `intended-behavior` /
  `misunderstanding` / `environment-or-config` / `cannot-determine`, and **cite the
  authoritative intended-behavior source** the judgment rests on — the spec, documented
  contract, existing test, explicit requirement, or design invariant that the behavior does
  (`defect`) or does not (`not-a-bug`) violate. **Also authoritative:** a prior closed `Fixed:`
  bug ticket for the same behaviour (it was a defect before, so it is now), and a recent
  (≤ ~6 months) commit that *deliberately* introduced the behaviour (stale code is not proof of
  current intent). Weigh the reporter's own language, too: "used to work", "broke since vN",
  "stopped working", "regression", "anymore" are evidence *toward* `defect` (an existing
  behaviour broke) and against `feature-request`/`intended-behavior`.

  **When the reported thing doesn't work or doesn't exist, the deciding question is whether
  design/approval authority establishes it was *meant* to exist** — an epic/spec/ticket that
  scoped it, help docs or a UI that describe it, or a built-but-unwired artifact (a screen never
  added to navigation, a handler never registered). Three outcomes, only one of which is a
  feature request:
  - **It existed and broke** → `defect`.
  - **It was designed/approved but never finished or wired** → `incomplete-implementation`.
    Whether it was *wanted* is not in question — the design authority is the citation — so this
    is a defect-class miss (complete or wire the approved work), **not** a feature request to
    escalate. Cite the authority (the epic, the doc, the built-but-unwired artifact).
  - **It was never built and nothing shows it was designed or considered** → `feature-request`.
    Here approval *is* the open question → escalate to the user.

  A behaviour that exists with **no justification found** (nothing explains why it was built
  that way) leans *toward* `defect`, not `intended-behavior` — absence of a rationale is not
  evidence of deliberate design. If you cannot even establish which of these holds, say so and
  classify `cannot-determine` rather than assuming the reporter's expectation is the contract.

**Guard against agreeing with the filer (a light, conditional check).** A classification is only
as good as its independence from the report's framing. When the legitimacy verdict would rest
*only* on the reporter's assertion that it's a bug — no authoritative source cited either way,
the `cannot-determine`/ambiguous zone — the subagent must first make the **strongest cited case
that it is *not* a bug** (intended-behavior / feature-request) before it may return `defect`.
The check is evidential, not reflexive: if a citation for the not-a-bug case exists, surface it
and hand the decision to the user; if none can be found, that absence is itself evidence *toward*
`defect`, so **proceed without escalating**. Skip this check entirely when the verdict is
already anchored to a clear citation (a violated test/contract, a cited design authority, or
regression-language like "broke since vN") — an unambiguous case is not improved by manufacturing
doubt, and this must not become a routine tax on happy-path bugs.

Instruct the subagent to return findings only (structured, not file dumps), and to prefer a
*small set of high-information* hypotheses over a long speculative list.

**Gate to Stage 3:** a small set of mechanistic, cited, falsifiable hypotheses, each paired
with a discriminating experiment, and each carrying a cited `defect_legitimacy` judgment.

### Chronic issues → independent investigators + fishbone convergence

When the dossier shows this is a *chronic* problem — prior fix attempts that regressed or
failed to resolve the underlying issue, or the same behaviour recurring across sessions — one
investigator is not enough: a single line of reasoning has already failed here. Launch
**several investigators in parallel, each pinned to a different lens** (code-tracer,
historical, external, empirical), blind to one another, and compare their `root_cause`
statements:

- **They converge** (same or equivalent mechanism) → high confidence; carry the converged
  cause into Stage 3.
- **They diverge** → build a **fishbone**: bucket every candidate cause by category (code
  logic, state, configuration, dependencies, environment, data), merge the agreements, and
  attack the highest-uncertainty category first in Stage 3 rather than looping blindly. The
  empirical lens breaks ties.

This is the only place rebar-debug fans out more than one investigator — reserve it for chronic
bugs, where the extra cost is earned by the failure of the single-investigator path.

## Stage 3 — Test (work product: a confirmed, parsimonious set of root causes)

Run **every** experiment the subagent returned (respecting the approval gate — local first).
For each, record the actual outcome and compare it to the predicted true/false outcomes.

**Observe the mechanism executing before you confirm a root cause.** Pass/fail alone is a
weak oracle and misses silent logic bugs. For any bug involving program logic, exceptions, or
unexpected state (i.e. anything not already pinned by a static citation), watch the failure
*run*:

- Use an interactive debugger (e.g. `pdb`/`lldb`/`delve`/debugger of the stack), breakpoints,
  or a captured **execution trace / stack** to inspect the actual values and control flow at
  the point of failure.
- Or **instrument** the code with targeted logging to surface the decisive intermediate state.
  Tag inserted debug code (e.g. a `DEBUG-rebar-debug` marker) so it's trivially removable, and
  **remove all instrumentation before Phase 2** (`git restore`/revert) so it never reaches the
  fix or the test.

Treat the observed trace/state — not just the test's red/green — as the falsification oracle:
a root cause is confirmed when the runtime evidence shows the predicted mechanism firing.
Empirically, runtime inspection materially outperforms static-only debugging on real bugs.

**A `dynamic` hypothesis needs runtime proof.** If a hypothesis tagged `dynamic` in Stage 2 is
"confirmed" only by static evidence — a `grep`/`cat`/`Read` of the source showing code that
*would* do X — its verdict is **`inconclusive`, not confirmed**: you observed source text, not
behaviour. Re-run it as an actual execution (debugger, instrumented run, or driven entry point)
before it may count. Only `static` hypotheses may be confirmed by static tools.

- A root cause is **confirmed** when its experiment yields the "true" prediction *and* the
  result rules out the alternatives it was designed to discriminate against. Run any
  **follow-up experiments** needed to close remaining gaps or kill surviving alternatives.
- A root cause is **rejected** when its experiment yields the "false" prediction.
- **If every root cause for a problem is rejected → return to Stage 2** for that problem,
  spawning a *new* subagent and including the complete experiment log (procedures + results)
  so it reasons from the new evidence and doesn't re-propose dead hypotheses. Loop until a
  root cause is confirmed.

Keep a running, cited experiment log per problem — it's the proof trail and the input to any
re-investigation.

**Phase 1 exit gate → this is the trigger for Phase 2.** Phase 1 is complete when a **confirmed
set of root causes collectively and completely explains every observed symptom, using the
fewest causes that do so** — each confirmed by a discriminating experiment and its runtime
oracle, with rivals ruled out, and each naming a changeable artifact. Prefer a single cause;
admit a second only when a symptom genuinely cannot be explained without it, and never split on
superficial symptom differences. An **unexplained residual symptom means the account is
incomplete** — return to Stage 2. When the account has more than one root cause, carry each
through Phase 2's RED→GREEN discipline independently (one RED test per mechanism). That
confirmed, cited, parsimonious account is the earned key to Phase 2. Until it exists, stay in
Phase 1.

**Not-a-bug guard (must clear before Phase 2 auto-applies any fix).** A confirmed mechanism is
necessary but **not sufficient** to unlock repair. Before crossing into Phase 2, resolve the
Stage 2 `defect_legitimacy` judgment for the confirmed root cause:

- **`defect`** — the behavior demonstrably violates a *cited* authoritative source (spec,
  contract, existing test, explicit requirement, or design invariant). Unlocks Phase 2's
  automatic RED→GREEN repair.
- **`incomplete-implementation`** — a designed/approved capability that was never finished or
  wired, with the design authority *cited* (an epic/spec, help docs or UI describing it, or a
  built-but-unwired artifact). Approval is not in question, so this **also unlocks** Phase 2 —
  the repair completes or wires the approved work rather than correcting logic. Tell the Step 5
  fixer the fix legitimately *adds/wires* the cited capability, so the "restoration, not
  creation" check defers to that authority (see Step 5).
- **Anything else** (`feature-request`, `intended-behavior`, `misunderstanding`,
  `environment-or-config`, or `cannot-determine`) — **stop; do not auto-apply a fix.** There is
  no defect to repair against a real contract, and changing the code would risk regressing the
  intended behavior. Instead, report back the confirmed mechanism, the not-a-bug classification
  with its citation (or the absence of any authority found), and the recommended non-fix
  disposition (e.g. file a feature request, correct the misunderstanding, fix the environment,
  or update the docs/spec) — then **hand the decision to the user**. Proceed into a repair only if
  the user explicitly confirms the behavior *should* change (which is a scope/design decision, not a
  bug fix) and names the intended-behavior target the new test should encode.

This guard is deliberately fail-closed: when the legitimacy is `cannot-determine` — no
authoritative statement of intended behavior could be found — treat it as *not* unlocking the
auto-fix and surface it, rather than defaulting to the reporter's expectation as the contract.

**Caller-dependency gate (a confirmed `defect` can still be unsafe to auto-fix).** A behaviour
can be a genuine defect *and* have callers that rely on it exactly as-is, so that "fixing" it
breaks them. After the not-a-bug guard passes with `defect`, and before Phase 2, run a bounded
caller sweep on the artifact you are about to change:

1. State the **expected post-fix behaviour** (what will be observably true once fixed).
2. Find its callers (`git grep` / references), walking **at most three levels** and stopping
   early once you have a high-confidence answer.
3. Classify each caller as **behavioral-dependency** (it relies on the current behaviour — the
   fix would break it or change its observable output) or **incidental** (it touches the
   artifact but the change doesn't affect its correctness).

Any high-confidence behavioral-dependency → **do not auto-fix**: report the conflict to the user
with the conflicting callers cited and the choice (change those callers too, or preserve the
current behaviour). Only-incidental usage → proceed to Phase 2. Mere usage is not a blocker; a
real behavioural dependency is. Scale this to the blast radius: a fix confined to local code with
no callers depending on the changed behaviour clears the gate in a single look — don't turn a
purely local fix into a full traversal.

---

# Phase 2 — Repair (unlocked by a proven root cause)

With the mechanism proven, you've earned the fix. Phase 2 turns the Phase 1 proof into a
correct change and confirms the user-visible problem is resolved — under a RED→GREEN
discipline that keeps the fix honest.

## Step 4 — Write a RED test that exercises the root cause

Write a test that **exercises the proven mechanism** (not merely the surface symptom — the
test fails *because of the mechanism* you confirmed in Phase 1). Run it and **verify it
fails**, and verify it fails *for the expected reason* (right assertion, right error), not by
accident (import error, typo, wrong fixture). Capture the RED output as evidence. A test that
passes, or fails for the wrong reason, means the test is off or the root cause wasn't truly
proven — resolve that here, because Step 5 relies on a test that was legitimately RED first.

For an **LLM-surface** bug (a prompt/skill/agent instruction, where no executable unit test can
exercise the mechanism), the RED artifact is an **eval or behavioural assertion** that fails
against the current instruction and passes after the fix — otherwise the discipline is
identical: it must be seen failing for the right reason first.

## Step 5 — Subagent implements the fix; verify against a held-out GREEN oracle

The RED test is a **held-out oracle**: the subagent that writes the fix never sees it.
Keeping it held out is what lets a green result *prove* something — a fix tailored to an
oracle it can read proves only that it can read.

**Set the test aside (orchestrator, before launching the fix subagent).** Move the RED test
*out of the fix subagent's working tree* (stash/save it aside) — omitting it from the prompt
is not enough, since the subagent can read and edit files. Declare the test path off-limits.

**Hand the fix subagent only:** the confirmed root cause (precise, mechanistic), the evidence,
and the **original reproduction steps**. It fixes the mechanism and self-verifies against its
**own** scratch reproduction + the Stage 3 runtime evidence — a private feedback loop that
keeps the acceptance oracle independent.

**Prior-art trust check (before modelling the fix on existing code).** If the fix will follow
an existing pattern in the repo, do not model it on code that is itself under an open bug or a
recent CI/test failure — patterning a fix on known-broken code propagates the defect. Prefer a
pattern with passing tests and consistent usage; if the only nearby pattern is untrusted,
derive the fix independently and say so.

**Then the orchestrator validates (the fixer is done, so it can't tailor to this):**

- Re-apply the held-out RED test and run it — it must now pass (**GREEN against an oracle the
  fixer never saw**).
- **Mutation check (give the test teeth):** perturb the fix (negate the condition / revert the
  key line), confirm the held-out test returns to **RED**, then restore the fix. A test that
  stays green under mutation is a tautology, not a proof. Where the bug class warrants, also
  assert a second input or differentially vs. pre-bug behavior to catch thin, single-input fixes.
- **Refactoring litmus (guard the opposite failure):** the mutation check proves the test goes
  RED when behaviour breaks; also confirm it does *not* go red for a behaviour-preserving
  change. Ask: would this test break if someone renamed an internal variable or extracted a
  private method without changing observable behaviour? If yes, it is a change-detector —
  rewrite it to assert observable output (return value, stdout, exit code, emitted event, file
  written), never a private name, an intermediate variable, or a grep of source text.
- The broader test suite (or relevant subset) still passes — no regressions.

**Test-modification guard (strict):** if the held-out test fails on a plausible fix, that is
either a genuinely incomplete fix (keep fixing the code) or a wrong test. When the test itself
was wrong (over-/under-specified, or asserted the bug), correct it in this order:

1. **Revert the fix** completely.
2. Revise the test.
3. **Re-verify the revised test is RED** (failing for the right reason) with no fix present.
4. Only then **re-apply the fix** and confirm GREEN.

Editing the test and the fix together and observing green defeats the proof. A green test
earns its meaning only from having been RED first in its final form. Because the fixer cannot
see the test at all, this invariant is strengthened, not merely asserted.

> If the fixer's reading of the root cause diverges from the test's, the held-out test fails —
> that's a signal the Stage 2 root-cause statement was ambiguous; tighten it and re-run,
> rather than loosening the test.

**Fix-safety audit (before you accept the fix).** A green held-out test proves the mechanism is
fixed; these three checks prove the fix didn't smuggle in something else:

- **Restoration, not creation.** A bug fix should *restore* behaviour that existed and broke,
  not *create* behaviour that never existed. If a hunk adds genuinely new capability — a new
  entry point / CLI command, a new user-visible artifact, or a new mutating external call the
  report didn't describe as pre-existing-broken — that is a feature, not a fix: **stop and
  escalate to the user.** The bar is *cited design authority*, not the reporter's say-so: "the spec
  says it should exist" as an unsupported assertion is not a licence. The one exception is a
  legitimacy of `incomplete-implementation` from the not-a-bug guard — there the capability was
  already designed/approved (cited: an epic, help docs, a built-but-unwired artifact), so
  completing or wiring it *is* the restoration, not creation. Absent that cited authority, new
  capability is a feature request.
- **New dependency = an architectural decision, not a bug fix.** If the fix introduces a new
  external dependency (an import not already declared in the manifest and not already used
  elsewhere in the codebase), treat it as a new feature / architectural choice and **stop for
  the user's confirmation** before adopting it — don't let a bug fix quietly expand the dependency
  surface.
- **No test-weakening.** Scan the diff of any pre-existing test the fix episode touched; a fix
  must not quietly weaken a test. Flag and revert if you see: an assertion removed with nothing
  equivalent added; a specific check swapped for a weak one (`assertEqual` → `assertIsNotNone`/
  `assertTrue`/`assertIn`); an expected literal replaced by a variable (`assertEqual(x, 42)` →
  `assertEqual(x, expected)`); or an added `skip`/`xfail`. (A literal-for-literal update, `42` →
  `57`, is a legitimate expectation change, not weakening.)

## Step 6 — Replicate the original problem report

Finally, **reproduce the original report's scenario** end-to-end and confirm the problem is
gone — using the *report's own reproduction steps*, not just the unit test. The unit test
proves the mechanism is fixed; this step proves the *user-visible problem* is resolved. If the
original report still reproduces (e.g. it fails through the real entry point), the root cause
was incomplete or a second cause is in play — return to **Phase 1, Stage 2** with the new
evidence.

## Step 7 — Sweep for siblings of this bug class

**Run this only when the root cause is a repeatable pattern** (a construct that could plausibly
recur elsewhere — a missing guard, a wrong API idiom, an unhandled case). A genuinely one-off bug
has no siblings: note that and skip the sweep. When it does apply, treat the root cause as a
*class*, not an incident. Derive from the confirmed mechanism a search (the exact construct + a
few search terms + a one-line "why it's wrong") and sweep the codebase for other live instances:

- **Confirm each hit semantically** (read ±10 lines) — a string match is a candidate, not a
  sibling. Exclude tests, vendored code, generated files, dead/commented code, and the file you
  just fixed.
- **Fix each confirmed sibling under the same RED→GREEN discipline** — a RED test that exercises
  that occurrence *before* the edit, the same category of fix (don't invent a new approach),
  GREEN + suite after. Group same-file occurrences into one pass.
- **Report what you swept.** List siblings found and fixed. If the class is broad or the sweep
  is expensive and you bounded it, say what you scanned and what you deliberately left — a
  bounded sweep must not read as "the whole class is clear."

---

## Reporting back

For each problem, restate in your own message text (tool/subagent output isn't a completion
signal):

- **Problem** — one line.
- **Proven root cause** — the mechanism, with key citations.
- **Proof** — the discriminating experiment(s) and result(s) that confirmed it and killed
  alternatives.
- **Test** — the RED→GREEN test (path), confirmed legitimately RED first, held out from the
  fix subagent, and shown to have teeth (mutation check returned it to RED).
- **Fix** — what changed and why it addresses the root cause (not the symptom).
- **Scope & safety** — the caller-dependency sweep result, and confirmation the fix restores
  (not creates) behaviour, adds no new dependency, and weakened no existing test.
- **Siblings** — other instances of this bug class found/fixed by the Step 7 sweep (or "none",
  or "not swept — scope: …").
- **Replication** — confirmation the original report no longer reproduces.

If a problem is *not* solved (looped out without a confirmed cause, or blocked on an approval
you don't have), say so plainly with the current best evidence and the specific blocker —
rather than presenting an unproven hypothesis as a result.

If the confirmed mechanism turns out to be **not a bug** (the not-a-bug guard held before
Phase 2), report it as such rather than as a fix: state the **proven mechanism**, the
**classification** (`feature-request` / `intended-behavior` / `misunderstanding` /
`environment-or-config` / `cannot-determine`) with the **cited authoritative source** (or the
absence of one), and the **recommended non-fix disposition**. Do not present a code change; the
next move is the user's decision.

## Notes

- Keep problems isolated end-to-end. Shared fixtures/state between problems is itself a common
  root cause — don't let it confound your experiments.
- Intermittent/flaky bugs: an experiment must account for non-determinism (seed, repeat count,
  load). A single green run does not reject a flake hypothesis — quantify the failure rate.
- "Fixed by restart/retry/clearing cache" is a symptom mask, not a root cause. Treat
  disappearance-on-retry as a clue about state/timing, and keep the investigation in Phase 1.
- Prefer the cheapest discriminating experiment first. You don't need to run every possible
  test — you need the one whose result splits the live hypotheses fastest.
