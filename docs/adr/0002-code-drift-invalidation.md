# ADR 0002: Code-drift invalidation for plan-review attestations

- **Status:** Accepted
- **Context:** Epic *Code-drift invalidation for plan-review attestations*
  (`boil-golem-veto`/4fcd); brainstorm session log `rich-keel-pen`.

## Context

A plan-review attestation (`src/rebar/llm/plan_review/attest.py`) certifies that a ticket's plan
passed review *against the code it reasons about*. Two facets of the current binding are wrong:

- `claim_gate_check` binds freshness to the exact code HEAD sha, so **any** commit — even an
  unrelated file — invalidates a still-correct attestation (over-invalidation; bug
  `worm-folly-barge`).
- The whole-HEAD binding is also imprecise: it is not scoped to the code the review actually
  depended on.

The material-fingerprint binding (description/AC/file_impact/decomposition edits invalidate; not
tags/comments/links/assignee — shipped in `dead-stalk-chasm`) is correct and orthogonal; this ADR
governs only the *code*-drift signal.

## Decision

1. **Scope invalidation to a dependency set, not to whole-HEAD.** The dependency set is the
   ticket's declared `file_impact` ∪ the files cited in its `REVIEW_RESULT` findings.

2. **Bind by content, not by attribution.** Store a per-path `{path: content-hash}` map (whole-file
   hashes, v1) **inside the signed manifest**, and at claim re-hash + compare. This is
   content-addressed (Bazel/Nix/git-style): ancestry-independent (rebase/HEAD-move immune) and
   tamper-evident. We explicitly reject attributing a change to a ticket/author to decide staleness
   — every sound incremental-staleness system (Gerrit `copyCondition`, Bazel, proof-caching) keys on
   the depended-on inputs, not the author, so the "sibling exemption" need dissolves.

3. **Per-path map, not a single rolled-up root.** The manifest serializes the binding as a
   `{path: hash}` map so a later stage (Story 2's delta-re-review) can attach a per-finding
   code-hash vintage and reuse map; a single rolled-up root would foreclose that.

4. **Invalidate (hard block), not flag-stale.** A drifted dependency blocks the claim and prompts
   re-review — consistent with the gate's existing absent/stale behavior. No soft-warning mode.

5. **Empty dependency set → conservative fallback** (invalidate on any commit), plus a new
   DET-floor advisory (`P9`) nudging authors to populate `file_impact` so scoping works.

## Consequences

- The over-invalidation bug is fixed and drift detection becomes precise.
- One-way-door: the signed manifest payload SHAPE (per-path map) becomes a contract that Story 2
  builds on; changing it later would break attestation verification across versions.
- Attestations signed before this lands carry no map and are treated as needing re-review on first
  claim under the new code (recovery = re-run `review-plan`). Back-out = revert the drift check; the
  material-fingerprint binding is untouched.
- Whole-file granularity may over-invalidate on a cosmetic edit elsewhere in a shared dependency
  file; region/AST narrowing is a possible future refinement, deliberately not built in v1.
