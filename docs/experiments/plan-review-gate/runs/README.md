# Plan-review ground-truth corpora (E1 — story `doctrinal-untruthful-vaquita` / `e95e`)

Frozen ground-truth for epic **pastoral-aquatic-viper** (task-decomposition R1–R7). This
directory holds two committed corpora and the scripts that regenerate them:

| Artifact | What it is |
|---|---|
| `outcome_corpus.jsonl` | one row per **reviewed** ticket — the per-ticket outcome signals E2/E3/E6/R7 join on |
| `adjudication_corpus.jsonl` | a stratified sample of plan-review **findings**, double-labeled TP/FP by two independent LLM raters |

**Provenance.** The §5 figures re-verified below are published in
`docs/research/task-decomposition-sota-2026.md` (recovered verbatim from commit
`143d5074e`; landed by this change as the re-verification baseline). All ticket data is
recovered from the **`tickets` orphan branch** of this repo (git objects), not from any
`~/.claude/jobs/*` scratch artifact.

## Regeneration (exact commands)

```sh
# from the repo root, with the local venv active
H=docs/experiments/plan-review-gate/harnesses
R=docs/experiments/plan-review-gate/runs

python $H/mine_outcome_corpus.py                 # -> $R/outcome_corpus.jsonl  (atomic)
python $H/mine_outcome_corpus.py --verify-s5     # print the §5 re-derivation table
python $H/build_adjudication_corpus.py           # -> $R/adjudication_corpus.jsonl (unlabeled)
python $H/adjudicate.py                          # fill rater_a (all) + rater_b (subset) — LLM
python $H/kappa.py $R/adjudication_corpus.jsonl  # inter-rater reliability + validity gate
```

`adjudicate.py` needs `ANTHROPIC_API_KEY` and the `[agents]` extra; the other three are
offline. All writes are atomic (`.tmp` + `os.replace`); the sampler and subset selector
use fixed seeds, so a re-run reproduces the same corpus.

---

## 1. `outcome_corpus.jsonl` — the outcome corpus

**Population:** the **628 reviewed tickets** (any ticket carrying ≥1 `REVIEW_RESULT`
plan-review sidecar — the §5.1 population, grown from 527 since the report was authored).

**Why a git-object walk, not an on-disk scan.** The on-disk `.tickets-tracker/<id>/`
directory is *compacted*: for older tickets, `CREATE` + `STATUS` events are folded into a
`SNAPSHOT` and their event files are **deleted with no `.retired` copy on disk**. Measured
here: **140 of 631 reviewed tickets (22%) have a SNAPSHOT but zero on-disk STATUS files**
— an on-disk scan would silently emit `reopen_count=0` / `post_claim_edit_class="none"` for
~1 in 5 rows. The miner therefore recovers events from git objects
(`git rev-list --objects --all` + `git cat-file --batch`), which returns the folded events
verbatim (e.g. the fully-compacted case ticket `dc58-af7b` yields its 1 CREATE + 2 STATUS +
4 EDIT). A pre-flight floor (≥500 reviewed tickets recovered) halts on a gc'd/pruned repo
rather than shipping a truncated corpus.

### Schema (one JSON object per line)

| field | type | meaning |
|---|---|---|
| `ticket_id` | str | 4-quad id |
| `ticket_type` | str | `epic` / `story` / `task` / `bug` (from the CREATE event) |
| `level` | str | same as `ticket_type` (hierarchy level) |
| `post_claim_edit_class` | str | one of the closed §5.2 vocabulary below |
| `reopen_count` | int | # `STATUS` events with `current_status=="closed" and status=="open"` |
| `force_close` | bool | any `COMMENT` whose body starts `"FORCE_CLOSE:"` |
| `completion_verifier_fail_count` | int | # `COMPLETION_VERDICT` with `schema=="completion_verifier_fail_v1"` (on-disk retention cap 10/ticket) |
| `review_round_count` | int | # distinct `REVIEW_RESULT` uuids in history (git-object count — the TRUE count, may exceed the on-disk retention cap of 50) |
| `had_persisted_review` | bool | `review_round_count > 0` (always true for this population) |

### `post_claim_edit_class` — closed vocabulary (§5.2 taxonomy)

Classified by **diffing consecutive description states** across the first claim→close
cycle (`[first STATUS→in_progress, first STATUS→closed]`); edits in any post-reopen window
are excluded (`reopen_count` is recorded separately). A pure checkbox check-off
(`- [ ]`→`- [x]`) is normalized away so it does **not** read as a plan change. Precedence
(highest wins across the window's deltas):

`plan-authored-post-claim` › `operator-attested-retag` › `ac-strengthened` ›
`substantive-unclassified` › `cosmetic` › `none`.

- **`plan-authored-post-claim`** — an `## Acceptance Criteria` block first appears in a
  post-claim edit (the ticket was claimed with no plan).
- **`operator-attested-retag`** — an `[operator-attested]` tag is newly added post-claim.
- **`ac-strengthened`** — the AC block's content changed (items added/reworded), beyond
  checkbox state.
- **`substantive-unclassified`** — a large non-AC prose change. The semantic §5.2 classes
  (`premise-invalidated` / `scope-reduction` / `approach-change`) are **not fabricated by
  the miner** — they land here for the adjudication pass to split by hand.
- **`cosmetic`** — a small non-AC change.
- **`none`** — no substantive post-claim description edit (or no observable first claim).

### §5 re-verification table (derived vs published — per figure)

Run `mine_outcome_corpus.py --verify-s5` to reproduce.

| §5 figure | published | derived (this corpus) | verdict |
|---|---|---|---|
| reviewed-ticket population (§5.1) | **527** | **628** | **CORRECTED-TO 628** — the store grew since report commit `143d5074e`; every reviewed ticket is a new `REVIEW_RESULT`, so the population only rises. |
| post-claim-edit rate (§5.2) | **16/505 = 3.2%** | **42/503 = 8.3%** (reviewed *work* tickets) | **CORRECTED-TO 8.3%** — §5's denominator is *all* claimed work tickets (mostly unreviewed); the frozen corpus is the *reviewed* subset (503 work tickets, near §5's 505). The 2.6× higher rate is a **methodology difference**: the miner counts *any* AC-content change (checkbox check-offs excluded) mechanically, whereas §5 hand-classified a stricter set. Spot-checked: every sampled `ac-strengthened` is a genuine substantive AC rewrite, not noise. |
| substantive share | **15/16** | **51/52** (reviewed pop.) | **CORRECTED-TO 51/52** — same population/method difference; 1 of 52 edits is `cosmetic`, the rest substantive. |
| class distribution | (per §5.2) | `none` 576, `ac-strengthened` 42, `operator-attested-retag` 7, `substantive-unclassified` 2, `cosmetic` 1 | recorded (mechanical; semantic classes fold into `substantive-unclassified`) |
| **persisted-review subset** (§5.2) | **8** (3 MISSED + 4 CAUGHT-BUT-IGNORED + 1 UNKNOWABLE) | **8** — all 8 case tickets present in the corpus | **AGREES** |
| **3 MISSED** asserted-capability cases | `dc58-af7b`, `db7b-c8fd`, `5886-d028` | all **present**; classes `ac-strengthened` / `ac-strengthened` / `ac-strengthened` | **AGREES** (present + post-claim-edited) |
| **4 CAUGHT-BUT-IGNORED** cases | `c8cc-68b8`, `f5df-0069`, `115b-ceea`, `8c4f-b81c` | all **present**; `substantive-unclassified` / `ac-strengthened` / `operator-attested-retag` / `operator-attested-retag` | **AGREES** (present + post-claim-edited) |
| **1 UNKNOWABLE** case | `3006-e198` | **present**; `ac-strengthened` | **AGREES** (present) — the mechanical class is recorded; §5's "unknowable" is a human verdict the corpus does not overturn. |

> Disambiguation: §5's `db7b` is the story `db7b-c8fd` ("REVIEW_RESULT reducer-ignored
> sidecar"), which shares its short id with the sidecar it discusses; the corpus pins full
> 4-quad ids to avoid ambiguity.

---

## 2. `adjudication_corpus.jsonl` — the finding SAMPLE (UNLABELED)

> **DESCOPED (2026-07-16): finding-level TP/FP LABELS are NOT shipped.** An earlier version carried
> LLM-assigned + human-adjudicated TP/FP labels. That labeling was **invalidated**: the adjudication
> tooling truncated the plan (`PLAN_CAP=6000`) and ran single-pass with **no codebase tools**, while the
> production gate reviews the *whole* plan and grounds 16 criteria *agentically* against real code — so
> ~79% of the labels were contaminated, and the human adjudication inherited the flaw (it adjudicated a
> dispute set selected by the faulty process, anchored on faulty rationales, without independent code
> grounding). A production-faithful grounded re-labeler was prototyped but rejected as duplicative of the
> gate's own persisted sidecar validity/impact and over the human-time budget. The independent
> human-adjudicated gold set is parked as idea `e59d-c078-724d-46e5` for future enrichment. What ships
> here is the reusable, **unlabeled** finding sample only.

**400 findings** sampled from the persisted `REVIEW_RESULT` sidecars: **300 surfaced + 100 dropped**
(floor ≥280 / ≥90), stratified by primary criterion, deterministic (`seed=1729`), deduped by
`(ticket_id, finding_id)`, **text-bearing** (findings whose prose the pre-4e19/e344 lean sidecar never
persisted are excluded). Regenerate: `python $H/build_adjudication_corpus.py`.

### SURFACED vs DROPPED is decided positionally, not by `decision`

`decision` alone can't classify a finding — an overflow-suppressed advisory and a surfaced advisory both
carry `decision="advisory"`. The sidecar concatenates findings in a fixed segment order and
`coverage.counts` gives each segment length:

```
findings = blocking ++ advisory_surfaced ++ advisory_overflow ++ indeterminate ++ dropped
SURFACED = findings[: blocking + advisory_surfaced]         # shown to the agent
DROPPED  = advisory_overflow segment ++ dropped segment     # sidecar-only (suppressed)
```

The **indeterminate** segment sits between overflow and dropped and is **excluded** (an abstain).
Verified store-wide: **0 of ~1900 payloads** have `sum(counts) != len(findings)`.

### Schema (one JSON object per line) — the UNLABELED sample

| field | meaning |
|---|---|
| `finding_id`, `ticket_id` | finding + its ticket |
| `criterion` / `criteria[]` | primary criterion (stratification key) / all cited criteria |
| `source` | `surfaced` \| `dropped` (positional, per above) |
| `decision` / `drop_reason` | the gate's Pass-3 decision / floor (nullable) |
| `finding`, `suggested_fix`, `location`, `severity` | the finding's substance |
| `impact`, `validity`, `priority` | **the gate's own** persisted scores (from the sidecar — NOT a label) |

No `tp_fp` / `rater_*` / `rationale` / gold label is shipped (see the descope note). The gate's own
`impact`/`validity`/`priority` are retained as the gate's self-assessment — useful downstream (E6 re-judges
these findings for self-consistency; it never needed the TP/FP labels).

---

## E6 — judge reliability (self-consistency + order-shuffle)

**Ticket a880** (epic 6982, plan-review calibration). E6 measures whether the plan-review gate's
LLM-as-judge is **reliable** — the same plan, re-judged, lands the same way. It measures
**agreement (reliability), not accuracy against a gold label** (that is why E6 survived the E1
descope: re-judging for self-consistency never needed the TP/FP labels the corpora deliberately
omit). **No `src/rebar` behavior changes** — E6 is measurement over the *existing* gate
(Pass-2 verify → Pass-3 decide → `orchestrator.finalize_verdict`).

Two bounded experiments, each gating a downstream ticket:

### Exp A — self-consistency (gates **R5**)
Re-judges N=50 findings **3× each** and measures agreement on the Pass-3
`decision ∈ {block, advisory, dropped, indeterminate}`. Pass-3 is deterministic given Pass-2, so
all measured variance is the judge's Pass-2 stochasticity.

- **Sample frame:** `adjudication_corpus.jsonl` rows with **criterion in {G6, E4, T3}** — the R5
  cohort (R5 adds a Pass-2 sub-answer routed over exactly this G6/E4/T3 slice). The cohort is
  **74 findings / 66 distinct tickets** (advisory 47 / dropped 20 / block 7), which clears the
  N≥50 floor with 24 rows of headroom.
- **N = 50**, sampled deterministically (seed `0xA880`) from the 74-member cohort, deduped by
  `(ticket_id, finding_id)`. Each finding's ticket plan text is snapshotted **once** (from rebar's
  replay-derived `description`) into `e6_selfconsistency_inputs.jsonl`, so the N-vote harness reads
  only that committed file — **no live-store dependency at judge time**. A finding whose plan is
  unretrievable is skipped and topped up from the cohort remainder to hold N=50.
- **Gate signal:** `self_consistency.pass` → whether R5's new Pass-2 sub-answer
  (`asserted_capability_confirmed`) emits stably enough to be worth adding. `asserted_capability_confirmed`
  does **not** exist yet — E6 records only the *existing* `decision`, never any R5 field.

### Exp B — order-shuffle (gates **R3**)
Re-judges each of N=14 plans under **3 distinct section-order permutations** and measures agreement
on the gate `verdict ∈ {PASS, BLOCK, INDETERMINATE}`. The gate already uses the recommended
mitigation (absolute rubric scoring, never pairwise); E6 empirically confirms the **residual**
order-sensitivity is below floor **before** R3 lands its new (order-exposed) container criterion.

- **Sample frame:** `corpus_sample.json` — the only committed plan-text corpus (the outcome and
  adjudication corpora store no plan text). Of its 19 inputs, **14 carry ≥3 top-level `##` sections**
  (the permutable set); 5 are excluded (one =2, four =1). A plan needs ≥3 sections because a
  2-section plan admits only `2! = 2` orderings, whereas `3! = 6` guarantees 3 distinct permutations.
- **Permutations (deterministic):** the pure helper `permute_sections` yields exactly 3 orderings —
  **permutation 0 = identity** (the plan's original order), permutations 1–2 drawn from
  `random.Random(seed).shuffle` with `seed = int(plan_id.split("-")[0], 16)`, taking the next
  ordering not already selected. Content is preserved verbatim — only the `##` block order changes.
  The concrete `section_order` index lists are committed in `e6_ordershuffle_inputs.jsonl`, so the
  experiment is re-runnable without re-deriving the shuffle.
- **Why the "≥20 container plans" requirement was dropped.** Only 5 of the 19 inputs are containers
  (`has_children=true`: 3 epics + 2 stories), and the run corpora hold exactly one container-type
  ticket — a container-only shuffle would be under-powered and irreproducible. Order-sensitivity is
  a **general** judge property: if the verdict is unstable under section shuffle at large, any new
  criterion (including R3's container criterion) inherits that instability, so a stable
  general-plan result is the **prerequisite** for trusting R3. The 5-plan container subset is
  reported **descriptively only** (`container_subset_descriptive` in the summary), never gated.
- **Gate signal:** `order_shuffle.pass` → prerequisite for trusting R3's container criterion.

### Pre-registered threshold (both experiments)
Fixed **before** the eval in `e6_prereg.json`: **PASS iff Fleiss' kappa ≥ 0.6 AND raw agreement ≥ 0.8.**
kappa ≥ 0.6 is Landis–Koch "substantial"; the raw-agreement co-floor is prevalence-robust because
Fleiss' kappa deflates under the cohort's skewed (advisory-heavy) base rates. `e6_summary.json`
records both figures and the boolean `pass` for each experiment.

### Infra-INDETERMINATE exclusion + retry cap
An **execution failure is not a stable judge outcome**, so it is dropped-and-re-run, not counted:

- **Exp A:** a vote whose Pass-3 `decision == "indeterminate"` (`three_pass.pass3_decide` returns
  this **only** when Pass-2 produced no verdict — error or agentic-no-verdict) is infra → re-run.
- **Exp B:** a `verdict == "INDETERMINATE"` whose `coverage` carries `llm_unavailable` or
  `verify_failed` is infra → re-run; a **genuine judge-INDETERMINATE** (neither flag) is **kept** as
  its own agreement category.
- **Retry cap (never pad):** collect up to 3 substantive votes within a budget of 6 attempts; a
  subject that cannot reach 3 is written to `e6_*_excluded.jsonl` as an explicit excluded row and is
  **never silently padded**.

### Prompts as run
The judge is exercised through the committed harness code, unchanged from production:

- **Exp A** re-uses `harnesses/three_pass.py` — `pass2_verify(..., agentic=True, repo_root=<checkout>)`
  inside a `gate_source.gate_read_root` session, then the deterministic `pass3_decide`. The Pass-2
  system prompt + `PASS2_TOOL`/`GRADED` binary sub-answer schema are those in `three_pass.py`.
- **Exp B** drives the **public** entrypoint `rebar.llm.plan_review.review_plan(ticket_id,
  source="local", repo_root=<checkout>, sign=False, emit_sidecar=False, force=True)` against a
  throwaway `REBAR_TRACKER_DIR` store clone (ticket-store root and code-grounding root are
  separable), so the real tickets store is never written.

### Bounded LLM spend
Deliberately small sub-samples (not the 400-finding adjudication corpus nor the 628-row outcome
corpus): **Exp A = 50 findings × 3 votes = 150 agentic Pass-2 calls** (Pass-3 is deterministic/free);
**Exp B = 14 plans × 3 permutations = 42 full-gate `review_plan` runs**. Infra re-runs add at most
the retry-cap budget on top.

### Files
| file | what |
|---|---|
| `e6_prereg.json` | pre-registration: thresholds + sample frames (written before the eval) |
| `e6_selfconsistency_inputs.jsonl` | Exp A frozen inputs (N=50 findings + snapshotted plan text) |
| `e6_ordershuffle_inputs.jsonl` | Exp B frozen inputs (14 plans × 3 permutation specs) |
| `e6_selfconsistency.jsonl` | Exp A recorded votes (3 substantive Pass-3 `decision`s / finding) |
| `e6_ordershuffle.jsonl` | Exp B recorded verdicts (1 kept `verdict` / plan × permutation) |
| `e6_*_excluded.jsonl` | explicitly excluded subjects (infra-cap or vanished ticket) — never padded |
| `e6_*_agreement.csv` | per-subject agreement tables |
| `e6_summary.json` | the two gate-input verdicts: `self_consistency.pass` → R5, `order_shuffle.pass` → R3 |
| `harnesses/e6_judge_reliability.py` | the driver (build-inputs / run-a / run-b / analyze) |
| `harnesses/e6_metrics.py` | the LLM-free agreement/permutation/exclusion helpers |
| `tests/unit/test_e6_agreement.py` | CI-collectable golden tests for the helpers |

### Results (from `e6_summary.json`)
| experiment | effective N | Fleiss' kappa | raw agreement | pass (κ≥0.6 AND raw≥0.8) |
|---|---|---|---|---|
| Exp A — self-consistency (R5) | 50/50 findings (0 excluded) | **0.874** | **0.96** | ✅ **PASS** |
| Exp B — order-shuffle (R3) | **10/14 plans** (4 excluded) | 0.55 | 0.70 | ❌ **FAIL** (below floor) |

- **Exp A clears both floors decisively** — the Pass-2/Pass-3 judge re-lands the same `decision`
  on 48/50 findings (κ = 0.874, "almost perfect" on Landis–Koch). R5's new Pass-2 sub-answer is
  worth adding: the judge is self-consistent enough to build on.
- **Exp B lands below floor** (κ = 0.55, raw = 0.70) — a real reliability finding, **not** a
  harness error: the gate `verdict` is order-sensitive enough that R3 must **not** rely on it until
  the residual instability is addressed. `order_shuffle.pass = false` blocks R3 as designed. The
  `mean_fired_criteria_jaccard = 0.671` shows the fired-criteria set also drifts under shuffle.
- **Effective N = 10/14 (honest).** Four plans — **`8722-f153-bd26-46d8`, `8bda-cd4b-3459-46da`,
  `d015-8af0-b627-4c18`, `e46e-f886-033d-490b`** — were permutable when the inputs were frozen
  (their plan text is still in `e6_ordershuffle_inputs.jsonl`), but their tickets have since been
  archived/removed from the `tickets` branch, so they are absent from the run-time
  `git clone --branch tickets` store clone (`rebar edit`/`show` → `ticket not found`). The harness
  records each as an explicit `ticket_not_found` row in `e6_ordershuffle_excluded.jsonl` (3
  permutations × 4 plans = 12 excluded rows) and **continues** — it never crashes and never pads a
  missing verdict. The below-floor result is computed over the 10 plans that survived.
