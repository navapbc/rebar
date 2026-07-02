# ADR 0020 — Two independent gate votes: add CI `Verified` alongside `LLM-Review`

**Status:** Accepted (epic 1fa8 / story S1). Amends **ADR-0013** (single-label gate).
**Date:** 2026-07-01

## Context

ADR-0013 made a **single** `LLM-Review` label + submit requirement the v1 code-review
gate, and explicitly deferred a CI `Verified` vote to a follow-on (epic 1fa8). That
follow-on is now being implemented: CI (build/test/lint/typecheck) runs on every
patchset via `gerrit-to-platform` → GitHub Actions and must gate submit **independently**
of the LLM review. ADR-0013 anticipated this: "Evolving to add a CI `Verified` vote is a
follow-on — it composes by adding labels/requirements, not by changing this one."

## Decision

1. **Add a second custom label `Verified`** (`-1..+1`, `function = NoBlock`) and a
   second submit requirement `submittableIf = label:Verified=MAX`. It is an
   **independent** vote: LLM-Review is unchanged; CI failing (or absent) cannot affect
   the LLM-Review vote and vice-versa. The effective gate becomes
   `label:LLM-Review=MAX AND label:Verified=MAX AND -has:unresolved`.

2. **Same voter-restriction as LLM-Review.** `label-Verified` is granted only to
   `Service Users` (the gerrit-to-platform CI service account, story S4) +
   `Administrators`; a developer cannot self-cast `Verified` and bypass CI.

3. **Same strict `copyCondition = changekind:NO_CODE_CHANGE`.** A `Verified` vote
   carries only across a true no-op re-upload, never a `TRIVIAL_REBASE` — so any real
   new patchset drops `Verified` and forces a fresh CI run. This is the GerriScary-safe
   reset (CVE-2025-1568): a stale/copied CI vote must never carry onto code CI never ran.
   The GitHub Actions workflow additionally clears `Verified→0` at run start (S5).

4. **Staged rollout — the `Verified` submit requirement ships INACTIVE.** It is
   authored in `project.config` with `applicableIf = is:false`, so the label records CI
   votes without blocking submit. Story S6 activates it (deletes the `applicableIf`
   line) only after the CI voter is proven end-to-end. This closes the window where the
   gate would be enforced but no automated voter exists (which would block all submits).

## Consequences

- The gate is now two independent deterministic votes; either failing blocks submit
  (fail-closed). Both are bot/CI-cast, restricted to Service Users.
- `MaxNoBlock` caveat (from ADR-0013) applies equally: `label:Verified=MAX` requires a
  MAX to be present but does not treat a `-1` as a hard veto. Under the single-CI-voter
  design this is moot (one vote per patchset; a `-1` leaves no MAX ⇒ non-submittable).
  If a second `Verified` voter is ever added, use `… AND -label:Verified=MIN`.
- Supersedes the "single vote / no CI Verified" statements in ADR-0013 §Decision(1) and
  in `docs/gerrit-aws-setup.md` §6. ADR-0013 otherwise stands (the LLM-Review vote is
  unchanged).

## Alternatives considered

- **Fold CI into the LLM-Review vote** (one label): rejected — couples two independent
  concerns; a CI infra flake would corrupt the review verdict, and vice-versa.
- **A human `Code-Review` vote instead of CI**: out of scope; the project is bot-gated.
