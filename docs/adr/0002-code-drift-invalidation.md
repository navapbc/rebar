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
   *(Amended by ticket 3e4b `saddened-unadult-snowmonkey`:)* a **container** additionally
   inherits the union of its **direct** children's declared `file_impact` into the signed
   set — with a poison rule (any non-closed child with empty impact disables inheritance,
   keeping the fallback fail-closed) and closed children neither contributing nor
   poisoning (ADR 0024). One level deep only: the container review's plan-material pins
   cover a direct child's `file_impact`, so a child impact edit invalidates the container
   attestation and the union is recomputed on re-review (claim-blocking under
   `verify.enforce_plan_material_pins`); no such self-healing exists for grandchildren.
   `P9` is container-aware: it passes a container with effective inheritance and names the
   poisoning children otherwise.

## Consequences

- The over-invalidation bug is fixed and drift detection becomes precise.
- One-way-door: the signed manifest payload SHAPE (per-path map) becomes a contract that Story 2
  builds on; changing it later would break attestation verification across versions.
- Attestations signed before this lands carry no map and are treated as needing re-review on first
  claim under the new code (recovery = re-run `review-plan`). Back-out = revert the drift check; the
  material-fingerprint binding is untouched.
- Whole-file granularity may over-invalidate on a cosmetic edit elsewhere in a shared dependency
  file; region/AST narrowing is a possible future refinement, deliberately not built in v1.

## Story 2 — progressive drift-refresh + measurement

Story 2 (`grace-thud-feint`) adds a **progressive (tripwire) re-review**: when an attestation is
stale ONLY because reviewed code drifted (ticket material + criteria-registry unchanged), run a
cheap probe — `E4` + `G1G2` ("is the plan still accurate vs the codebase") — against the current
code. A clean probe REFRESHES the attestation (re-sign the prior verdict with the current
dependency hashes); any blocking finding, or a finding citing a drifted file, escalates to a FULL
re-review. Soundness is whole-verdict, gated by the probe — there is NO per-criterion finding reuse,
so the (unenforced) code-blind criterion partition is not relied upon. Gated by
`verify.progressive_drift_refresh` (**default ON** since 2026-07-12, epic a37b; explicit false backs out) (retired in the config-prune epic; the behavior is now always-on and unconditional); fail-safe to full re-review on probe
error / registry-version skew / no reusable prior verdict.

> **One of three Pass-3 floors.** This drift-refresh is one of three deterministic Pass-3
> drop/refresh paths along independent axes: **material freshness** (this ADR), **novelty**
> (ADR 0008 — plan-edit convergence), and **delivered-completion** (ADR 0024 — the container
> completion floor, which suppresses re-litigation of already-delivered, settled plan text).
> They compose; each is separately gated and shipped inert by default. (Update 2026-07-11:
> the novelty floor's flags — `remediation_mode` + `novelty_drop_active` — were flipped ON by
> default, operator-authorized on field evidence; drift-refresh and the completion floor
> remain default-off.) (Later update: `remediation_mode`, `novelty_drop_active`, and
> `progressive_drift_refresh` were all retired in the config-prune epic; those behaviors are
> now always-on and unconditional.)

### Measurement (cost of probe-only refresh vs full re-review)

Reported from this implementation's own real plan-review runs (model `claude-opus-4-8`):

| Review | LLM calls | Wall-clock (llm_ms) |
|---|---|---|
| Full story-level review (Story 1 `jerky-vista-amok`) | 9 | ~172 s |
| Full story-level review (Story 2 `grace-thud-feint`) | 9 | ~337 s |
| Full epic-level review (`boil-golem-veto`) | 11 | ~512 s |

A full story review is ~6 agent-tier (85×) criteria + ~15 single-turn criteria + DET + Pass-2/Pass-4.
The progressive **probe** runs only `E4` + `G1G2` (2 agent-tier finders) + one Pass-2 aggregate
verify (Pass-3 is deterministic, no LLM) ≈ **3 LLM calls**, and the agent-tier finders dominate
latency — so the probe is ≈ **1/3 of the full-review LLM calls** and a smaller fraction of wall-clock
(2 of 6 agent-tier finders vs the full set), comfortably under the <50% sanity target. (Update
2026-07-12, epic a37b: the saving is real but bounded — the 2 probe criteria are themselves
agent-tier — and on that structural basis the path is now **default ON**; an explicit
`verify.progressive_drift_refresh = false` backs out to the full-re-review path.) (Retired in
the config-prune epic: the config key no longer exists and the path is now always-on.)

## Shared ref-resolution boundary (epic `raze-vet-ditch`, S4b `piney-gold-day`)

ADR 0005 makes the code-reading gates verify a *pinned-SHA snapshot* (attested mode) instead of
the server's mutable checkout. That re-opens a divergence hazard *inside* this drift mechanism:
plan-review computes the signed `{path:hash}` map at one ref basis, and the claim-gate freshness
re-check (this ADR) re-hashes those paths at another. If the two resolved the ref independently —
plan-review at the pinned-SHA snapshot, the claim gate at whole-HEAD / the working tree — they
would disagree and re-introduce the staleness false-positive this ADR exists to prevent.

**Consolidation** *(amended by bug 72d9 `athletic-esthetical-polecat` — see below)*. There is
now exactly ONE shared ref-resolution boundary,
`rebar.llm.plan_review.attest._hash_basis(repo_root, *, at_current_gate_ref=False)`, that BOTH
consumers resolve through (the Rule-of-Three is satisfied by the two consumers + the concrete
divergence risk, not speculative generality):

- **plan-review signing** (`dependency_hashes` / `_rehash`) hashes at the active attested snapshot
  (S3's context-local code root) and records the snapshot SHA in the signed manifest as a
  `verified-at-sha:<sha>` step (the same channel S4 uses — no payload/version change).
- **the claim gate** (`claim_gate_check`) re-hashes the dependency paths at a snapshot of the
  **current gate ref** (`REBAR_GATE_REF` / `[snapshot].ref` / `origin/main`, resolved from the
  local object DB with no network) and compares against the manifest's pinned hashes.

The boundary's guarantee is that both sides resolve with the SAME **semantics** — a committed
snapshot under the same gate configuration — never a possibly-dirty working tree on one side
and a snapshot on the other. It must NOT make the two bases **identical**: the original S4b
consolidation had the claim gate re-hash at the signature's own pinned `verified_at_sha`,
which compared the manifest against the immutable tree it was generated from. That comparison
always matched, so scoped drift was structurally undetectable in attested mode — the
production default (bug 72d9, `athletic-esthetical-polecat`). With the amended basis, a signed
dependency file that changed between the review's pinned SHA and the current gate ref
registers as drift, while an unrelated commit still does not (per-path content comparison —
the anti-false-positive property this ADR exists for). An uncommitted working-tree edit moves
neither side, so the S4b false-positive protection is preserved.

## Addendum — scoping the NO-`file_impact` case to the review read-set (ticket 81ca)

Decision 5 above sends an EMPTY dependency set to the whole-HEAD fallback. That fail-safe was
measured to over-trigger badly: 21% of full plan re-reviews ran on byte-identical material, most
of them from base-SHA drift alone. This addendum scopes the fallback instead of removing it; the
per-path comparison loop, the `{path: hash}` payload shape, and the ref-resolution boundary are
all unchanged.

- **The review's READ-SET rides inside the signed manifest.** The agentic passes already observe
  which repository files they open (`distinct_fetches` in `rebar.llm.usage_log`). That telemetry
  is projected onto repo-relative POSIX file paths by
  `rebar.llm.plan_review.read_set.normalize_read_set` — `read_file` targets only (a `search_files`
  target is a query string, a `list_directory` target is a directory), resolved against the hash
  root, dropped when they escape it or do not name an existing regular file, then deduped and
  sorted. The manifest records them as additive `read-path: <path>` lines under a
  `read-set: <count>` marker. Because the signature covers the whole manifest, a tampered or
  truncated read-set fails verification and resolves to the fail-safe.
  The marker's presence is load-bearing: **no** `read-set:` line (a pre-81ca attestation, a
  review where no agentic pass ran, or a telemetry failure) means the scoping was not applied and
  whole-HEAD freshness still governs; `read-set: 0` means the review verifiably read nothing.

  The marker is set ONLY when at least one repository fetch was OBSERVED and survived
  normalization. An empty observation is deliberately not read as "the review verifiably read
  nothing": a runner that reports no fetch telemetry is indistinguishable from one that genuinely
  opened no files, and the stronger reading would scope in the fail-OPEN direction.

- **Composition rule — and its two preconditions.** The scoping applies ONLY where this ADR
  leaves a hole. All three must hold: the pre-existing dependency set (declared `file_impact` ∪
  cited paths ∪ inherited child impact) is EMPTY; the container poison rule is NOT in effect; and
  a read-set was recorded. The set then becomes read-set ∪ the blast-radius entries
  (`read_set.BLAST_RADIUS_ENTRIES`: the criteria machinery, the routing overlays, the reviewer
  rubrics, the gate workflows, and `rebar.toml`).

  Both preconditions are load-bearing. Adding the blast radius to an ALREADY-scoped set would
  only widen it and re-create the false positive this scoping exists to remove — an unrelated
  commit touching `rebar.toml` would invalidate a container correctly scoped to its child's
  files. And a poisoned container (decision 5's amendment: a live child with undeclared impact)
  is deliberately fail-CLOSED at whole-HEAD, precisely because any partial scope is fail-OPEN
  for that undeclared impact; read-set scoping is such a partial scope, so it must not apply.

- **Globs: expansion + membership digest.** The currency check iterates the concrete entries
  recorded at signing time, so a glob is handled twice. It is EXPANDED to its matching regular
  files, each recorded as an ordinary `dep` line (content drift, named per file); and the glob
  PATTERN itself is recorded as an entry whose digest is the SHA-256 of its sorted, newline-joined
  match list. The second closes the addition/deletion blind spot a purely per-path expansion
  leaves: a reviewer rubric added after signing has no baked per-file hash but moves the
  membership digest. Both the signing side and the claim-gate re-check dispatch through the one
  shared `read_set.hash_dep_entry`, so the two can never compute a glob digest differently.

- **Reported basis.** The manifest records `currency-basis: file_impact | read-set | fail-safe`,
  surfaced by `rebar review-plan <id> --status` (no LLM, no network). A pre-81ca manifest carries
  no line and is derived conservatively: `file_impact` when dep lines exist, else `fail-safe`.

**Back-out.** A configured `source=local` gate — or a gate ref/snapshot that cannot be
resolved — degrades the claim-gate basis to the in-place checkout, exactly the per-site
working-tree behavior that predated this consolidation (the conservative direction: the
checkout normally contains the drift). A pre-S4b attestation carries no `verified-at-sha`
step; it still verifies, with its signed hashes compared against the same current-gate-ref
basis. The per-path map contract and the material-fingerprint binding are untouched.
