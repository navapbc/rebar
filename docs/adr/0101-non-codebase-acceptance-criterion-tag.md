# ADR 0101 — `[non-codebase]` replaces `[operator-attested]` as the acceptance-criterion tag

**Status:** Accepted
**Date:** 2026-08-19  
**Ticket:** `depilatory-bairnly-silverfox` / `d1fb-cdeb-f3cf-4cd9`  
**Epic:** `undamaging-dissimilar-sidewinder` / `6f5a-4a49-7bbf-4a30`  
**Amends:** [ADR 0043](0043-operator-attested-completion-evidence.md) — renames the tag that ADR
established and relaxes its exact-token rule to a two-spelling rule. Everything else in ADR 0043
(the trust model, the classification-from-author-tag design, the laundering hazard) stands
unchanged. See also [ADR 0061](0061-measurement-provenance-in-attested-evidence.md), which amended
ADR 0043's evidence contract and whose `provenance:` requirement is unaffected by this rename.

## Context

ADR 0043 introduced `[operator-attested]`: an acceptance-criterion checkbox tag declaring that the
criterion's "done" evidence inherently lives OUTSIDE the codebase — a deploy, a live drill, a
console setting, a Gerrit vote — so the completion verifier should accept a recorded attestation
on the ticket instead of failing to find code proof.

The tag names the wrong axis. It names **who attests** rather than **where the evidence lives**,
and the word it chose for the actor is read narrowly. Across this repository `operator` means a
*human specifically*: "the operator runbook", "the operator guide", "the operator escape hatch",
"the operator can". The plan-time detector even carries a marker family named `human`
(`det_operator_attested.py`, `_OPERATOR_EVIDENCE_MARKERS`). Agents reading `[operator-attested]`
therefore infer that a person is required — a reasonable inference from the surrounding usage, and
a wrong one: an agent recording a Gerrit vote outcome or a live-run result attests exactly as well
as a human does.

The observed cost is authors declining the tag where it applies, which is the failure mode ADR 0043
was written to prevent (two tickets, 115b and 8c4f, burned close-gate cycles for precisely this
reason before ADR 0043 existed).

## Decision

**The canonical acceptance-criterion tag is `[non-codebase]`.**

It is the missing half of a pair the vocabulary already had. The `[codebase]` token is already
recognized at three sites — `docs/adr/0043-operator-attested-completion-evidence.md:45`,
`src/rebar/llm/reviewers/completion_verifier.md:95`, and
`src/rebar/llm/reviewers/plan_review_evidence_kind.md:56` — where an explicit `[codebase]` tag is
treated, like an untagged criterion, as codebase-verifiable. `[codebase]` is prompt-level only: no
regex parses it. `[non-codebase]` completes that pair with the tag the matcher *does* parse.

Note that the bracket tag `[codebase]` and the criterion-kind category name `codebase-verifiable`
are DIFFERENT vocabularies. `src/rebar/llm/plan_review/criteria_routing.json` uses the category
name and never the bracket tag; this ADR does not conflate them.

**`[operator-attested]` is retained as an accepted but deliberately UNDOCUMENTED alias.** The
matcher accepts either spelling; author-facing guidance teaches only `[non-codebase]`.

**The completion-verdict `kind` value changes in lockstep**, from `operator-attested` to
`non-codebase`, so the deterministic matcher and the LLM's emitted classification cannot diverge.
Readers accept both values; only the emitter changes.

### Why not `[attested]`

`attested` is already a load-bearing configuration value in an adjacent subsystem:
`SOURCE_ATTESTED = "attested"` (`src/rebar/_snapshot/repo_snapshot.py`) is the gate read-root mode,
surfaced to users as `source = "attested"` in `docs/config.md`. Its meaning is close to the
OPPOSITE of the tag's: snapshot-`attested` means *pinned to an immutable in-repo SHA*, whereas the
tag means *evidence is not in the repo at all*. There is no functional conflict — nothing parses
acceptance-criterion text against snapshot source modes — but both are read by the same LLM
reviewers, and the repository additionally carries `src/rebar/attest/` (DSSE/SSHSIG signing) and
~2,000 uses of `attestation` for the signed certificates. A bare `[attested]` would land in the
middle of three unrelated senses.

### Why not a mechanism-named tag such as `[recorded-evidence]`

Both kinds of criteria have evidence recorded on the ticket, so "recorded" does not discriminate —
and naming the tag that way would actively invite the laundering anti-pattern. rebar instructs
authors, for *codebase-verifiable* criteria, to "add an UNTAGGED comment to this ticket citing the
exact test function names, file paths, and merge SHAs … so recorded evidence is taken into account"
(`INSUFFICIENT_EVIDENCE_REMEDIATION`). Under a `[recorded-evidence]` name that instruction reads as
self-contradictory, and an author would reasonably conclude the tag applies.

`[non-codebase]` discriminates on the axis that actually decides the question, and it resists
laundering by construction: to tag a criterion whose proof is `tests/unit/test_scan_scoping.py`
(the live bug 2f56 case), an author must assert that file is "non-codebase" — self-evidently false.
The name does the `det_attestation_launder` guard's work in the author's head before the guard runs.

## Consequences

**The exact-token fail-safe is relaxed, deliberately and narrowly.** ADR 0043 made matching exact
on one token so that a garbled or missing tag falls back to the STRICTER codebase-verifiable bar.
The matcher now accepts exactly TWO tokens — `non-codebase` and `operator-attested` — via a
non-capturing alternation. Everything else, including near-misses such as `[operator_attested]`,
`[non_codebase]`, and `[noncodebase]`, still falls back to codebase-verifiable. The fail-safe
property is preserved; only the accepted set grew by one deliberate member.

**Existing tickets keep working, which is why the alias is not optional.** 827 of 4,285 tickets in
the live store (19%) carry `[operator-attested]` in their descriptions. Those are immutable
event-sourced records. Dropping the spelling would silently reclassify every one of their tagged
criteria as codebase-verifiable and break their close path.

**The close-gate exemption reaches both spellings.** `ensure_ac_boxes_checked`
(`src/rebar/_commands/txn.py`) reuses the same matcher with inverted semantics — a match EXEMPTS an
unchecked criterion from the close block. An unchecked `[non-codebase]` criterion is therefore
close-exempt exactly as an `[operator-attested]` one is. This is intended: the exemption is the
tag's purpose, and a rename that did not carry it would silently strip the tag's effect.

**Not renamed.** The Python identifiers keep their names — the `det_operator_attested` module,
`_OPERATOR_ATTESTED_TAG_RE`, `_OPERATOR_ATTESTED_AC_RE`, the `operator_attested` finding attribute,
and the `operator_attested_gaps` coverage key. Renaming them would churn the object-identity
re-export seam (`workflow_ops` re-exports the compiled pattern by identity, asserted with `is`) for
no behavioral gain. The historical record is likewise untouched: ADR 0043 stands as written, and the
frozen experiment corpora under `docs/experiments/plan-review-gate/` keep their captured text and
their `operator-attested-retag` edit-classification label, which is a different token family and not
this tag.
