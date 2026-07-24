# ADR 0053 — Grandfather plan-review attestations across criteria-registry drift

**Status:** Accepted
**Date:** 2026-07-24
**Amends:** **ADR 0015** (project-supplied criteria) — specifically its `stale-regver`
claim-gate check. ADR 0015's `registry_version` stamp, its overlay-awareness, and its
`disabled: true` built-in mechanism all stand unchanged; only the *consequence* of a stamp
mismatch changes.
**Ticket:** `1f32-f86e-8a51-4d8e` (`flinty-unwhite-wolf`).

## Context

ADR 0015 bound a criteria-registry version stamp (`regver:`) into every signed plan-review
manifest and made the claim gate reject any attestation whose stamp no longer matches the
current registry:

> `compute_validity`'s plan-review branch compares the manifest's signed `regver:` against the
> current `registry_version(repo_root)`; a mismatch — or a **missing** `regver:` line — is
> `{valid: false, verdict: "stale-regver"}`, forcing a fresh `review-plan` before the claim.

The motivating threat was real: a project could activate, re-tune, or disable a criterion and
then keep claiming against a review that never saw the new rule.

The remedy, however, is far broader than the threat. `registry_version` hashes the **entire
effective routing index** — every criterion's free-text `check` string included. It therefore
cannot distinguish a criterion being **tightened** from a **typo fix**, a wording clarification,
or a vocabulary rename. Any edit to `criteria_routing.json` rotates the stamp, and because the
stamp is global rather than per-criterion, one edit invalidates **every outstanding attestation
in the repo at once**.

Measured on this repository while scoping an unrelated vocabulary change (renaming one phrase
inside a single criterion's `check` string):

| | |
|---|---|
| `regver` before | `b058e30f0de6a907` |
| `regver` after a one-phrase edit | `23913c1f6987711b` |
| Tickets holding a plan-review attestation | **632** |
| Of those, open / in_progress (would need re-review to claim) | **~40** |

So a cosmetic edit costs ~40 full LLM re-reviews and stalls every in-flight claim. The
predictable outcome is that **criteria maintenance freezes**: the cheapest way to avoid the
bill is to never touch the registry, which is precisely the opposite of what a living criteria
system needs.

The deeper error is what the check asserts. An attestation is a statement about the past: *this
plan passed the criteria as they stood at signing time.* That statement remains true forever. A
later registry change does not falsify it; it merely means the plan has not been evaluated
against the *newer* criteria. Treating "not yet re-evaluated" as "no longer valid" conflates a
freshness question with a validity question — and unlike `stale-code` or `stale-material`, where
the reviewed artifact itself moved, here **nothing about the plan or the code changed at all**.

## Decision

**Criteria-registry drift is grandfathered at the claim gate.** A `regver` mismatch — or a
missing `regver:` line on an older manifest — no longer produces `{valid: false}` and no longer
blocks a claim.

The drift is still **detected and reported**, not discarded. `compute_validity` records
`registry_drift: {"signed": <stamp>, "current": <stamp>}` on its result, and that field rides
along on every verdict the call returns, so `review-plan --status` and the gate UX can still
tell an operator the plan was reviewed under an older criteria registry.

The `stale-regver` verdict is removed from the classifier's vocabulary.

**Explicitly unchanged — these still block:**

- `stale-code` / `stale-head` — the code the plan was reviewed against drifted.
- `stale-material` — the plan itself was edited since review.
- `stale-reopened` — the attestation predates the latest reopen.
- `unverifiable-material`, `incompatible-phase`, pin-status staleness, and signature
  verification.

**Also unchanged — the reuse path stays conservative.** Registry drift still denies
progressive **drift-refresh** and remediation-mode eligibility: `registry_unchanged` in
`_signature_branch_decision`, `_sidecar_branch_decision`, and `drift_floor`, plus an explicit
`registry_drift` check in `drift_refresh_candidate`. Those decide whether a cheap refresh may
**reuse** a prior verdict versus running a full review — a verdict reached under older criteria
should not be carried forward by a probe. Declining there costs one full re-review; it never
blocks a claim, so ADR 0015's intent is preserved exactly where it is cheap.

Note that `drift_refresh_candidate` previously enforced this *implicitly*, by inheriting the
`stale-regver` verdict from `compute_validity`. Removing that verdict silently opened the reuse
path — caught by `test_drift_refresh_skips_on_registry_skew` — so the check is now explicit.
This is the one place where the two policies genuinely differ and the seam has to be stated.

The signed `regver` continues to be read from the **authenticated** manifest, never its
plaintext mirror. Reporting must not become a channel for unauthenticated data (ADR 0049); the
opcert binding tests continue to pin this.

## Consequences

- **Criteria maintenance becomes affordable.** Wording fixes, clarifications, and vocabulary
  renames no longer cost a repo-wide re-review, so the registry can be kept accurate.
- **In-flight work stops being collateral.** Editing criteria no longer strands every claimed
  and about-to-be-claimed ticket.
- **Accepted cost — a criteria *tightening* no longer applies retroactively.** A plan reviewed
  under a weaker rule stays claimable indefinitely. This is the real thing ADR 0015 was
  protecting, and it is given up deliberately. Two things make it tolerable: the new rule still
  applies to every *future* review, and the drift is visible rather than silent, so an operator
  who tightens a rule can see which attestations predate it and re-review those deliberately.
- **The threat ADR 0015 named is narrowed, not eliminated.** A project that disables a criterion
  can now claim against a review that predates the change. The mitigation is visibility
  (`registry_drift`) plus the fact that the overlay itself is a reviewed, committed artifact —
  not an invisible runtime toggle.

## Future work (not built here)

The right long-term shape is **per-criterion, material-vs-cosmetic** invalidation rather than a
single global stamp: hash each criterion's rule separately, and let a registry edit invalidate
only the attestations whose review actually exercised a criterion whose *semantics* changed —
with an explicit "this is a tightening" marker an author sets when the change is material. That
preserves grandfathering as the default while restoring a targeted escape hatch. It is out of
scope here; this ADR fixes the false-invalidation bug without building that machinery.

## References

- ADR 0015 — project-supplied criteria (the `regver` stamp and the original `stale-regver` check)
- ADR 0049 — opcert asymmetric attestation (authenticated-manifest sourcing)
- `src/rebar/llm/plan_review/attest.py` — `compute_validity`
- `src/rebar/llm/plan_review/manifest.py` — `registry_version`
