---
schema_version: 1
title: CI gate compatibility
description: Find a new hard-fail pipeline gate that would break branches created before the change.
outputs: code_review_findings
execution_mode: single_turn
category: code-review-pass
dimension: ci-gate-compat
---
You are a Pass-1 finder for the `project.ci-gate-compat` criterion. The change under review
touches **pipeline / gate configuration** — the declarative files or scripts that decide whether
an automated build of a branch passes or fails. Ask exactly one question:

> Does this change introduce a **hard-fail gate** with **no compatibility guard**, so a branch
> created *before* this change — one that has not been rebased onto it — would now fail?

Report only grounded, identifiable cases. Abstain whenever you cannot name all four elements
below in the changed text.

## What counts (definitions — deliberately tool-neutral)

- A **hard-fail gate** is any added or newly-required step whose failure fails the whole build:
  a non-zero exit that is not tolerated, an assertion/threshold/floor/cap check, a newly
  required stage, or a check newly promoted from warn-only to failing. A step that only prints,
  warns, annotates, or is explicitly allowed to fail is **not** a hard-fail gate.
- A gate's **precondition** is what the build must already contain for the gate to pass: a file,
  a config key, a manifest entry, a baseline/ledger record, a tool version, a commit-message
  form, a code property, or a size/complexity budget.
- A **compatibility guard** (equivalently, a **grandfather** clause) is any condition that makes
  the gate skip or soft-pass when its precondition is not yet met: an if-present / file-exists
  test, a change-scope filter that limits the gate to files the change itself touches, an
  effective-date or merge-base cutoff, an opt-in flag, a baseline/allowlist of pre-existing
  violations, or a documented ratchet that only forbids getting *worse*.

## Emit a finding only when all four are present

1. **The cited gate** — the changed step, script line, or setting that fails the build, quoted
   with its location.
2. **The precondition it demands** — what a build must already contain to satisfy it.
3. **The absence of a guard** — no if-present test, scope filter, cutoff, opt-in, or baseline
   in the changed text or its immediate surroundings.
4. **The pre-existing state that fails it** — a concrete branch or commit that predates this
   change, does not satisfy the precondition, and would therefore fail the gate.

State (3) as something you checked, not something you assumed. If the changed text is a
fragment and the guard could plausibly live in the surrounding, unchanged configuration you were
not shown, **abstain**.

## Do not flag

- A gate that only warns, annotates, or is explicitly permitted to fail.
- A gate that already carries a compatibility guard, however it is spelled.
- A change that relaxes, removes, or widens an existing gate.
- A gate whose precondition every pre-existing commit already satisfies (for example, a check
  over files the change scope guarantees are present).
- Documentation, tests, or fixtures that merely describe or exercise a gate.
- A deliberately unguarded gate whose change explains, in the diff, why no grace period applies.

## Output

Use `criteria: ["project.ci-gate-compat"]`. Put the quoted gate in `location`, the gate plus its
unmet precondition in `finding`, the pre-existing-branch breakage in `scenarios`, and the quoted
changed lines you relied on in `evidence`. Leave `suggested_fix` empty — this is an advisory
finder, not a request to design the guard. When in doubt, emit nothing.

## Change under review: {{ticket_id}}

{{ticket_context}}
