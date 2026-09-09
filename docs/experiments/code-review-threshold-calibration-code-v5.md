# Code-review threshold calibration 4 (code-v5)

Ticket `3055-e92d-7063-4a32` (`cattish-disdainful-gander`). The FIRST calibration segmented to
the **code-v5** impact model. Generated with:

```
python docs/experiments/calibrate_code_review_thresholds.py --tracker <checkout>/.tickets-tracker
corpus: 1135 sidecars / 703 changes / 3573 pooled findings
skipped remainder: 7757 sidecars (different_version=2153, untagged=0, unparseable=0, wrong_schema=5604)
```

## Why this run exists

The thresholds in force were adjudicated on the **code-v3** corpus (this directory's
`code-review-threshold-calibration.md`, plus the code-v4 friction replay which was itself still
*over the code-v3 corpus*). `IMPACT_MODEL_VERSION` moved to **code-v5** on 2026-08-21
(`sidecar.py:35`), one day before the last routing flip. ADR 0036 forbids pooling across impact-
model versions, so every committed threshold was tuned against a model that is no longer running,
and 1,132 code-v5 sidecars had accrued unanalysed (2026-08-21 → 2026-09-08).

Per ADR 0054 this is a **field-corpus** calibration: no bespoke LLM assessment suite. The
adjudication below is a content read of the findings each flip would newly block, because the
false-positive/nit rate of a threshold change is a question about finding TEXT that no validity
statistic answers.

## Method

`--dump-newly-blocking CRIT=THR` (added in this ticket) writes the findings whose priority falls
in `[proposed_threshold, current_threshold)` — exactly the set a flip ADDS over what already
blocks. Each was classified **TP-blockworthy** / **TP-but-nit** / **FP**, verifying the cited code
against the tree where the finding names a file (34 of 37 verified; 3 cite changes that never
landed and are marked unverifiable rather than FP).

A high validity score means "the claim is true", NOT "it matters"; only the content read
separates a defect that should stop a change from a correct-but-cosmetic observation.

## Adjudicated flips

| criterion | now | proposed | newly blocking | TP-blockworthy | nit | FP | decision |
|---|---|---|---|---|---|---|---|
| `security` | blocking@0.54 | **0.45** | 13 | 13 | 0 | 0 | **flip** |
| `concurrency` | advisory@0.95 | **blocking@0.52** | 2 | 2 | 0 | 0 | **flip** |
| `maintainability` | UNROUTED (0.95 default) | 0.60 | 17 | 8 | 8 | 1 | **hold** |

- **`security` @0.45** — friction 0.29% → 1.43% of changes (2 → 10 of 703). Mean validity of the
  would-block set 0.990; zero findings below validity 0.5. Every one names a hole in a guard that
  ALREADY exists: a fail-closed PAT check skipped on the anonymous-client write path;
  `_tf_files_in_dir` with no realpath check so symlinked leaves escape containment;
  `_pinned_environment_keys` dropping `revoked_at_log_position` so revoked keys still certify;
  `detected_by` absent from `_IMMUTABLE_EDIT_FIELDS`, leaving provenance mutable by EDIT.
- **`concurrency` @0.52** — friction 0.29%. Held advisory at the code-v4 replay solely because
  n=1 was below the script's `MIN_N = 25` power floor; the code-v5 segment gives it n=68
  (53 surviving), so the hold reason has expired. **0.52 rather than 0.50**: the three adjudicated
  nits all sit at priority 0.5143, which 0.52 excludes and 0.50 admits; and because
  `threshold_for` resolves a multi-criterion finding at the MIN of its criteria's thresholds,
  0.52 limits the leak onto co-tagged `correctness` findings (blocking@0.54) to 0.02 rather
  than 0.04. Five of the seven are co-tagged `correctness`.
- **`maintainability` HOLD** — it is UNROUTED today (no entry at all, so it silently takes the
  0.95 unknown-criterion default) despite being the third-largest criterion, n=607. Routing it is
  correct in principle; 0.60 is not the threshold. 53% of what it would block is nit or FP:
  8 real defects (a regression test silently uncollected and running its body as dead statements;
  a network `git fetch` on the ticket write path; process-global `warnings.filterwarnings` from a
  constructor) against 8 documentation-drift nits (a stale docstring, a schema `description`
  contradicting its own property) and 1 FP. Deferred pending a separator.

## Rejected: a `blast_radius` / `debt_impact` severity floor

Within the two would-block bands above, `impact` is CONSTANT (0.6 for every `concurrency`
finding; 0.6 for 16 of 17 `maintainability` findings), so `priority` reduces to `validity` and no
threshold can separate a nit from a defect. `blast_radius == "system" OR debt_impact == "high"`
separated them at 100% precision on those 8 and 3 findings respectively, suggesting a floor.

**Measured across the whole corpus, that rule is unsafe and is NOT adopted.** It would newly block
48 findings across 10 criteria; adjudicated, **27 TP-blockworthy, 20 nits, 1 FP — 44%
non-blockworthy**. Per axis: `blast_radius == system` alone is 50% precise and fires on 43 of the
48; `debt_impact == high` is 80% precise but reaches only 10; the conjunct is 100% precise on 5,
which is precisely the cohort the rule was fitted to. `api-compat` is the worst case at 7/7
non-blockworthy — all deliberate, documented, mitigated changes re-litigated as defects.

The mechanism: **`blast_radius` records the scope of the MECHANISM, not the scope of the HARM.**
It was applied to a runbook markdown file, a content-addressed cache gaining an entry cap, and a
documented default with a documented opt-out. Five of the lifted findings scored `impact` exactly
0.0 — the verifier found no consequence lane at all — and the floor would have taken them to 0.9
on one ordinal label.

This also contradicts a standing design decision: `review_kernel/decide.py:359` records
*"Ordinal labels are an LLM anti-pattern: models do not apply none|low|medium|high reliably enough
for deterministic gate behavior"* (plan-v5, ticket `fixable-angular-caribou`). The floor would
re-admit exactly the signal class that was retired, as a hard override at the serious tier.

**The separator must be a consequence binary, not an ordinal floor.** The block-worthy findings
left in advisory are ones where an existing binary is plainly true and was left false — e.g.
`security_bypass_not_enforced_elsewhere` on a `detected_by` privilege escalation. That is a Pass-2
emission defect, tracked separately.

## Zero-impact population

**912 of 3,563 findings (25.8%) score `impact` exactly 0.0**, of which **376 carry validity ≥ 0.8**
— verifier-confirmed yet unblockable at any threshold, since `priority = validity × impact`.

A 147-finding stratified sample, reweighted: **44.4% NIT** (correctly zero — concentrated in
`tests` and `maintainability`, 199 of the 376, mostly dangling docstring references after a
rename), **31.9% a substantive defect scored at zero**, 23.7% ambiguous. Four spot-checked
findings have since been fixed by hand — the gate found them, scored them zero, and the repo paid
to fix them later.

The misses cluster; the largest pattern (47%) is **a guard that exists but cannot fail** — e.g.
`scripts/check_uv_pin.py:418` globs `**/Dockerfile*` only, so Compose `image:` keys are unguarded,
and `docker-compose.langfuse.yml:113` still pins `docker.io/minio/minio:latest`, the exact defect
that gate exists to eliminate. No existing binary covers this: `safety_net_removal_without_replacement`
needs a removal, `reachable_path_without_automated_coverage` needs absent coverage; here coverage
nominally exists.

Structural cause: `impact_code` has **no additive path from the severity axes**. Every lane is
`max(tier over TRUE binaries) × multiplier`, so when all 12 binaries abstain nothing lifts the
score off zero. Further, the moderate-maint lane is inert for this population — 364 of the 376
have `prod_impact ∈ {none, low}` by construction, and the lane multiplies by exactly that.
Measured: flipping one moderate-lane binary true moves **2** of 376 above 0.50; the same tier in
the undamped serious-maint lane moves **263**.

## Mixed criteria (rubric defects, not threshold defects)

| criterion | n | validity ≥ 0.8 | validity < 0.5 | dominant refutation |
|---|---|---|---|---|
| `supply-chain` | 139 | 23.0% | 49.6% | `impact_follows_necessarily` 93% "no" |
| `scope-intent` | 72 | 23.6% | 48.6% | `impact_follows_necessarily` 100% "no" |

Both are bimodal — a real ~23% true-positive mode alongside a ~49% refuted mode — so no threshold
separates them and neither should be suppressed. `supply-chain`'s FP mode is the
*unverifiable-from-the-diff* claim (a hedge regex hits 20 of 40 low-validity and 0 of 20
high-validity findings), licensed by the overlay's evidence contract admitting "an ABSENCE
rationale". `scope-intent`'s is *enumeration-as-boundary* — treating a ticket's ACs as an
exhaustive whitelist — licensed by step 2's "drive-by edits with no ticket backing". Root causes
per `docs/code-review-fp-ledger.md`: `stale-baseline` / `hallucinated-gap` and
`rubric-overapplication` respectively. Prompt remediations are tracked separately.

## Friction under code-v5 at the thresholds in force

| criterion | thr | code-v5 % of changes | recorded at its calibration |
|---|---|---|---|
| `tests` | 0.54 | 8.70% | 8.0% (operator-accepted budget) |
| `correctness` | 0.54 | 8.42% | 4.04% |
| `regression` | 0.54 | 5.99% | 2.81% |
| `error-handling` | 0.50 | 3.28% | 0.26% |
| `api-compat` | 0.51 | 2.85% | 0.77% |
| `edge-cases` | 0.54 | 2.57% | 1.74% |

**16.71% of changes (118 of 703) are blocked by at least one criterion**, and `tests` has drifted
past the 8.0% budget recorded on its own routing entry. The right-hand column is NOT a controlled
comparison — it was measured on the code-v3 corpus, a different diff mix, and ADR 0036 forbids
pooling the two — so it is directional only. The code-v5 column is measured.

## Corpus hygiene

Duplicate `norm_id` **within a single sidecar is zero** across all 2,801 surfaced findings: the
Pass-3 dedup works. The 346 repeats in the corpus are the same finding re-reported on a later
patchset of the same change, which is correct behaviour. What IS real: **20.7% of surfaced
findings carry two or more blocking criteria**, and since `threshold_for` takes the MIN across
them, promoting any criterion lowers the effective bar on findings that primarily belong to
another. That resolution rule has no recorded rationale — it entered as an inline expression in
the original plan-review gate (commit `2873a916`, epic 5fd2) and was propagated mechanically; the
only documentation is the parenthetical "most aggressive".
