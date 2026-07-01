# ADR 0013 — Single `LLM-Review` label + submit requirement as the v1 code-review gate

**Status:** Accepted (epic d251 / story S3)
**Date:** 2026-06-30

## Context

rebar's `main` is gated by Gerrit: a change may only be submitted (and then replicated
to GitHub, S5) when an automated code review passes. We must decide how that gate is
expressed in Gerrit and what carries/blocks the vote.

## Decision

1. **A single custom label `LLM-Review`** (values `-1..+1`) is the only code-review
   vote in v1 — no separate human `Code-Review` and no CI `Verified` vote. The
   deterministic review bot (S4) is the sole voter.

2. **The label is advisory; a submit requirement does the blocking.** The label is
   defined with `function = NoBlock` because Gerrit 3.14.1 *rejects* a blocking label
   function. Blocking is expressed by a submit requirement:
   `submittableIf = label:LLM-Review=MAX AND -has:unresolved`. So a change is
   submittable only when the vote is MAX **and** there are no unresolved comments.
   Note: `label:LLM-Review=MAX` is MaxNoBlock-equivalent — it requires a MAX vote to
   be present but does not treat a MIN (`-1`) as a hard veto. Under the v1 single-voter
   design this is moot (the bot casts one vote per patch set; a `-1` leaves no MAX, so
   the change is non-submittable). If a second authorized voter is ever added and a
   `-1` must always block, the expression needs `… AND -label:LLM-Review=MIN`.

3. **Only Service Users (+ Administrators) may cast `LLM-Review`.** The
   `label-LLM-Review` permission is granted exclusively to the `Service Users` group
   (the bot, wired in S4) and `Administrators`. A regular developer can push a change
   for review but cannot vote the label, so the gate cannot be self-approved.

4. **`copyCondition = changekind:NO_CODE_CHANGE`** — the vote is carried across a true
   no-op re-upload (e.g. a commit-message-only amend) but **not** across a
   `TRIVIAL_REBASE`. A trivial rebase produces a byte-identical diff against a *moved*
   base; copying the vote there would certify a tree the LLM never reviewed against the
   new base. A real rebase therefore drops the vote and forces a fresh review — the
   safe default for a correctness gate.

5. **`change.submitWholeTopic = true`** is set in the **server-level** `gerrit.config`
   `[change]` section (it is a global key, silently ignored in project.config), so a
   whole reviewed feature spanning multiple changes lands atomically. Topics are global
   and un-namespaced across repos; adopt a `rebar-<feature>` naming convention.

## Consequences

- The gate is simple and deterministic: one vote, one requirement, one voter class.
- Evolving to add a human `Code-Review` or a CI `Verified` vote is a follow-on
  (epic 1fa8) — it composes by adding labels/requirements, not by changing this one.
- A rebase always re-triggers review (slightly more LLM cost, maximum safety).
