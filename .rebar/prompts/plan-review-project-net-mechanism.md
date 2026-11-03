---
schema_version: 1
title: Rebar net mechanism
description: Find plans that add a counted mechanism without naming a retirement or why none is possible.
execution_mode: single_turn
category: plan-review-criterion
dimension: project-invariants
---
You are reviewing a rebar plan for **net mechanism growth**. rebar counts seven kinds of
mechanism and ratchets them shrink-only: a plan may add one, but the project's standing
requirement is that it say what the addition BUYS BACK. The motive is measured, not
stylistic — a majority of sampled fixes add a mechanism where a minority are pure logic
fixes, so each cycle grows the surface that produces the next cycle's defect classes.

Your job is to flag a plan that adds a mechanism of a counted kind while saying nothing
about retirement. You are reviewing the PLAN's own declarations, at claim time, before any
code exists. You have no access to the tree, the ratchet script, or the baseline file, and
you must not speculate about what the implementation might turn out to need.

## The counted kinds

This list is CLOSED. Exactly these seven kinds are counted, with the cue for recognising
each in plan prose:

1. `lock` — a new mutex, file lock, advisory lock, or serialising critical section.
2. `env_var` — a new environment variable the code reads, including a kill switch.
3. `config_key` — a new NON-BOOLEAN settings key the plan introduces.
4. `feature_flag` — a new BOOLEAN settings key, toggle, or opt-in/opt-out switch.
5. `ci_gate` — a new automated check the plan says must pass before a change can land,
   whatever system runs it.
6. `autouse_fixture` — a new test fixture that applies automatically without being
   requested by the test.
7. `test_helper` — a new shared helper, factory, or builder that test modules import.

An element matching NONE of these seven is not this criterion's concern, however complex
it is. General complexity growth belongs to a different criterion; do not reach for it
here. A new function, class, module, script, document, or data file is not a counted kind.

## Detection heuristic

From the plan's stated edit set, extract every element it proposes to ADD that matches a
listed kind, and every element of a listed kind it proposes to REMOVE. Recognise an
addition from the plan's own declarative statements about what it will create — a named
new key, flag, lock, gate, fixture, or helper. Do not infer an addition from a plan's
silence, from a general description of behaviour, or from your own view of what the work
would require in practice.

## Finding threshold

Emit a finding ONLY when ALL FOUR of the following hold. If any one fails, do not emit —
silence is correct, and this criterion is deliberately conjunctive so that an ambiguous
plan passes rather than blocks.

1. `a counted addition` — the plan adds at least one element of a listed kind;
2. `no named retirement` — the plan names no element of any counted kind that it will
   retire, remove, collapse, or subsume;
3. `no stated justification` — the plan carries no explicit statement of why no retirement
   is possible;
4. `a nameable addition` — the addition is specific enough that you can state both its
   KIND and its NAME.

Condition 4 is load-bearing. It stops this criterion manufacturing findings from a plan
that is merely underspecified — a plan too vague to name what it adds has a different
problem, and that problem is another criterion's subject, not yours.

## Required finding fields

Every finding you emit MUST populate exactly these fields, with these types:

- `location: str` — the plan citation: the heading, step number, or quoted phrase in the
  plan where the addition is proposed.
- `finding: str` — the kind and name of the mechanism added, together with the fact that
  the plan names neither a retirement nor a reason none is possible.
- `scenarios: list[str]` — one entry per added kind/name pair, each naming the kind and
  the name.
- `evidence: list[str]` — the verbatim plan text that declares the addition.
- `criteria: list[str]` — a list containing `project.net-mechanism`.

Keep each field tight and load-bearing; do not pad with restatements.

## Non-findings

Some plans look like mechanism growth but are not. Do NOT emit a finding for these:

- `A plan that retires more than it adds` — a net reduction is the outcome this criterion
  exists to encourage, and is never a finding.
- `A plan naming a retirement in a different kind` — the trade need not be like-for-like.
  A plan adding a `config_key` while retiring a `lock` has named a retirement.
- `A plan stating why no retirement is possible` — whatever you think of the reason. This
  criterion checks that the justification is PRESENT, not that it is persuasive. Judging
  persuasiveness would make the criterion irreproducible across runs and reviewers, so do
  not do it.
- `A plan adding no element of a counted kind` — including a plan that adds substantial
  code, documents, or scripts, none of which is a counted kind.
- `A plan adding complexity outside the seven kinds` — out of scope here, as stated above.

When in doubt, prefer silence: emit only when all four threshold conditions are fully and
concretely satisfied.
