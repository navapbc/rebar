# ADR 0035 — Code-review impact is a two-lane, tier-tagged, severity-first MAX

**Status:** Accepted (epic foliaged-merry-collie — review-gate impact redesign / story albite-lazy-barb)
**Date:** 2026-07-07

## Context

The four-pass review kernel computes a finding's `impact ∈ [0,1]` from the Pass-2 verifier's
coarse severity attributes, and `priority = validity × impact` drives the block/advisory decision.
The **plan-review** gate replaced the kernel's mean `impact` with a severity-first MAX
(`impact_plan`, ADR-adjacent story `fishable-apivorous-redhead`) dispatched via a per-gate
`impact_fn` parameter on `pass3_decide`. **Code review** still used the kernel mean `impact` —
`mean(prod_impact, debt_impact, blast_radius, likelihood, reversibility)`.

That mean structurally **mis-measures** code-review findings:

- A genuine maintainability landmine — a feature left silently dead in prod because its wiring is
  untested — scores `prod_impact: none` and `reversibility: easy`, and those low axes **average
  the finding down to ~0.4**, below any useful bar.
- **Landmines and nits overlap** on the mean scale, so no scalar threshold separates
  "block-worthy" from "coaching nit". Raising the bar drops landmines; lowering it floods on nits.

Code review's severity space is genuinely two-dimensional (a **production/correctness** axis and a
**maintainability/latent-regression** axis) with consequences that do not average — a serious data
loss and a minor style nit are not "one medium finding".

## Decision

Introduce `decide.impact_code`, dispatched into `pass3_decide` via `impact_fn=impact_code` from
`code_review_decide` (plan-review keeps `impact_plan`; a caller that passes no `impact_fn` gets the
mean, byte-unchanged). The model is a **two-lane, tier-tagged, severity-first MAX**:

- **Consequence binaries, one lane + one tier each.** The Pass-2 verifier
  (`code_review_verification_model`, a `CodeSeverityAttrs` that EXTENDS the base five) emits a
  closed set of boolean consequence binaries. Each is assigned EXACTLY one lane and one tier:
  `minor=0.3`, `moderate=0.6`, `serious=0.9`.
  - **Production lane:** `data_loss_without_recovery` (0.9), `security_bypass_not_enforced_elsewhere`
    (0.9), `silent_wrong_feeding_a_decision` (0.9), `capability_degraded` (0.6).
  - **Maintainability lane:** `unversioned_published_contract_break` (0.9),
    `safety_net_removal_without_replacement` (0.9), `contract_drift` (0.6), `hidden_invariant`
    (0.6), `implicit_coupling` (0.3), `dead_code` (0.3).
- **Lane severity = MAX of the TRUE binaries' tiers** (no dilution, no cross-binary compounding —
  a conservative default; a minor/moderate binary alone cannot reach the block zone).
- **Per-lane multipliers.**
  - `prod_lane = prod_sev × trigger_likelihood_mult`, where `trigger_likelihood ∈
    {common:1.0, sometimes:0.6, rare:0.25}`. Absent ⇒ `common` (1.0) so a serious correctness
    finding is never silently dampened by missing metadata.
  - `maint_lane = maint_sev × freq_mult`, where `freq_mult = 0.5 + 0.5·min(churn90,30)/30`.
    `churn90` (90-day commit count for the finding's file) is DET-enriched; absent ⇒ 0 ⇒ 0.5.
- **Detection amplifier.** `amp = 1.0` if the finding is silent (`silent_failure` OR
  `escapes_automation`), else `0.8`.
- **Gated reversibility floor.** `impact = max(min(1.0, impact_base × amp), reversibility_floor)`,
  where `impact_base = MAX(prod_lane, maint_lane)` and the floor is `0.6` **only when
  `impact_base > 0` AND** the change touches a hard-to-reverse surface (a one-way door: released
  packaging — `pyproject.toml`/`setup.py`/`setup.cfg`/`CHANGELOG`; a serialization/schema artifact —
  `.proto`/`.sql`/`schema*.json`/`*.schema.json`; or a **deletion**). The `impact_base > 0` gate is
  deliberate: it lifts a GENUINE defect on a one-way-door surface to ≥0.6 but never MANUFACTURES
  impact for a clean/no-consequence finding that merely happens to touch that file.

### Where the DET signals are merged (the subtle bit)

`pass3_decide` reads its attrs from the **verification** dict
(`verification.get("severity_attributes")`), NOT from the finding dict. So `code_review_decide`
DET-enriches (`churn90`, `hard_to_reverse_surface`) into **`reshape.verifications[i]["severity_attributes"]`**
— the exact dict `impact_code` receives — BEFORE calling `pass3_over_findings`. Writing to the
finding's own `severity_attributes` would silently no-op (impact_code would see only defaults). The
enrichment is best-effort: any git/path failure leaves the signal at its safe default (churn 0 ⇒
freq_mult 0.5; surface False ⇒ no floor).

## Consequences

- The old `0.33` attribute floor is dropped; separation now comes from the tier values +
  multipliers, co-calibrated on a labeled fixture (`tests/unit/fixtures/code_review_impact_labels.jsonl`):
  `median(impact_code[HIGH]) − median(impact_code[NIT]) > 0.30` with `median(NIT) < 0.30`. In
  practice a maintainability landmine reaches ~0.7–0.9 and a nit ~0.1–0.2.
- The verifier now emits a **larger** sub-answer set. An older/absent verifier (every binary
  `False`, `trigger_likelihood` absent) **ABSTAINS** — every lane is 0, impact is 0 — so a stale
  verifier never inflates.
- `trigger_likelihood` is named apart from the base `likelihood` to avoid a Pydantic
  field-override collision (the base `CodeSeverityAttrs` already declares `likelihood`, mapped by
  `_LIKE01`); the two are semantically distinct (trigger frequency vs. harm likelihood).
- The absolute block threshold that turns this separation into postures is co-calibrated by the
  rollout child (`raptorial-galloping-dragon`) via a diff-grounded A/B; this ADR fixes only the
  **shape** of the scale, not its operating point.
