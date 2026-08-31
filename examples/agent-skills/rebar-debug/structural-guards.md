# Structural guards — deciding whether a class earns one, and designing it to last

This standard governs one decision: after a sibling sweep confirms that a bug is an
*instance of a class*, whether to add a **structural guard** — a single automated check
that fails when a *new* instance of the class appears anywhere — and, when the answer is
yes, how to build one that stays useful. Your skill's own protocol governs the sweep
itself and RED→GREEN integrity; this file is only the guard decision. Load it:

- **rebar-debug** — at Step 7, Axis 1, once the construct sweep has confirmed one or more
  siblings and you are choosing the class-level acceptance criterion.
- **rebar-implement** — when a plan's acceptance criteria propose a class-wide check.

A structural guard is powerful and permanent: every future change pays for it on every
run, and a good one repays that many times over by turning a recurring class into a
one-time fix. The sections below give a bright-line test for when that trade is worth it,
and the properties that keep a guard from decaying into noise.

## Start here: the regression test is usually enough

Most fixes end at the RED→GREEN regression test your protocol already required — that
test *is* the guard for a single-site bug. A structural guard is the **deliberate
exception** you add for a *class* that has shown it can re-enter the codebase from more
than one place. Reach for one only when Section 1 says the class earns it; otherwise the
lighter move in Section 2 is the correct, complete answer. Adding a guard to a bug that
does not need one is its own defect: it spends CI time and future-maintainer attention on
a risk that was never real.

## 1. Does this class earn a guard? (all five → yes)

Work these as positive tests. A guard is warranted only when **every** answer is yes;
each "no" is a reason to choose the lighter move in Section 2 instead.

1. **Binary verdict.** Two competent engineers, reading only the check's pass/fail
   output, will always agree — no interpretation, no "it depends." (Objectivity is the
   line between a guard and a heuristic: *cyclomatic-complexity ≤ N* is a guard; *"keep it
   maintainable"* is not.)

2. **Real, located escape.** This class already reached `main` at least once **at a place
   a structural match would have caught** — you can point to the site the check would have
   flagged. One located escape is enough; you are confirming the detection gap is real and
   mechanical, not counting occurrences.

3. **Re-entry past review.** The violating shape can be reintroduced by an author, bot, or
   agent who would not be looking for it — a different contributor, a code-generator, a
   future you six months on. A rule a human reviewer must remember is a rule that gets
   bypassed; a machine check does not forget.

4. **Contract, not construction.** The property you would encode is a property of the
   design's *contract* (a boundary that must not be crossed, a case that must be handled, a
   call that must precede another), so the check would still pass after a behavior-preserving
   refactor of the fixed code. The bright test: *rename the function you just fixed, extract
   a helper out of it, and the check still passes.*

5. **Nameable boundary.** You can write, just as easily, one example that must fire (the
   defect) **and** one example that must not (legitimate code of the same shape). If the
   "allowed form" is hard to state, the invariant is not yet precise enough to encode —
   sharpen it or choose the lighter move.

## 2. When it does not earn a guard, match the enforcement to the risk

A "no" on any Section-1 question points to a proportionate move, not to doing nothing:

- **One-off, single site** → the RED→GREEN regression test at that site is the whole
  answer. Stop.
- **Real but review already catches it with margin** (single owner, no independent
  re-entry path) → record the invariant where the reviewers read — an ADR note, a
  code-review checklist item, the ticket — rather than in CI.
- **A class you believe is real but cannot yet state objectively** → file a ticket
  capturing the suspected invariant and the escapes seen so far, and let evidence
  accumulate until the boundary is nameable. A guard with a fuzzy boundary fails on
  legitimate code and gets deleted; a ticket keeps the signal without the cost.

## 3. Name the invariant before you pick a tool

State the rule as **operation + property, independent of the fixed site** — the same
artifact Axis 2's "NAME THE RULE" step produces ("repo-root is obtained only through the
validated resolver"; "every union variant is handled at every consumer"; "a mutating
route calls the auth check first"). If you can only state it in terms of the file you
fixed, you have a fix, not yet a class — return to the sweep. The named invariant is what
the check encodes and what its inline intent message will say.

## 4. Choose the cheapest enforcement layer already present

Prefer the layer that expresses the invariant **structurally** (over a syntax tree,
type, import graph, or a derived set) rather than over source text, and prefer a layer
the project already runs. Portable shapes, by the class of bug:

| The invariant is about… | Guard shape | Stack-native examples |
|---|---|---|
| a new variant/case that every consumer must handle | exhaustiveness in the type system (zero-cost, compile-time) | TS `never` check · Rust `match` (+ `#[non_exhaustive]`) · Python `assert_never`/mypy |
| a boundary, layer, or dependency that must/most-not exist | architecture / import rule as a test | ArchUnit (JVM) · dependency-cruiser (JS/TS) · import-linter / Tach (Py) · NetArchTest (.NET) |
| a banned construct, a required idiom, a call that must accompany another | custom AST/static rule | Semgrep (30+ langs) · ESLint `no-restricted-syntax`/custom rule · Ruff restriction rules |
| a cross-service/producer-consumer shape that must stay in sync | contract test (runs on the *other* system's cadence, not feature CI) | Pact / consumer-driven contracts |
| "the complete set of X must be covered", derivable from independent sources | a derived-invariant check: derive the expected set two independent ways and diff | a small script wired into the lint target (see Exemplars) |

When an existing guard already covers a neighbouring case, **extend it** (one more covered
path, one more handled case) rather than adding a parallel mechanism — every new mechanism
is standing CI surface, and one guard that grows with the class is cheaper to keep than
five that overlap.

## 5. Properties of a guard you can keep

Build the check so all of these hold — they are what separate a durable guard from a
check that erodes into noise and gets disabled:

1. **Structural, not textual.** It matches on the tree / type / import graph / derived
   set, so a rename, a reformat, or a reorder never changes whether it fires.
2. **Carries its intent.** The rule ships with a one-line message stating the invariant
   and *why* it matters, next to the pattern — so a future maintainer can tell "still
   relevant" from "obsolete" without archaeology.
3. **Binary, blocking, actionable.** Pass/fail with a `file:line` and the rule name; a
   failure exits non-zero and stops the merge. A check that only trends, or whose
   threshold is set past any realistic violation, is a dashboard, not a guard.
4. **Survives refactoring, fires on contract change.** Of the four kinds of change — pure
   refactor, new feature, bug fix, behaviour change — only the last may turn it red. Verify
   this directly (Section 6).
5. **Pinned by both a true-positive and a true-negative.** The check comes with an example
   that must fire and an example that must not. The true-negative defines the boundary and
   documents the legitimate form, keeping false positives from accumulating.

## 6. Prove the guard the same way you proved the fix

A guard added without evidence is as untrustworthy as a fix added without a RED test.
Confirm, and record the confirmation:

- **True-positive from the defect.** Reuse the mutation artifact — re-introduce the exact
  confirmed root cause and show the guard goes **red** (it would have caught this escape).
- **True-negative.** Show the guard stays **green** on the legitimate form nearest the
  defect (the Section-1.5 "allowed" example).
- **Refactor-survival.** Apply one behavior-preserving change to the fixed code (rename,
  extract) and show the guard stays green. This is the check that it is a guard and not a
  change-detector.
- **No vacuous green.** If the guard works by *deriving* a set and diffing (the last row
  of Section 4), assert the derivation is non-empty — a guard that passes because it found
  nothing to check has failed open. Add a liveness floor that fails when the derived set is
  empty.

## 7. Keep it alive by evolving the rule, not silencing it

When the legitimate design later changes, **change the rule to match the new intent** —
that is the maintenance a guard is supposed to invite, and it is why the invariant is
written down. Reserve suppression (an inline exemption with a stated reason, an allow-list
entry) for a genuine, documented exception, never as the routine way to get to green.
A guard whose exemption list grows faster than its coverage is telling you the invariant
was mis-stated; re-derive it from Section 1.

## Exemplars (in this repo)

- **Derived-invariant + liveness floor:** `scripts/check_deploy_manifest.py` derives the
  set of deploy-relevant paths two independent ways (Dockerfile/compose directives and
  filename conventions), diffs against the autodeploy manifests using autodeploy's own
  prefix semantics, fails on an empty derivation, ships true-positive/true-negative tests
  with per-path mutation teeth, and is wired into `make lint` (portable, no CI-provider
  dependency). This is the "reliably tell what to enforce without a second hand-curated
  list" pattern: derive it, don't curate it.
- **Construct-uniqueness / exhaustiveness contract:** the repo-root resolution guard and
  the `error_code_for` subclass-closure contract test both fail when a new instance of a
  known-wrong construct (or an unhandled subclass) appears, and both pass under renames —
  extend the matching one when your class overlaps rather than adding a new check.
