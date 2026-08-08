# ADR renumbering — 2026-08 collision resolution (story 0743)

`docs/adr/` had accumulated **17 duplicate numbers across 22 files** (one number, `0037`, was shared by five unrelated ADRs). Numbers are an identity, so duplicates make every bare `ADR NNNN` citation ambiguous. This change makes ADR numbers a **bijection**: one number ↔ one ADR, enforced by a per-number marker file (`docs/adr/.numbers/NNNN`, whose add/add git conflict blocks a duplicate claim at merge time) and a CI backstop (`scripts/check_adr_numbers.py`).

**Resolution rule.** Within each collision group the **earliest-landed** ADR keeps the number (git-history landing order, full history — not the shallow-clone graft point); the others were reassigned to the next free band **0070–0091** in ascending landing order. No ADR content changed except a one-line provenance header (moved ADRs get a *Renumbered* note; kept ADRs get a *Number note* recording the former ambiguity window).

## Old → new number mapping

| Old # | New # | ADR (slug) | Kept at old # (earliest-landed) | Landed |
|------|------|------------|----------------------------------|--------|
| 0007 | **0070** | `0007-jira-onboard-config-write.md` | `0007-review-bot-receiver.md` | 2026-06-29 |
| 0007 | **0071** | `0007-editing-prompt-contracts-from-the-editor.md` | `0007-review-bot-receiver.md` | 2026-06-29 |
| 0008 | **0072** | `0008-convergent-plan-edit-re-review.md` | `0008-secrets-source-ssm.md` | 2026-06-29 |
| 0009 | **0073** | `0009-reopen-invalidation-validity-on-read.md` | `0009-review-bot-pipe.md` | 2026-06-30 |
| 0010 | **0074** | `0010-code-review-overlay-escalation.md` | `0010-gerrit-github-replication.md` | 2026-06-30 |
| 0011 | **0075** | `0011-retire-single-pass-code-review.md` | `0011-github-mirror-lock.md` | 2026-06-30 |
| 0012 | **0076** | `0012-code-review-secrets-security-detectors.md` | `0012-iac-foundations.md` | 2026-06-30 |
| 0015 | **0077** | `0015-code-review-voter-four-pass-rewire.md` | `0015-project-supplied-criteria.md` | 2026-06-30 |
| 0016 | **0078** | `0016-plan-review-container-leaf-scrutiny.md` | `0016-project-det-invariants.md` | 2026-07-01 |
| 0026 | **0079** | `0026-autodeploy-on-box-timer.md` | `0026-reconciler-three-way-merge-baseline.md` | 2026-07-03 |
| 0031 | **0080** | `0031-schema-derived-typeddicts.md` | `0031-reconciler-ref-lock.md` | 2026-07-04 |
| 0032 | **0081** | `0032-adjective-adjective-animal-aliases.md` | `0032-pass2-graded-subanswer-vs-prompt-gloss.md` | 2026-07-06 |
| 0035 | **0082** | `0035-code-review-two-lane-tier-tagged-impact.md` | `0035-rc2b-snapshot-horizon-safe-replay.md` | 2026-07-07 |
| 0035 | **0083** | `0035-reconciler-vendor-adapter-seam.md` | `0035-rc2b-snapshot-horizon-safe-replay.md` | 2026-07-08 |
| 0036 | **0084** | `0036-acli-429-rate-limit-backoff.md` | `0036-impact-model-rollout-and-calibration-cadence.md` | 2026-07-08 |
| 0037 | **0085** | `0037-reconciler-live-validation-harness.md` | `0037-oss-contribution-intake-posture.md` | 2026-07-09 |
| 0037 | **0086** | `0037-cross-ticket-overlap.md` | `0037-oss-contribution-intake-posture.md` | 2026-07-09 |
| 0037 | **0087** | `0037-transport-retry.md` | `0037-oss-contribution-intake-posture.md` | 2026-07-09 |
| 0037 | **0088** | `0037-code-review-novelty-convergence.md` | `0037-oss-contribution-intake-posture.md` | 2026-07-09 |
| 0038 | **0089** | `0038-async-liveness-watchdog.md` | `0038-governance-artifacts.md` | 2026-07-09 |
| 0040 | **0090** | `0040-main-fast-forward-only-submit.md` | `0040-exit-11-block-but-retryable.md` | 2026-07-10 |
| 0041 | **0091** | `0041-llm-review-carry-trivial-rebase.md` | `0041-llm-diagnostic-sanitization.md` | 2026-07-10 |

## Per-reference resolution table

Every live `adr/NNNN-slug` reference in `docs/` and `src/` that named a moved ADR was rewritten to its new number. All were **slug-exact** (the citation carried the unique slug, so the target ADR is unambiguous — the highest-confidence tier). Referential integrity is now enforced by `scripts/check_adr_numbers.py` (a dangling `adr/*-slug` link fails CI).

| Reference site | Old target | New target | Tier |
|----------------|-----------|-----------|------|
| `docs/adr/0009-review-bot-pipe.md` | `0007-editing-prompt-contracts-from-the-editor.md` | `0071-editing-prompt-contracts-from-the-editor.md` | slug-exact |
| `docs/adr/0009-review-bot-pipe.md` | `0007-jira-onboard-config-write.md` | `0070-jira-onboard-config-write.md` | slug-exact |
| `docs/adr/0009-review-bot-pipe.md` | `0008-convergent-plan-edit-re-review.md` | `0072-convergent-plan-edit-re-review.md` | slug-exact |
| `docs/adr/0055-jira-family-sub-seam.md` | `0035-code-review-two-lane-tier-tagged-impact.md` | `0082-code-review-two-lane-tier-tagged-impact.md` | slug-exact |
| `docs/adr/0055-jira-family-sub-seam.md` (×11) | `0035-reconciler-vendor-adapter-seam.md` | `0083-reconciler-vendor-adapter-seam.md` | slug-exact |
| `docs/config.md` | `0037-transport-retry.md` | `0087-transport-retry.md` | slug-exact |
| `docs/plan-review-gate.md` | `0008-convergent-plan-edit-re-review.md` | `0072-convergent-plan-edit-re-review.md` | slug-exact |
| `docs/reuse-surface.md` | `0037-code-review-novelty-convergence.md` | `0088-code-review-novelty-convergence.md` | slug-exact |
| `docs/review-kernel.md` | `0037-code-review-novelty-convergence.md` | `0088-code-review-novelty-convergence.md` | slug-exact |
| `docs/workflow-authoring-v2.md` (×2) | `0007-editing-prompt-contracts-from-the-editor.md` | `0071-editing-prompt-contracts-from-the-editor.md` | slug-exact |
| `src/rebar/_engine/rebar_reconciler/adapters/__init__.py` | `0035-reconciler-vendor-adapter-seam.md` | `0083-reconciler-vendor-adapter-seam.md` | slug-exact |
| `src/rebar/_engine/rebar_reconciler/adapters/jira/__init__.py` | `0035-reconciler-vendor-adapter-seam.md` | `0083-reconciler-vendor-adapter-seam.md` | slug-exact |
| `src/rebar/_engine/rebar_reconciler/adapters/jira_family/__init__.py` | `0035-reconciler-vendor-adapter-seam.md` | `0083-reconciler-vendor-adapter-seam.md` | slug-exact |
| `src/rebar/_engine/rebar_reconciler/adapters/jira_family/rich_text.py` | `0035-reconciler-vendor-adapter-seam.md` | `0083-reconciler-vendor-adapter-seam.md` | slug-exact |
| `src/rebar/schemas/gen_types.py` | `0031-schema-derived-typeddicts.md` | `0080-schema-derived-typeddicts.md` | slug-exact |

## UNRESOLVED

Bare-number citations (`ADR NNNN` with **no** slug) are not mechanically rewritable — a bare `ADR 0037` cannot be bound to one of the five former 0037 ADRs by string alone. In **live docs** every surviving bare-number citation now resolves correctly to the ADR that *kept* the number (the earliest-landed), so none were left dangling. In **immutable git history** (commit messages and `rebar-ticket`/ADR trailers on already-landed commits) bare-number citations that historically meant a since-moved ADR **cannot be rewritten** and are recorded here as historically ambiguous. Of the ~90 historical commit references surveyed at design time, ancestry + co-landing + topic-matching resolve all but **one**, which resists confident human resolution and is retained UNRESOLVED:

- **1 UNRESOLVED** historical commit citation of a former collision number whose intended ADR cannot be determined from context; left as-is in immutable history. Future readers should consult this table and the target ADRs' *Number note* headers to disambiguate.

_Generated as part of story 0743; the mapping above is authoritative._
