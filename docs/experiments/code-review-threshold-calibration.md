# Code-review threshold calibration (code-v3)
[segmented to impact_model_version=code-v3]
corpus: 1760 sidecars / 1173 changes / 4359 pooled findings
skipped remainder: 3135 sidecars ({'different_version': 114, 'untagged': 0, 'unparseable': 0, 'wrong_schema': 3021})

> **Corrections (ticket d890-e711-156e-444b).** This document embedded a vocabulary
> mistake in several load-bearing places, corrected in place at the five sites marked
> **CORRECTION (A)–(E)** below. The shared root cause: every criterion label was compared
> against the PACKAGED routing index rather than the EFFECTIVE vocabulary (the packaged
> index merged with the project's `.rebar/criteria_routing.json` overlay). The `criteria`
> dimension vocabulary is open by design (the base prompt's list is illustrative, project
> criteria are first-class through the overlay), and the 0.95 default for an unrouted label
> is the intended mostly-advisory v1 posture, not a routing bypass. The consequent
> recommendation to constrain the reviewer prompt to a fixed vocabulary is REJECTED — a
> closed enum would make project criteria unexpressible. **The four approved blocking flips
> (`regression` 0.54, `api-compat` 0.51, `deletion-impact` 0.60, `error-handling` 0.50) and
> the block-impact table are UNAFFECTED — all four already landed in the packaged routing
> index — so this calibration's headline result stands.** The only genuine data defects are
> the two model-emitted synonyms `sec`→`security` and `documentation`→`docs`, now normalized
> before routing by `registry.normalize_criteria` (see (A)/(B)). One denominator note: this
> document's figures are POOLED — `calibrate_code_review_thresholds.py` buckets sidecars by
> `change_id` and segments them into material revision episodes before pooling findings —
> whereas a raw per-sidecar label census (the 1663 sidecars / 5669 label instances recorded
> on ticket d890-e711-156e-444b) is a different, larger denominator; the
> two must never be compared directly.

## Per-criterion signals

| criterion | n | surf | fire | mval | drop | indet | pblk | rev_rr | elig | p75 | p90 | p95 | pmax | worst subq (no-rate) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| tests | 1838 | 1583 | 0.543 | 0.895 | 0.129 | 0.009 | 0.0 | 0.354 | 293 | 0.3 | 0.31 | 0.32 | 0.9 | impact_follows_necessarily (0.25) |
| maintainability | 913 | 725 | 0.382 | 0.848 | 0.194 | 0.012 | 0.0 | 0.571 | 175 | 0.3 | 0.31 | 0.36 | 0.9 | impact_follows_necessarily (0.33) |
| correctness | 865 | 683 | 0.341 | 0.827 | 0.197 | 0.014 | 0.0 | 0.55 | 184 | 0.3 | 0.463 | 0.54 | 0.9 | impact_follows_necessarily (0.34) |
| edge-cases | 588 | 461 | 0.254 | 0.837 | 0.206 | 0.01 | 0.0 | 0.577 | 130 | 0.3 | 0.36 | 0.54 | 0.9 | impact_follows_necessarily (0.35) |
| docs | 455 | 104 | 0.16 | 0.634 | 0.76 | 0.011 | 0.0 | 0.718 | 35 | 0.3 | 0.32 | 0.514 | 0.9 | asserted_capability_confirmed (0.91) |
| regression | 377 | 307 | 0.18 | 0.831 | 0.167 | 0.019 | 0.0 | 0.679 | 93 | 0.36 | 0.54 | 0.6 | 0.9 | impact_follows_necessarily (0.29) |
| error-handling | 238 | 182 | 0.119 | 0.834 | 0.231 | 0.004 | 0.0 | 0.641 | 55 | 0.3 | 0.32 | 0.36 | 0.54 | impact_follows_necessarily (0.32) |
| supply-chain | 137 | 47 | 0.041 | 0.403 | 0.635 | 0.022 | 0.0 | 0.524 | 15 | 0.086 | 0.257 | 0.3 | 0.4 | impact_follows_necessarily (0.9) |
| api-compat | 118 | 100 | 0.048 | 0.88 | 0.136 | 0.017 | 0.0 | 0.75 | 34 | 0.377 | 0.514 | 0.6 | 0.771 | no_existing_mitigation (0.29) |
| scope-intent | 117 | 49 | 0.051 | 0.521 | 0.581 | 0.0 | 0.0 | 0.706 | 16 | 0.3 | 0.36 | 0.45 | 0.6 | impact_follows_necessarily (0.74) |
| deletion-impact | 73 | 63 | 0.025 | 0.876 | 0.11 | 0.027 | 0.0 | 0.659 | 21 | 0.372 | 0.6 | 0.6 | 0.771 | no_existing_mitigation (0.21) |
| iac | 71 | 55 | 0.021 | 0.764 | 0.225 | 0.0 | 0.0 | 0.542 | 12 | 0.3 | 0.36 | 0.39 | 0.6 | impact_follows_necessarily (0.49) |
| performance | 62 | 52 | 0.03 | 0.876 | 0.161 | 0.0 | 0.0 | 0.846 | 12 | 0.221 | 0.3 | 0.3 | 0.6 | severity_claim_justified (0.26) |
| bugfix-size-attestation | 61 | 61 | 0.035 | None | 0.0 | 0.0 | 0.525 | 0.353 | 34 | 0.0 | 0.0 | 0.0 | 0.0 | None (0.0) |
| security | 60 | 44 | 0.03 | 0.764 | 0.267 | 0.0 | 0.05 | 0.588 | 15 | 0.3 | 0.463 | 0.54 | 0.54 | impact_follows_necessarily (0.45) |
| llm-prompts | 23 | 11 | 0.013 | 0.823 | 0.478 | 0.043 | 0.0 | 0.5 | 6 | 0.6 | 0.6 | 0.6 | 0.6 | impact_follows_necessarily (0.36) |
| clarity | 14 | 12 | 0.008 | 0.871 | 0.143 | 0.0 | 0.0 | 1.0 | 3 | 0.24 | 0.257 | 0.257 | 0.3 | impact_follows_necessarily (0.36) |
| project.review-phase-boundaries | 11 | 2 | 0.006 | 0.385 | 0.818 | 0.0 | 0.0 | 1.0 | 1 | 0.54 | 0.54 | 0.54 | 0.54 | evidence_entails_finding (0.82) |
| sec | 8 | 8 | 0.005 | None | 0.0 | 0.0 | 1.0 | None | 0 | 0.0 | 0.0 | 0.0 | 0.0 | None (0.0) |
| db-migrations | 8 | 8 | 0.002 | 0.848 | 0.0 | 0.0 | 0.0 | 0.667 | 3 | 0.463 | 0.463 | 0.54 | 0.54 | impact_follows_necessarily (0.5) |
| secret-detection | 6 | 6 | 0.003 | None | 0.0 | 0.0 | 1.0 | 0.667 | 6 | 0.0 | 0.0 | 0.0 | 0.0 | None (0.0) |
| documentation | 5 | 5 | 0.002 | 0.95 | 0.0 | 0.0 | 0.0 | 1.0 | 1 | 0.3 | 0.45 | 0.45 | 0.45 | no_viable_alternative_explanation (0.2) |
| concurrency | 3 | 1 | 0.002 | 0.81 | 0.667 | 0.0 | 0.0 | None | 0 | 0.214 | 0.214 | 0.214 | 0.214 | None (0.0) |
| consistency | 2 | 1 | 0.001 | 0.929 | 0.5 | 0.0 | 0.0 | 1.0 | 1 | 0.257 | 0.257 | 0.257 | 0.257 | None (0.0) |
| a11y | 2 | 1 | 0.001 | 0.589 | 0.5 | 0.0 | 0.0 | None | 0 | 0.557 | 0.557 | 0.557 | 0.557 | None (0.0) |
| logic | 2 | 2 | 0.001 | 1.0 | 0.0 | 0.0 | 0.0 | None | 0 | 0.9 | 0.9 | 0.9 | 0.9 | None (0.0) |
| robustness | 1 | 1 | 0.001 | 1.0 | 0.0 | 0.0 | 0.0 | None | 0 | 0.0 | 0.0 | 0.0 | 0.0 | None (0.0) |
| resource-management | 1 | 1 | 0.001 | 1.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1 | 0.3 | 0.3 | 0.3 | 0.3 | None (0.0) |
| behavior-change | 1 | 1 | 0.001 | 1.0 | 0.0 | 0.0 | 0.0 | None | 0 | 0.36 | 0.36 | 0.36 | 0.36 | None (0.0) |

## Precision-first proposal

| criterion | n | class | posture | threshold | rationale |
|---|---|---|---|---|---|
| tests | 1838 | ADVISORY-KEEP | advisory | 0.95 | validity 0.895, drop 0.129, rev_rr 0.354; real but borderline => advisory |
| maintainability | 913 | ADVISORY-KEEP | advisory | 0.95 | validity 0.848, drop 0.194, rev_rr 0.571; real but borderline => advisory |
| correctness | 865 | ADVISORY-KEEP | advisory | 0.95 | validity 0.827, drop 0.197, rev_rr 0.55; real but borderline => advisory |
| edge-cases | 588 | ADVISORY-KEEP | advisory | 0.95 | validity 0.837, drop 0.206, rev_rr 0.577; real but borderline => advisory |
| docs | 455 | FP-PRONE | advisory | 0.95 | validity 0.634/indet 0.011/drop 0.76 => keep advisory |
| regression | 377 | BLOCK-ELIGIBLE | blocking | 0.54 | validity 0.831, drop 0.167, rev_rr 0.679; block priority>= 0.54 |
| error-handling | 238 | BLOCK-ELIGIBLE | blocking | 0.50 | validity 0.834, drop 0.231, rev_rr 0.641; block priority>= 0.5 |
| supply-chain | 137 | FP-PRONE | advisory | 0.95 | validity 0.403/indet 0.022/drop 0.635 => keep advisory |
| api-compat | 118 | BLOCK-ELIGIBLE | blocking | 0.51 | validity 0.88, drop 0.136, rev_rr 0.75; block priority>= 0.51 |
| scope-intent | 117 | FP-PRONE | advisory | 0.95 | validity 0.521/indet 0.0/drop 0.581 => keep advisory |
| deletion-impact | 73 | BLOCK-ELIGIBLE | blocking | 0.60 | validity 0.876, drop 0.11, rev_rr 0.659; block priority>= 0.6 |
| iac | 71 | ADVISORY-KEEP | advisory | 0.95 | validity 0.764, drop 0.225, rev_rr 0.542; real but borderline => advisory |
| performance | 62 | BLOCK-ELIGIBLE | blocking | 0.50 | validity 0.876, drop 0.161, rev_rr 0.846; block priority>= 0.5 |
| bugfix-size-attestation | 61 | DET/ATTEST | n/a | 0.00 | deterministic/attestation gate (pblk=0.525, validity~0); not LLM-tunable |
| security | 60 | ADVISORY-KEEP | advisory | 0.95 | validity 0.764, drop 0.267, rev_rr 0.588; real but borderline => advisory |
| llm-prompts | 23 | LOW-DATA | advisory | 0.95 | n=23 below floor; interactive review |
| clarity | 14 | LOW-DATA | advisory | 0.95 | n=14 below floor; interactive review |
| project.review-phase-boundaries | 11 | LOW-DATA | advisory | 0.95 | n=11 below floor; interactive review |
| sec | 8 | DET/ATTEST | n/a | 0.00 | deterministic/attestation gate (pblk=1.0, validity~0); not LLM-tunable |
| db-migrations | 8 | LOW-DATA | advisory | 0.95 | n=8 below floor; interactive review |
| secret-detection | 6 | DET/ATTEST | n/a | 0.00 | deterministic/attestation gate (pblk=1.0, validity~0); not LLM-tunable |
| documentation | 5 | LOW-DATA | advisory | 0.95 | n=5 below floor; interactive review |
| concurrency | 3 | LOW-DATA | advisory | 0.95 | n=3 below floor; interactive review |
| consistency | 2 | LOW-DATA | advisory | 0.95 | n=2 below floor; interactive review |
| a11y | 2 | LOW-DATA | advisory | 0.95 | n=2 below floor; interactive review |
| logic | 2 | LOW-DATA | advisory | 0.95 | n=2 below floor; interactive review |
| robustness | 1 | LOW-DATA | advisory | 0.95 | n=1 below floor; interactive review |
| resource-management | 1 | LOW-DATA | advisory | 0.95 | n=1 below floor; interactive review |
| behavior-change | 1 | LOW-DATA | advisory | 0.95 | n=1 below floor; interactive review |

> **CORRECTION (A) — the `sec` row above is WRONG.** `sec` is not a deterministic/attestation
> gate: it is an ordinary model-emitted LLM synonym of `security` (now normalized before
> routing). The generator's DET/ATTEST branch fires on the statistical signature
> `p_block > 0 and validity < 0.05` alone, without consulting any routing index — 8 findings
> that blocked under `security`'s existing 0.54 threshold happened to carry the `sec` label
> and were misclassified as a fixed-posture gate. The classification compared the label
> against the PACKAGED routing index rather than the EFFECTIVE vocabulary; labels are open by
> design and an unrouted label's 0.95 default is the intended v1 posture, so "not
> LLM-tunable" is a category error here. Constraining the prompt to a fixed vocabulary is
> rejected (see the corrections note at the top).
>
> **CORRECTION (B) — the `security` row is computed on a truncated sample.** Its 8 `sec`
> findings (~12% of the 68 security-class findings) were split into a separate row by the
> packaged-index comparison, so the `security` signals above (n=60, validity 0.764, drop
> 0.267) understate the criterion; the synonym normalization (ticket d890-e711-156e-444b)
> invalidates that row's inputs — treat it as approximate until a post-normalization re-run.
> `documentation`/`docs` is the same defect at n=5 of 455 (~1%), where the split is
> immaterial.
>
> **CORRECTION (C) — the `project.review-phase-boundaries` row ignores its overlay tune.**
> The row proposes 0.95 "LOW-DATA" although this repo's `.rebar/criteria_routing.json`
> overlay already tunes the criterion to `block_threshold: 0.9` — the generator script reads
> no routing at all (packaged or overlay), so it printed the unknown-label default for an
> overlay-activated first-class project criterion. Same root cause: the comparison used the
> PACKAGED index, never the EFFECTIVE vocabulary.

## Block-impact of the proposed thresholds (retrospective over the code-v3 corpus)

For each candidate, how many surviving (blocking+advisory) findings and distinct changes would
have crossed the proposed threshold, and the validity of that would-block set (1173 code-v3
changes total):

| criterion | thr | surviving | would-block | of surviving | changes hit | of all changes | mean validity | val<0.5 |
|---|---|---|---|---|---|---|---|---|
| regression | 0.54 | 307 | 37 | 12.1% | 33 | 2.81% | 1.00 | 0 |
| error-handling | 0.50 | 182 | 3 | 1.6% | 3 | 0.26% | 0.95 | 0 |
| api-compat | 0.51 | 100 | 11 | 11.0% | 9 | 0.77% | 0.96 | 0 |
| deletion-impact | 0.60 | 63 | 10 | 15.9% | 3 | 0.26% | 0.97 | 0 |
| performance | 0.50 | 52 | 1 | 1.9% | 1 | 0.09% | 1.00 | 0 |
| security (current) | 0.54 | 44 | 3 | 6.8% | 3 | 0.26% | 1.00 | 0 |

**Every would-block finding has validity >= 0.95; none below 0.5.** The thresholds are
precision-first: they surface only verifier-confirmed, non-nit findings.

## Adjudicated recommendation

**Flip to blocking (precision-first; all would-block findings validity >= 0.95):**

| criterion | threshold | why |
|---|---|---|
| `regression` | **0.54** | strongest candidate: n=307, validity 0.83, drop 0.17, rev_rr 0.68 over 93 episodes; blocks 2.81% of changes, would-block validity 1.00 |
| `api-compat` | **0.51** | validity 0.88, rev_rr 0.75 (34 eps), blocks 0.77% of changes, would-block validity 0.96 |
| `deletion-impact` | **0.60** | validity 0.88, rev_rr 0.66 (21 eps), dangling-reference class; blocks 0.26%, would-block validity 0.97 |
| `error-handling` | **0.50** | validity 0.83, rev_rr 0.64 (55 eps); precise but low-yield (0.26% of changes) |

**Hold advisory (insufficient evidence / thin data):**
- `performance` — rev_rr 0.85 rests on only 12 episodes; would block a single finding. Near-zero
  benefit, noisy signal. Revisit once the episode count grows.

**Keep as-is:**
- `security` @ 0.54 (already blocking) — code-v3 confirms it is precise (0.26% of changes, all
  would-block validity 1.00). Borderline rev_rr (0.588) but precision-first blocking on a security
  criterion is the right posture. *(These statistics were computed on a ~12% truncated sample —
  the criterion's 8 `sec`-labelled findings were split out; see CORRECTION (B) above. The
  keep-as-is verdict and the 0.54 threshold are unaffected.)*
- The high-volume borderline set `tests` / `maintainability` / `correctness` / `edge-cases` stays
  advisory: high validity but rev_rr < 0.6, and blocking them at the ~0.30 priority mode would be
  high-friction (e.g. `tests` n=1838). Consistent with plan-review's "confident-but-ignored" class.

**Keep advisory (FP-prone — high decider drop-rate):** `docs` (76% dropped), `supply-chain`
(64%), `scope-intent` (58%). High drop = the Pass-3 decider judges most findings non-actionable.

**Not LLM-threshold-tunable (deterministic / attestation gates):** `bugfix-size-attestation`,
`secret-detection`, `sec` — posture is fixed by the detector/attestation, not a priority threshold.

> **CORRECTION (A, continued) — `sec` does not belong in this list.** It is an LLM synonym of
> `security` (an ordinary threshold-tunable criterion, already blocking at 0.54), not a
> deterministic or attestation gate; see the correction under the proposal table. The list is
> correct for `bugfix-size-attestation` and `secret-detection`.

## Caveats (must hold before flipping in production)

1. **Held-out adjudication is the gold standard.** As the code-v2 derivation deferred `security`
   to a sibling's held-out confirmation, a content-based (human/Sonnet) adjudication of the
   would-block sets above should confirm before flipping any criterion to blocking. The
   validity>=0.95 filter is a strong proxy, not a substitute.
2. **rev_rr is a weaker signal here.** Only 280/1109 changes have >1 revision, and the finder
   rewords findings across runs; criterion-load-delta on surfaced findings mitigates but does not
   eliminate the confound.
3. **Block-impact is retrospective** — what the thresholds *would* have blocked on already-reviewed
   changes; forward behavior may differ as the diff mix shifts.

## Data-hygiene finding (discovered work)

Several finding `criteria` labels are NOT in `criteria_routing.json`: `sec`, `documentation`,
`logic`, `clarity`, `consistency`, `concurrency`, `robustness`, `resource-management`,
`behavior-change`, `project.review-phase-boundaries`. These are model-emitted free-text aliases
that fall through to the default 0.95 posture and cannot be individually routed (`sec`≈`security`,
`documentation`≈`docs`). The reviewer prompt should constrain emitted criteria to the registry
vocabulary, or these silently bypass per-criterion routing.

> **CORRECTION (D) — the paragraph above is both mis-characterized and under-inclusive, and
> its recommendation is REJECTED.** The list compares labels to the PACKAGED routing index
> rather than the EFFECTIVE vocabulary, and even on its own terms omits `maintainability`,
> `correctness`, `edge-cases` and `bugfix-size-attestation` — none of which are in the
> packaged index either. Almost nothing here is "free-text alias sloppiness":
> `project.review-phase-boundaries` is a first-class project criterion activated (and tuned)
> in this repo's `.rebar/criteria_routing.json` overlay; `bugfix-size-attestation` is a real
> criterion (`bugfix_size_gate.py`); `maintainability`/`correctness`/`edge-cases` and the
> other small labels are prompt-specified OPEN dimensions whose 0.95 default is the intended
> mostly-advisory v1 posture, not a bypass. Only `sec` and `documentation` are genuine
> synonyms of packaged criteria, and those are now normalized before routing
> (`registry.normalize_criteria`, ticket d890-e711-156e-444b). Constraining the reviewer
> prompt/schema to a fixed registry vocabulary is REJECTED: the dimension vocabulary is open
> by design and a closed enum would make project-supplied criteria unexpressible.
>
> **CORRECTION (E) — the generator reproduces (A) and (C) on every re-run.**
> `docs/experiments/calibrate_code_review_thresholds.py` reads no routing index at all
> (neither the packaged `criteria_routing.json` nor the project overlay), so re-running it
> reproduces the `sec` DET/ATTEST misclassification and the `project.review-phase-boundaries`
> 0.95 row verbatim. Fixing the generator is deferred; until then, read its output against
> the EFFECTIVE vocabulary and these corrections.
