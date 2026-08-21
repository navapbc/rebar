# ADR 0102 — Review discovery reads ONE compiled effective-policy snapshot

**Status:** Accepted
**Date:** 2026-08-21
**Ticket:** RP-06 S1 — Compile immutable effective review-policy snapshots
**Relates to:** [ADR 0017](0017-unified-criteria-registry.md) (the shared
`rebar.llm.criteria` delegation layer this extends), [ADR 0098](0098-operation-scoped-config-and-provider-composition.md)
(the `OperationSnapshot` this is composed with, not duplicated), and
[ADR 0074](0074-code-review-overlay-escalation.md) (the project-vs-built-in
`applies_to` meaning this makes explicit).

## Context

The overlay core (`rebar.llm.criteria.overlay`) already reconciles the packaged routing
index with a project `.rebar/criteria_routing.json` overlay into an *effective* view, and
each gate's registry (`plan_review.registry`, `code_review.registry`) exposes thin readers
over it (`effective_routing` / `effective_criteria` / `disabled_builtins`, plus code-review's
`project_criterion_applies`). But every consumer reads that ambient policy on its OWN cadence.
Two consequences follow structurally:

* **Divergent reads.** Two consumers of the same gate can disagree the instant the overlay
  changes under them — there is no single pinned authority they both quote.
* **Gate-siloed applicability.** The code-review `applies_to` glob rule lives inside one
  gate's registry, invisible to any cross-gate reader. Its ungated spelling — an empty
  `applies_to` meaning "runs on every review" — collided with a naive
  `any(glob_match(f, g) for f in changed_files)` migration: rewriting `[]` to `["**"]` would
  have made the criterion silently STOP selecting a review whose `changed_files` set is empty,
  because `any(...)` over an empty iterable is False. That is the exact regression RP-06's
  plan-review flagged.

## Decision

Add a repository-bound **`CriteriaSnapshot`** in `rebar.llm.criteria.snapshot`, compiled by
`compile_snapshot(repo_root)`. It is ONE immutable (`frozen=True`), digest-bound projection
compiling — for BOTH gates — the effective built-ins, project LLM criteria, project DET
criteria, routing, and per-id source provenance. Consumers receive the snapshot whole and read
policy off it; they do NOT reread ambient policy. It is **data/policy only**: it does not
interpret YAML/BPMN topology, execute a criterion, or decide a verdict.

**Digest.** `.digest` is a deterministic 64-char sha256 hex string. It REUSES the overlay's
existing content signature `overlay._overlay_signature(repo_root)` as an input, combined with a
canonical serialization of the compiled per-gate routing. So the digest is stable across
recompiles of the same policy and changes exactly when overlay content changes — the same
content digest the overlay cache already keys on, not a second, drift-prone hash. The digest is
deliberately a **content fingerprint of the effective policy, not a per-repository identifier**:
two repositories whose effective overlays are byte-identical (or both absent) share a digest,
which is the intended semantics for "has the effective policy changed?" comparisons. Repository
binding is a SEPARATE, explicitly modeled concern — the snapshot carries `repo_root` as its own
frozen field so the digest never has to encode repository identity, and the shared
`check_repo_root_agreement` separately guards that criterion discovery and prompt resolution ran
against the same root — so the digest is never the mechanism that distinguishes repositories.

**Gate-specific applicability.** A code-review project LLM criterion's `applies_to` is
validated at snapshot-compile time as a NON-EMPTY list of non-empty repository-relative glob
strings; `["**"]` means repository-wide and MUST select the criterion UNCONDITIONALLY —
including when `changed_files` is empty. That single rule
(`select_project_applicability`) is implemented ONCE and shared by both the snapshot's
`code_review_project_applies` and the code-review registry consumer, so the `[]` → `["**"]`
overlay migration is behavior-preserving at the empty-`changed_files` edge. The stricter rule
is enforced on the code-review-gated path only; the shared `overlay._validate_applies_to`
still permits an empty list ("ungated") for both gates, and plan-review project criteria (which
carry no `applies_to`) are unaffected.

**Prior art — composed, not duplicated.** The snapshot is a projection of criteria policy,
composed WITH ADR 0098's `OperationSnapshot` (the operation-scoped configuration authority),
not a re-implementation of it: `OperationSnapshot` pins provider/config bindings; this pins the
effective criteria vocabulary and routing. It extends the ADR 0017 shared-`rebar.llm.criteria`
delegation layer by adding a compiled authority ALONGSIDE the existing registry readers, which
remain compatibility adapters over the same overlay core.

**Why one compiled authority beats tightening gate reads in place.** Fixing the empty-
`changed_files` edge by patching each gate's registry read would leave the divergent-read class
intact — every future consumer re-derives policy and can re-introduce the same skew. A single
compiled, digest-bound authority removes that class STRUCTURALLY: there is exactly one
projection, quoted by digest, that every consumer shares.

## Consequences

* Existing registry readers stay as compatibility adapters; an overlay-absent repo behaves
  byte-identically to before.
* The `[]` → `["**"]` migration of `code_review.project.review-phase-boundaries.applies_to`
  is behavior-preserving.
* Rollback is a plain code revert (no data migration, no persisted snapshot state).
