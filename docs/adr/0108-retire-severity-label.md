# ADR 0108 — Retire the severity label; priority + blocking/advisory is the canonical signal

**Status:** Accepted (epic `pink-complex-xenurine` / REB-1486)
**Date:** 2026-08-30

## Context

Every finding produced by the plan-review / code-review kernel (`pass3_decide` in
`review_kernel/decide.py`) carries a `severity` string derived from **impact alone**:

```
severity_label(imp) = critical if imp >= 0.75, major if >= 0.5, minor if >= 0.25, else none
```

The same finding also carries `priority = validity x impact`, the value the kernel's own
BLOCK/ADVISORY decision actually uses (`priority >= block_threshold`). Because `severity`
ignores `validity`, the two signals do not agree: a high-impact/low-validity finding can be
labeled `critical` while a moderate-impact/high-validity finding — the one that actually
blocks — is labeled `minor`. This is a foot-gun: a reader (or a downstream gate) reasonably
assumes the more alarming label is the more important finding, when it may rank below the
"minor" one on the metric the kernel itself decides with.

Two duplicated vocabularies compound the confusion. The kernel emits a 4-value vocabulary
(`critical | major | minor | none`); the older `findings.py` module (from commit `b501c38d1`,
predating the four-pass kernel) defines a 5-value vocabulary
(`critical | high | medium | low | info`). A byte-identical `_KERNEL_TO_COMMON_SEVERITY` map
is duplicated in `src/rebar/llm/code_review/shim.py` and `src/rebar/review_bot/adapter.py`
solely to translate one into the other before a downstream consumer (which clamps any unknown
string to `"info"`) sees it. This translation caused a real field misread: the same
finding surfaced as `INFO` then `MEDIUM` across identical runs because only its `impact`
(not its actual priority) drifted across the `major`/`minor` boundary.

Three real consumers currently read `severity`, not `priority`:

- The Gerrit `LLM-Review` comment (`review_bot/adapter.py`).
- Cross-finding aggregation/dedup, which picks the highest-`severity` representative and
  sorts the merged list by it (`aggregate.py`'s `_severity_rank`).
- The `fail_on_severity` workflow gate policy (`workflow/steps.py`'s `GATE_POLICIES`), which
  fails a step by exact string membership against the 5-level vocabulary.

Two DET-tier (deterministic, non-LLM) finding sources — the secrets detector
(`code_review/detectors.py`) and the bugfix-size gate (`code_review/bugfix_size_gate.py`) —
hardcode a `severity` string directly and never run through `pass3_decide`, so they carry no
computed `priority`/`validity`/`impact` today.

## Decision

Findings surface **`priority`** (the existing numeric `validity x impact`) plus a
**`blocking`/`advisory`** boolean/decision tag as the one canonical signal, everywhere a
human or a gate reads a finding. The `severity` field is removed from the finding shape
entirely — this is a **BREAKING** change to the two JSON schemas that declare it `required`
(`completion_verdict.schema.json` and `review_result.schema.json`, both via
`common.schema.json`'s shared `$defs/finding`), and is recorded as a documented BREAKING
entry in `docs/release-notes.md` and `CHANGELOG.md` per `docs/api-stability.md`'s policy for
required-field removal.

**DET-tier findings get fixed `priority` constants** at their hardcode sites (the secrets
detector's always-blocking finding is pinned above every configured `block_threshold`; the
bugfix-size gate's finding gets a slightly lower fixed value) rather than being retrofitted
through `pass3_decide` — they are deterministic pass/fail checks, not LLM-scored judgments,
and a fixed priority preserves their existing always-block behavior while letting them
participate uniformly in priority-based ranking and gating.

**`severity_label()` itself is kept**, but decoupled from the Finding shape: it remains an
internal impact-bucketing helper used by calibration/eval code
(`test_divergence_grade_split.py` and related instrumentation), which needs a coarse
impact bucket independent of any per-finding field. What changes is that its output is no
longer stamped into a finding's `severity` key.

**Human-facing surfaces show blocking/advisory only — no raw priority number, no severity
word.** The Gerrit comment and the CLI text renderers (`_cli/_llm_commands.py`'s plan-review,
code-review, and completion-verdict renderers) print `BLOCKING`/`ADVISORY` (or lower-case
`advisory`) per finding. Priority stays available internally for ranking and gating, but is
not rendered per finding in the human-facing text — this keeps the comment/CLI output simple
while still fixing the actual bug (mis-ranking) at its source.

### Migration sequence

Landed as five independently reviewable changes, in this order (each depends on the
previous — a later change's plan review is only valid once its prerequisite is closed):

1. `uncheering-uncivil-pig` — assign fixed `priority`/`decision` to DET-tier findings
   (secrets detector, bugfix-size gate).
2. `beaming-snively-urial` — rank aggregation (`aggregate.py`) by `priority` instead of the
   ordinal `_severity_rank`; thread `priority` through `code_review/shim.py`'s
   `_to_common_finding`.
3. `patriotic-abdicative-arrowana` — surface `blocking`/`advisory` (not severity) in the
   Gerrit review-bot comment and the three CLI renderers; delete the duplicated
   `_KERNEL_TO_COMMON_SEVERITY`.
4. `camlet-exodermal-kodiakbear` — migrate the `fail_on_severity` workflow gate policy to a
   `priority_threshold`, reusing `review_kernel.decide.DEFAULT_BLOCK_THRESHOLD` as the
   `default` policy's threshold rather than a second hardcoded number.
5. `byzantine-spinelike-penguin` — drop the `severity` field from the finding shape
   entirely: `findings.py`'s `SEVERITIES` vocabulary and Pydantic field,
   `common.schema.json`'s `$defs/finding.severity` (the BREAKING schema change), and the
   now-fully-vestigial `BLOCKING_SEVERITIES` config in `review_bot/config.py`. This change
   also adds the required `docs/release-notes.md`/`CHANGELOG.md` BREAKING entry.

## Consequences

- The motivating bug is fixed: aggregation, the Gerrit comment, and the workflow gate all
  key on the same `priority` metric the kernel's own BLOCK decision uses, so a
  high-impact/low-validity finding can no longer outrank a genuinely higher-priority one.
- `completion_verdict` and `review_result` consumers that read `finding.severity` as a
  required field must update to read `priority`/`decision` instead — announced as the one
  BREAKING entry in `docs/release-notes.md`/`CHANGELOG.md` (step 5 above). Every other
  schema referencing findings (`gate_input`, `gate_output`, `code_review_verdict`,
  `plan_review_verdict`, `validate_report`) does not declare `severity` required, so removal
  there is schema-compatible.
- `severity_label()` survives as an internal calibration/eval helper (impact bucketing),
  unaffected by the Finding-shape change — its own existing tests
  (`test_severity_label_buckets`, `test_divergence_grade_split.py`) are expected to keep
  passing unchanged.
- DET-tier findings (secrets detector, bugfix-size gate) gain a `priority`/`decision`
  vocabulary consistent with LLM-scored findings, closing the gap that let them bypass
  priority-based ranking and gating.

## Amendment (2026-08-31, during implementation)

Decomposition-time research corrected the scope above on three points, all discovered by the
plan-review gate rather than assumed at authoring time:

1. **Step 2's target was dead code.** `aggregate.py::aggregate_findings`/`_severity_rank` has no
   caller anywhere in `src/rebar/` — orphaned since ADR 0075 retired the single-pass code-review
   route. The live cross-finding dedup (`_cluster_findings` in `code_review/workflow_ops.py`,
   `dedup_key`/`suppress_and_dedup` in `review_kernel/decide.py`) picks a representative
   first-in-order, never by severity or priority, so there was no live misranking bug to fix
   there. `beaming-snively-urial`'s actual landed scope is narrower: thread `priority`/`decision`
   through `code_review/shim.py::_to_common_finding` for the `review_result`/`review_code()`
   consumer only. `aggregate.py` is untouched by this epic.
2. **Step 4's target (`steps.gate`/`fail_on_severity`) is not wired into any real production
   gate.** All three real gate workflows (plan-review, code-review, completion-verification) use
   their own bespoke `_decide`/`_reconcile` ops instead; `steps.gate` is exercised only by a
   retained sample workflow and a test fixture, both severity-only. The landed change is
   therefore additive (a new `priority_threshold` alongside the existing `fail_on_severity`,
   never a replacement) rather than a migration of live behavior.
3. **Step 5 ("drop the `severity` field entirely") was too broad.** `findings.py`'s shared
   `SEVERITIES` vocabulary and `normalize_finding` clamp are NOT removed — they serve an
   entirely separate completion-verification module cluster (`completion_reconcile.py`,
   `completion_child_gate.py`, `epic_bug_screen.py`, `workflow/gate_ops.py`,
   `workflow/completion_verdict_assembly.py`) that hardcodes `severity` as its only signal, with
   no `priority`/`decision`/`impact`/`validity` concept anywhere in that call path — and
   `spec_scan.py`'s independent LLM reviewer, likewise severity-only. Deleting the shared
   vocabulary would have broken both with no fallback. The landed change instead: (a) stops
   `pass3_decide` from stamping `severity` at any of its return sites; (b) stops
   `code_review/shim.py::_to_common_finding` from defaulting an absent severity to a literal
   (deletes the now-unneeded `_KERNEL_TO_COMMON_SEVERITY`/`_SEVERITY_DEFAULT`, passing through
   an explicit value unchanged and omitting the key when absent); (c) changes
   `findings.py::normalize_finding` to only clamp to `"info"` when a `severity` key IS PRESENT
   but holds an unrecognized value — never backfilling `"info"` when the key is genuinely
   absent (confirmed safe: no production caller or existing test relied on that backfill); (d)
   relaxes (not removes) `severity` from `common.schema.json`'s shared `finding.required` list.
   Because `completion_verdict` findings never pass through the review-kernel and always supply
   `severity` in practice, this relaxation has no observable effect on `completion_verdict`
   consumers, despite `completion_verdict.schema.json` and `review_result.schema.json` sharing
   the same `$defs/finding` definition.

The core decision — findings surface `priority` + `blocking`/`advisory` as the canonical
signal, retiring the impact-only severity label as a decision-adjacent signal — is unchanged.
Only the blast radius of removing the *field* itself narrowed once the actual (not assumed)
call graph was traced.
