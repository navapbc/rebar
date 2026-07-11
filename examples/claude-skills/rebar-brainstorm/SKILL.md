---
description: A collaborative brainstorming protocol for designing new agents, skills, tools, or features. Use when the user wants to design/spec something together before building it, or invokes /rebar-brainstorm. Drives a one-question-at-a-time clarification loop, grounded in web research and subagent critique, until the design is ready to implement with high confidence.
---

# Brainstorm Protocol

A structured clarification loop for designing something (an agent, skill, tool, feature, or system) before any implementation begins. The goal is a design that is ready to implement: intent is clear, edge cases are settled, and every decision the build depends on has been made — not to start building.

## Core loop

Ask questions **one at a time** until you fully understand, for each thing being designed:

1. **Purpose** — what it does and why it exists.
2. **Problem it solves** — the concrete pain point, with examples.
3. **Edge cases** — how it should behave when inputs are ambiguous, missing, conflicting, or unusual.
4. **Implementation** — exactly how you would build it, with high confidence.

Do not move on to implementation until all four are clear for every item in scope.

## How to ask

- **One question per turn.** Pick the single highest-leverage unknown and ask only that. Never send more than one question in a message.
- **Ask only what the user alone can answer.** Reserve questions for intent, priorities, preferences, and tradeoff calls — the things only the user knows. Everything factual, discover yourself first from the codebase, docs, git history, and prior art. Never ask the user for something you can find on your own.
- **Ask only what's genuinely unresolved.** Treat everything the user has already told you — directly or in passing — as settled, and build on it. Never re-ask something already answered.
- **Be critical.** Challenge the user's assumptions. If a premise seems flawed, redundant with existing tooling, or likely to fail in practice, say so directly and explain why — before asking the next question.
- **Ground every question and suggestion.** Each substantive question or design suggestion should be backed by at least one of:
  - **Web research** — published research, official docs, blog posts from credible practitioners.
  - **Prior art** — GitHub repos, existing tools, established patterns that solve similar problems.
  - **Subagent convergence** — independent subagent review that agrees with (or sharpened) the reasoning.
- Briefly cite or summarize the grounding when presenting a question or challenge, so the user can evaluate it.

## Research and self-critique (authorized every turn)

- **Web research is authorized on every turn.** Use it to understand the domain, find prior art, and inform question framing. GitHub prior art and published research are high-quality sources.
- **Subagents are authorized to review your thinking.** Before bringing a question, challenge, or design proposal to the user, you may spawn subagents to question your own logic, find holes, or propose alternatives. Prefer this for anything non-obvious.
- Don't present a suggestion that is purely your own intuition when research or critique could strengthen or refute it.

## Interrogate novelty before proposing it

When a candidate solution appears to have **no prior art** — nobody seems to do it
this way, there's no off-the-shelf tool, no standard, no established pattern — treat
that absence as a **signal to investigate, not a green light to invent**. Many smart
people are thinking about the same problem space; if the obvious approach were good,
it would usually already exist. So before proposing a novel approach, establish:

1. **Why is there no prior art?** Distinguish the benign reasons (the niche is too
   small to attract tooling; the need is genuinely new; existing players are locked
   into a constraint we don't have) from the damning ones (people tried it and it
   fails; a hidden cost or gotcha makes it impractical; it conflicts with a
   constraint everyone else treats as non-negotiable).
2. **What do others do instead, and why?** Find the alternatives people actually
   converge on for this problem, and the reason they accept that approach's
   tradeoffs over the one we're considering.
3. **What are the gotchas that keep our approach from being widely used?** Name the
   concrete failure modes — and how we'd avoid or absorb each — before committing.

Novel approaches are not inherently wrong; sometimes the absence really is a gap, or
everyone else is constrained in a way we aren't. But that conclusion has to be
**earned by understanding why the approach is novel**, not assumed. Ground this with
web research and subagent critique like any other question, and bring the finding to
the user explicitly ("this is novel because X; the usual approach is Y; we'd be accepting
gotcha Z") rather than presenting a no-prior-art idea as if novelty were a virtue.

## Understanding gate

Between the dialogue and the completion summary, check your own understanding
**silently** — do not narrate a recap of the conversation back to the user.

List what you are currently treating as true that the user has **not** stated or confirmed.
Each such item is an **intent gap**. A gap is material when getting it wrong would
change the design or the implementation.

- When a material intent gap exists, turn the highest-leverage one into your next
  question and return to the Core loop. The dialogue itself closes the gap; there is
  no separate step.
- Repeat until no material intent gaps remain — every design-shaping assumption then
  traces back to something the user said or confirmed.

Minor defaults that are safe to assume are not gaps. Carry those to the completion
summary for veto rather than spending a question on them.

## Resolve decisions; don't defer them

The output is a design ready to implement, so every decision the build depends on is
in scope **now**.

- Resolve ambiguity in the moment: when a mechanic is unclear, settle it through
  research, prior art, or a question.
- Own the hard parts. Key mechanics are the substance of the design.
- State each decision as a concrete choice with a definite outcome.

Never leave an implementation-critical decision unresolved. Never defer a core
mechanic to a future investigation.

## Exit criteria

The brainstorm is done when the understanding gate passes and you can state, for each item:
- its purpose and the problem it solves,
- its edge-case behavior,
- a concrete implementation plan you'd execute with high confidence, with every
  build-critical decision resolved.

### The completion summary

Before writing any code or files, summarize back to the user for confirmation — but make
the summary **earn its space**. The user already lived through the conversation; do not
replay decisions they explicitly made. The summary's job is to surface **what you
inferred or decided that they did *not* directly confirm**, so they can catch a wrong
assumption before it becomes code.

- **Lead with the unconfirmed.** Foreground the minor defaults you carried past the
  understanding gate: defaults you picked, gaps you filled, call sites/edge cases you
  resolved by reading the code, scope you drew. These are where a silent wrong turn hides.
- **Compress the confirmed.** Decisions the user already answered need at most a one-line
  recap for context (or a reference), not a re-argument. If they chose option A, don't
  re-explain A, B, and C.
- **Flag each inference as vetoable.** State assumptions as "I'm defaulting X (veto if
  wrong)", not as settled fact, so the gaps are obvious and cheap to correct.
- **Plain language, no ceremony.** Short, concrete sentences. Skip restating the
  protocol, the grounding citations, or the journey; the user wants the decisions and the
  open inferences, not a transcript.
- **End with the concrete next action.**

### Before recording tickets, read the project's plan-review gate

When the design will be recorded as tickets (e.g. rebar epics/stories/tasks), the concrete
next action is to CREATE those tickets — and before you do, **read the project's plan-review
gate documentation** so every ticket is authored to pass it. In this project that is
`docs/plan-review-gate.md` and `docs/plan-review-criteria-guide.md`. Shape each ticket to the
gate's **blocking** criteria — cross-section coherence, no unresolved/placeholder decisions,
measurable in-session acceptance criteria, single-concern decomposition, a sound approach with
a stated positive rationale, compat/rollback for any migration, and maintainability/ADR — and
its **overlays** (security trust-boundary, infra endpoint-auth, prior-art, migration-safety,
new-prohibition consumer scan, CI-trigger). Then run the plan-review gate on each ticket
(`rebar review-plan <id>`) and remediate findings before claiming. Reading the gate first is
standard process: it is far cheaper to author to the criteria than to remediate a BLOCK after
the fact.
