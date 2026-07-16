# AI Task Decomposition: State of the Art (2026) and What It Means for rebar's Plan-Review Gate

**Status:** research report + gap analysis + recommendations · **Ticket:** `englacial-communal-conure` (d39b-8c45-385c-418a) · **Compiled:** 2026-07-15

## Sources and provenance

This report synthesizes three streams of evidence:

1. **Independent deep research** (this session): four parallel research passes over (a) 2024–2026 empirical research with data and code, (b) the actual prompt/template files of 15 OSS decomposition systems (read at file level, quotes verbatim), (c) plan-critique / LLM-judge / false-positive research, and (d) rebar's own plan-review implementation and store history.
2. **Two operator-supplied reports** (pinned inputs; not committed):
   - `ai-task-decomposition-report.md` — sha256 `e85aaac27ad511782772acb54f71e344806d38c08179f8e923eca458f8bd7d93`
   - `AI Task Decomposition Best Practices.md` — sha256 `32cc59125cb02dead4225f122c49792117a30c2ba2549a0d35c73afc19fd4a66`
3. **This repo's plan-review history**: REVIEW_RESULT sidecars and ticket event streams in the rebar store (quantified in §5).

Confidence labels: **[verified]** = fetched from primary source this session; **[preprint]** = arXiv/workshop, not peer-reviewed; **[unverified]** = claim appears in an input report or search snippet but could not be confirmed at the primary source — treated as a lead, not a fact.

Claims from the operator-supplied reports that we could NOT verify and therefore do not rely on: the CMU thesis's exact "feature-based / component-based / step-based" pattern names (taxonomy exists, exact terminology unconfirmed); SWE-Adept's quantitative results; SlopCodeBench's specific percentages; the MASFT "41.8% specification failures" category split (secondary sources only); the second operator report's "OpenClaw vs Hermes" ecosystem framing (vendor/SEO-heavy sourcing).

---

## 1. What the research actually shows (data-first)

### 1.1 Decomposition helps — when the decomposer is good and retries are isolated

- **CMU MS thesis CMU-CS-25-132** (Zhijie Xu, 2025, advisors Rosé/Hilton) **[verified]**: integrating a task-decomposition component into SWE-agent (a fine-tuned Qwen3-1.7B decomposer, trained on tasks from 10 Apache projects, **capped at 12 subtasks per level** — the 85th percentile of the human dataset) improved SWE-bench Verified resolution **+24%** over the non-decomposed baseline. Caveats: MS thesis (not peer-reviewed); trained on Java, evaluated on Python. The empirically-derived subtask cap — not a fixed universal number — is the transferable finding.
- **Runtime-Structured Task Decomposition** (IBM/Zoom, arXiv:2605.15425) **[verified, workshop preprint]**: on two agentic workloads, **static decomposition was *worse* than a monolithic run** (Kubernetes RCA: 1,632 vs 904 retry tokens) because a failure cascades a rerun of every downstream subtask. Runtime-structured decomposition — typed, schema-validated subtask outputs with failure-isolated retries — cut retry cost **51.7% vs monolithic and 73.3% vs static**. Decomposition pays only when failed work can be isolated and retried alone.
- **CodePlan** (Microsoft, FSE 2024) **[verified]**: dependency-graph-driven "chain of edits" for repo-wide changes (2–97 files) passed validity checks on **5/7 repositories vs 0/7** for planning-free baselines. Inter-file dependency *ordering* — not just task listing — is what mattered.
- **Agentless** (FSE 2025) **[verified]**: a *fixed* three-phase pipeline (localize → repair → validate), with no agentic planning at all, hit **32.0% on SWE-bench Lite at $0.70/issue** — then the best result at the lowest cost among 26 agent-based systems. For issue-shaped tasks, a rigid, well-chosen decomposition beats open-ended planning.

### 1.2 Where agents actually fail (what a plan reviewer should look for)

- **Specification defects dominate.** The MASFT study (Berkeley, arXiv:2503.13657) **[verified]** analyzed 151 multi-agent traces (κ=0.88): improving specification and verification prompts lifted ChatDev correctness from 25% to **34.4–40.6%**. AgentErrorTaxonomy (arXiv:2509.25370) finds **early planning errors compound** — they are the primary reliability bottleneck. In the one domain with a hard number, **goal-specification uncertainty accounted for 64.1% of failures** vs 32.1% for environment uncertainty (Robotouille, arXiv:2502.05227) **[snippet]**.
- **Over-action is a first-class failure.** FixedBench (ETH/LogicStar, arXiv:2605.07769) **[verified]**: on 200 tasks whose correct answer is *no code change*, SOTA agents proposed undesirable changes on **35–65%**. A plan that doesn't admit "justified no-op" as an outcome invites fabricated work.
- **Coordination is expensive by default.** CooperBench (Stanford/SAP, arXiv:2601.13295) **[verified]**: 652 collaborative tasks; agents were **~30% *less* successful working together than solo** ("curse of coordination"). But structure rescues it: CAID's dependency-aware asynchronous delegation gained **+26.7pp (PaperBench) / +14.3pp (Commit0)** over single-agent (arXiv:2603.21489), and Shepherd's live supervision raised CooperBench pair-coding pass rates **28.8% → 54.7%** (arXiv:2605.10913). Parallelism pays only with isolated workspaces, stable contracts, and a coordinator.
- **Environment and setup are load-bearing.** GitTaskBench (AAAI 2026): best system solves only **48.15%** of real repo-leveraging tasks, with setup/baseline failures prominent. SetUpAgent (ICML 2025): environment setup and issue detail materially move success (up to 60% swings). A plan with no baseline/reproduction step starts blind.
- **Task-length is the strongest single difficulty predictor.** METR Time Horizon 1.1 (Jan 2026) **[verified]**: 50%-success horizon doubling every ~3 months (since-2024 fit), R²≈0.83 between task length and success — but the horizon is a *statistical* 50% bar on unusually clean tasks, explicitly not a delegation-safety threshold.

### 1.3 Reviewing plans without drowning in false positives

- **The trust cliff is ~10% FP.** Google Tricorder (ICSE 2015 / CACM) **[verified, production-validated]**: analyzer findings must stay under **~10% effective false positives** or developers dismiss and then disable the check; Google operates at **<5%**. This is the calibration bar for any blocking plan finding.
- **Two-stage find-then-verify is the best-evidenced FP mitigation.** CORE (FSE 2024): a ranker/verifier stage after the proposer **reduced FPs 25.8%**. Tencent's LLM4PFA triage classified true vs false static-analysis alarms at 93–94% accuracy on streams that were >76% false **[snippet]** — at recall cost, so demote unverified findings to advisory rather than dropping them.
- **Deterministic spec checks have a known ceiling.** AQUSA (Lucassen et al., *Requirements Engineering* 2016; 1,023 user stories, 18 companies) **[verified]**: rule-based user-story quality checks achieved **93.8% recall / 72.2% precision**. Structural defects (missing AC, non-atomic stories, duplicates) are cheaply detectable; semantic ambiguity is where precision dies.
- **Ambiguity ≠ failure; incompleteness and inconsistency = failure.** A REFSQ 2010 industrial study (40 projects) found requirement ambiguity did *not* correlate with project success — humans disambiguate in-flight. But incomplete/underspecified/inconsistent requirements rank top-5 among project-failure causes (NaPiRE mapping, *REJ* 2022). Inference (unsourced but load-bearing): an LLM executor *guesses* instead of asking, so ambiguity matters more for agent consumers than the human-era literature suggests — yet the blocking weight should still sit on incompleteness and contradiction, with vagueness as coaching.
- **Judge mechanics.** Absolute rubric scoring, never pairwise (order-swap flipped 82.5% of pairwise verdicts — Wang et al., ACL 2024); ~3-vote self-consistency captures most of the available gain (+4pp, saturating); judges are systematically overconfident (arXiv:2508.06225); verdict-style judges under-flag while open-ended finding generators over-flag — so generate for recall, verify for precision.
- **Nobody has published a plan-gate eval.** Claude Code plan mode, Cursor plan mode, Devin's planning subagent, Amazon Q, and spec-kit's `/speckit.analyze` all ship a plan-approve gate; none publishes effectiveness data. The justification everywhere is (a) planning failures dominate and (b) plan-time fixes are cheap. Instrumenting a gate against downstream outcomes — which rebar's sidecar history enables — is ahead of the published field.

---

## 2. What the OSS community has converged on (from the prompt files themselves)

Fifteen systems' actual template/prompt files were read this session (GitHub Spec Kit ~120k★, OpenSpec ~61k★, Kiro, claude-task-master ~28k★, BMAD-METHOD ~51k★, SuperClaude, Agent-OS, Claude Code docs, Anthropic engineering posts, OpenAI ExecPlans, Aider, OpenHands ~81k★, obra/superpowers ~252k★, ai-dev-tasks, Cursor plan mode). None cites the others; the convergence is independent.

**Convergence set (appears nearly everywhere):**

1. **A staged pause between plan and execution.** Spec Kit's approval gate, Agent-OS "approve/adjust", ai-dev-tasks' literal "Go", Cursor's editable plan, Claude Code plan mode, OpenHands' plan→approve→generate. rebar's plan-review claim gate is this pattern, with an LLM reviewer standing in for the human.
2. **Dependencies as a first-class, checkable field — a DAG, not a flat list.** claude-task-master's parse-prd prompt: *"Set appropriate dependency IDs (a task can only depend on tasks with lower IDs)"* — a DAG by construction. Spec Kit's `[P]` marker: *"[P]: Can run in parallel (different files, no dependencies)"*. Kiro *"builds a dependency graph of the tasks in your tasks.md and groups independent tasks into waves."*
3. **Atomicity defined by reviewability, not size.** obra/superpowers (`skills/writing-plans/SKILL.md`): *"A task is the smallest unit that carries its own test cycle and is worth a fresh reviewer's gate. […] split only where a reviewer could meaningfully reject one task while approving its neighbor."* OpenSpec: *"small enough to complete in one session."* BMAD (`create-next-story.md`): story context must be complete enough that *"the dev agent should NEVER need to read the architecture documents."* cursorrules: *"independently completable […] cannot be broken down further meaningfully."*
4. **Verification as a required structured field.** claude-task-master requires `testStrategy` per task; ExecPlans (`PLANS.md`) requires *"Explicit acceptance: state behaviors, commands, and observable outputs that prove success"*; Anthropic: *"Each evaluation prompt should be paired with a verifiable response or outcome."*
5. **Concrete anchors per task.** Spec Kit's command prompt shows literal format enforcement: `✅ CORRECT: - [ ] T012 [P] [US1] Create User model in src/models/user.py` / `❌ WRONG: … Create model (missing file path)`. Anthropic's multi-agent post: *"Each subagent needs an objective, an output format, guidance on the tools and sources to use, and clear task boundaries."*
6. **Self-contained task packets for a context-poorer executor.** Aider's architect prompt: *"The editor engineer will rely solely on your instructions, so make them unambiguous and complete."* ExecPlans: *"a self-contained, living specification that a novice can follow."*
7. **Living plans.** ExecPlans' required sections include Progress, Surprises & Discoveries, Decision Log — the plan is expected to change during execution, and the change is recorded, not silent.

**Contested (genuine disagreement — a reviewer should not enforce either side):**

- **Parallel-by-default (Spec Kit, Kiro waves) vs sequential-by-default (BMAD: "CRITICAL: NEVER automatically skip to another epic").**
- **Machine-checkable task grammar (Spec Kit) vs editorial prose (OpenSpec: "fluid not rigid").** Martin Fowler's documented critique of Kiro — a small bug turned into *"4 'user stories' with a total of 16 acceptance criteria […] a sledgehammer to crack a nut"* — is the canonical over-decomposition counter-example. Over-decomposition is a real, documented failure mode, not merely a taste issue; it also matches the IBM finding that static decomposition can be worse than no decomposition.
- **Numeric sizing rubrics: almost nobody has one.** Only ExecPlans (">about an hour" triggers a plan) and BMAD's loose story-count bands. The research agrees: METR explicitly warns against reading horizons as task-sizing rules; the CMU cap (12) was derived per-corpus. Universal LOC/hour thresholds are not supported by any evidence found.

**OpenSpec's split heuristic** (docs/writing-specs.md, "Right-size the change") is worth quoting in full because it is the cleanest statement of decomposition smells in the survey:

> "A good change has one intent you can say in a sentence. […] Signs a change is too big: The proposal's scope reads like a list of unrelated features. / Reviewing it would take an afternoon, so nobody will. / Two people couldn't work on it without colliding. / Half the tasks could ship on their own."

---

## 3. The synthesized operating model (merging the two input reports with verified research)

The two operator-supplied reports and this session's research triangulate to the same core (differences noted below):

1. **The unit of work is a bounded, independently verifiable, reversible vertical slice of behavior** — not a fixed size. Size by uncertainty, coupling, observability, reversibility; never by elapsed-hours alone.
2. **Specify outcomes before implementation**: observable end-state, constraints/invariants, non-goals, and the exact evidence that counts as done (commands, not "verify it").
3. **Dependency-aware plan, not a flat checklist**: ordering, interface seams, contested resources, and safe parallel branches made explicit. Parallelize only sibling nodes with stable contracts, disjoint write sets, isolated workspaces, and a named integration owner (CooperBench/CAID/Shepherd).
4. **Separate knowledge work from production work**: unresolved architectural/product questions become time-boxed spikes whose output is a decision + revised downstream plan — never hidden inside a feature task (ProjDevBench's design-failure category; the input report's Step 4).
5. **Baseline/environment work is a first-class task** with deliverable evidence (commands, versions, pre-existing failures) — GitTaskBench/SetUpAgent.
6. **A no-op gate**: "verify the change is actually needed; a justified no-op or docs/test-only outcome is acceptable" (FixedBench).
7. **Keep the plan alive**: re-plan on discovery; record decisions and surprises next to the work (ExecPlans; the "static plan as commandment" antipattern; IBM's static-worse-than-monolithic result).
8. **Verification is part of the task contract, and the oracle must be independent** of the implementer where risk warrants (test-deletion/reward-hacking failure modes; the second input report's "deny the implementing agent write access to test files").
9. **Judge the review system by actionable-finding rate under a hard FP budget** (Tricorder <10%), with structure: deterministic checks first, high-recall LLM finding pass second, strict verification pass before anything blocks, coaching for the rest.

Where the input reports diverge from verified evidence: the second report's TDD/ZOMBIES and "regenerative software" sections are directionally consistent with Beck/Fowler practice but rest largely on vendor/SEO sources; its SWE-bench Pro verifier-error table (32.5% combined error) could not be verified at the primary source this session and is treated as a lead. The first report is broadly verified — its headline numbers checked out (§1) with the caveats listed in Sources.

---

## 4. Gap analysis: what rebar's plan-review gate already measures vs the SOTA

rebar's gate is a four-pass pipeline (pass 1 find → pass 2 verify → pass 3 deterministic decide → pass 4 coach) over a deterministic floor (P1–P9) and 37 LLM criteria, advisory-by-default with 11 blocking-eligible criteria, an advisory cap of 20, per-criterion thresholds, and signed attestations consumed by the claim gate. Findings below are grouped by pass, respecting the separation of concerns: **pass 1 changes = what can be *found*** (criteria/rubrics), **pass 2 changes = how findings are *validated and scored***, **pass 4 changes = what coaching a surviving finding earns**. Pass 3 is pure arithmetic and is treated as fixed.

### 4.1 Where the gate already matches or leads the SOTA

| SOTA practice | rebar today |
|---|---|
| DET-first, LLM-second dual gate (AQUSA→LLM hybrid) | DET floor P1–P9 before LLM tiers |
| Find-then-verify FP mitigation (CORE −25.8% FP) | Pass 1 recall-first finder → pass 2 skeptical verifier → validity<0.5 drop |
| Absolute rubric scoring, never pairwise | Per-criterion rubrics, typed binary sub-answers |
| Blocking rare + evidence-cited (Tricorder) | 11/37 blocking-eligible, thresholds 0.6–0.75, vetoes on inaccurate citation/refuted absence |
| Atomicity/single-outcome check | G5 (blocking): unit bundles >1 independently-releasable outcome |
| Dependency cycle soundness | P5 task-DAG (blocking on cycle) + sibling file-impact interference (advisory) |
| Container coverage / cross-child interaction | G3 (4-bucket AC coverage audit), G4 (7 interaction modes) |
| Codebase grounding of named files/symbols | G1G2/E4 (blocking, agentic Grep/Read verification) |
| Verification-as-contract | E6 (proving command per completion claim), P6 verify-command lint |
| Spike separation for risky assumptions | T2 (advisory) + pass-4 moves 1 (spike) and 4 (riskiest-assumption) |
| Oversize/reviewability bound | P4 heuristics + P8 token-budget block |

This is an unusually complete implementation of the published consensus — including several things (signed attestations, sidecar history, novelty floors) the field hasn't published at all.

### 4.2 Gaps (each becomes a recommendation in §6)

1. **Dependency *ordering and readiness* is checked much more weakly than the OSS convergence demands.** P5 blocks only on cycles; sibling interference is advisory and file-impact-based. Nothing checks the claude-task-master/Kiro-style property: does each child's dependency set actually reflect its stated inputs (a task consuming an interface defined in a sibling with no ordering edge)? G4's "dependency gap" mode partially covers this but only at container review, and only as one of seven modes competing in one agentic call.
2. **No layer-cake / vertical-slice signal.** The single strongest decomposition-shape consensus (thin vertical slices over horizontal layer splits; CodePlan's integration-truth argument) has no criterion. A container whose children are "DB task / backend task / frontend task" passes G5 (each child is one releasable outcome by its own text) and G3 (the union covers the parent ACs).
3. **No baseline/environment-task check.** GitTaskBench/SetUpAgent-grade evidence says missing reproduction/baseline steps are a dominant real-world failure; no criterion asks "does this plan establish a green baseline / reproduce current behavior before changing it?" (T3 verifies named surfaces exist; that's different.)
4. **No no-op gate.** FixedBench's 35–65% over-action result has no counterpart: nothing asks "does the plan demonstrate the change is actually needed (current behavior reproduced, existing implementation searched)?" — closest is removal-rationale (Chesterton's Fence), which covers only removals.
5. **Identifier/contract drift across children.** superpowers' most concrete check — *"a function called clearLayers() in Task 3 but clearFullLayers() in Task 7 is a bug"* — is only obliquely covered by G4's "conflicting assumptions" mode and COH's intra-document contradictions.
6. **Post-claim plan drift is invisible to the gate's feedback loop.** The material-fingerprint machinery detects drift for attestation staleness, but nothing *learns* from it: a description that had to change materially after claim is direct evidence the review missed something, and the sidecar history to measure this already exists (§5).
7. **Coaching moves lack decomposition-shape vocabulary.** The move registry has "thin vertical slice" (move 7) but no move for "extract a baseline task", "add an ordering edge / stabilize the contract before parallelizing", "merge over-split tasks" (the Fowler/Kiro failure mode), or "add a no-op/necessity check".
8. **Calibration is possible but not yet closed-loop.** Sidecars record priority/decision per finding with an IMPACT_MODEL_VERSION stamp, but there is no offline job correlating findings (or their absence) with downstream signals (post-claim edits, reopen events, force-closes, completion-verifier FAILs) — the eval the published field lacks and this store can uniquely produce.

---

## 5. Evidence from this repo's own plan-review history

Method: full event history recovered from git objects across all refs of the `tickets` branch (15,802 unique event files; the on-disk worktree is heavily compacted — 1,019 snapshots). Plan reviews identified by the presence of `coverage.det` (P1–P9) in REVIEW_RESULT sidecars; reconciler-authored edits excluded. Caveats: sidecar persistence began 2026-06-23, so reviews before that left no events; claim timestamps are unrecoverable for some compacted tickets (the 505 denominator undercounts); the earliest sidecars store criterion ids but no finding text.

### 5.1 The gate's throughput and shape

- **1,897 plan reviews on 527 tickets** (2026-06-23 → 2026-07-15). Verdicts: **PASS 892 (47%) · BLOCK 854 (45%) · INDETERMINATE 151 (8%)**. Only 48 reviews (2.5%) were completely clean.
- Mean **12.9 findings surfaced per review** (median 13, max 54); **30% of candidate findings are dropped** by pass 2/3 before surfacing (10,450 dropped vs 24,382 surfaced) — the find-then-verify FP mitigation is doing real work.
- **68% of reviewed tickets went ≥2 rounds** (mean 3.6, median 2, max 21); of 247 tickets that ever hit BLOCK, **227 (92%) subsequently reached PASS** — the block→remediate→re-review loop converges.
- Top criteria by surfaced findings: T8 (undefined decision rules) 3,132 · G1G2 (grounding accuracy) 2,386 · G6 (mechanism unspecified) 2,326 · E4 (overclaimed feasibility) 1,760 · E1/E2/E6 (AC↔intent gaps, untestable ACs, proving-command-proves-nothing) ≈4,780 combined. The evidence family (E1+E2+E4+E5+E6 ≈ 9,800) and grounding family (G1G2+G6+G4 ≈ 7,600) dominate.

### 5.2 The post-claim edit signal (a proxy for review misses)

Of **1,256 work tickets**, 505 have an observable first transition into `in_progress`. **16 (3.2%)** had a post-claim, pre-close description edit by a human/agent — and **15 of the 16 were substantive**: discovered constraint / premise invalidated (3), scope reduction (3), approach change (3), plan authored entirely post-claim (2), AC strengthened / advisory remediation recorded (2), ACs re-tagged `[operator-attested]` for the close gate (2), cosmetic (1).

Cross-referencing the 8 cases that had persisted plan reviews:

| Judgment | Count | Cases |
|---|---|---|
| **MISSED** (review could have caught it) | 3 | dc58 (planned sign step used a helper `completion.py` doesn't have; pipeline redesigned mid-flight), db7b (planned retention via compaction, which by rebar's own contract never absorbs reducer-ignored events), 5886 (bug — gate-exempt, nobody looked; the planned new alert already existed in `fetcher.py`) |
| **CAUGHT-BUT-IGNORED** (advisory surfaced pre-claim, applied post-claim) | 4 | c8cc (5 advisories applied by finding id — after claim), f5df (E6 flagged the weak proving method; the CI drift-check fix came post-claim), 115b + 8c4f (AC verifiability flagged; `[operator-attested]` re-tagging waited until the close gate forced it) |
| **UNKNOWABLE** (emergent, no reviewer could foresee) | 1 | 3006 (deployment-phase re-scope; the review had in fact already flagged the AC-ownership gap) |

Three structural conclusions:

1. **Both clean misses are the same defect class: asserted-capability grounding.** The plan asserted that an *existing* module provides a capability it does not have. This is nominally G6/E4/T3 territory — those criteria fired on both tickets — but the probes did not check the specific named module for the specific asserted capability.
2. **The dominant leak is advisory latency, not blindness.** In 4 of 8 cases the gate found the problem and the finding was applied only after claim (twice only when the close gate forced it). Detection improvements alone won't fix this; the loop from advisory → plan change needs tightening.
3. **The exempt path is a blind spot with a measured cost.** The one bug in the sample produced a MISSED verdict precisely because bugs skip plan review entirely.

---

## 6. Recommendations

Each recommendation is scoped to a single pass (pass 1 = what can be found; pass 2 = how findings are validated/scored; pass 4 = what coaching survivors earn — pass 3 stays pure arithmetic), and carries (a) a falsifiable experiment with a pass/fail condition and (b) a false-positive assessment against the history in §5. Calibration bar throughout: blocking findings must stay under the Tricorder ~10% effective-FP cliff; new criteria ship advisory-first per the existing overlay discipline.

### R1 — Pass 1: asserted-capability grounding probe (extend G6/E4 rubrics, agentic)

**What:** When a plan asserts that an existing, named component provides a specific capability the plan will consume ("reuse X's signing helper", "compaction will bound the sidecar", "the fetcher's alert covers this"), the finder must treat the *capability*, not just the file, as the reference to ground: probe the named module for the asserted function/behavior and raise a finding when it is absent or contradicted. Today G1G2 grounds file/symbol *existence* and E4 grounds *claims*, but the two clean MISSes show asserted-capability-of-existing-module slips through both.

**Falsifiable experiment:** (i) Retrospective: run the amended rubric on the frozen plan texts of dc58, db7b, and 5886 — pass condition: ≥2 of 3 produce a surfaced finding naming the absent capability. (ii) FP control: run it on 30 randomly sampled historical plans that PASSed and closed with *zero* post-claim description edits — fail condition: >3 of 30 (10%) gain a new *blocking* finding, or the median new-advisory count rises by >1 per review. The `fidelity_spot_eval` / `production_batch_runner` seams already support batch re-runs.

**FP assessment vs history:** E4/G6 are already the #3–#4 firing criteria with a 30% pass-2 drop rate absorbing over-reach; the amendment narrows scope to *named existing component + asserted consumed capability*, a pattern with a checkable ground truth (the code), so pass-2 vetoes (cited-reference-inaccurate) remain effective. Risk is duplicate findings with G1G2 — mitigate via the existing cohort/dedup machinery.

### R2 — Pass 1 (DET): operator-attested evidence-kind lint (extend P6)

**What:** Deterministic lexical check: an AC whose completion evidence is inherently operational (matches a lexicon: deploy/prod/live/console/vote/drill/dashboard/E2E-against-real-…) but is not tagged `[operator-attested]` gets an advisory finding pointing at the ADR-0043 tag. Two of the 16 post-claim edits (115b, 8c4f) were exactly this re-tagging, done late under close-gate pressure.

**Falsifiable experiment:** Run the lexicon over all historical ACs (recoverable from event history). Pass condition: (i) the 115b/8c4f pre-edit ACs are flagged; (ii) on a random sample of 50 flagged ACs, ≥70% are judged genuinely operator-attestable by a human rater (AQUSA's 72% precision is the deterministic-check benchmark); (iii) flag rate over all ACs stays under ~5% so P6 stays quiet on ordinary plans.

**FP assessment vs history:** zero-LLM, advisory-only, and P6 already fires 1,261 times without complaint; a mis-flag costs one advisory line. The known-failure lexicon can be tuned offline against the full historical corpus before shipping — this is the cheapest recommendation with a directly observed failure pair.

### R3 — Pass 1: decomposition-shape criterion for containers (layer-cake and dependency-consumption checks)

**What:** A container-scope criterion (sibling to G3/G4) with two checks drawn from the strongest OSS/research consensus: (i) **vertical-slice check** — if children partition by architectural layer (schema/backend/frontend/tests) such that no single child yields an end-to-end observable outcome, raise a finding (CodePlan's integration-truth argument; the universal layer-cake antipattern); (ii) **consumption-vs-ordering check** — if child B's text names an artifact/interface that child A creates and no `depends_on`/`blocks` edge or explicit sequencing exists, raise a finding (claude-task-master's lower-ID-only DAG; Kiro's waves; currently only one of G4's seven competing modes). Advisory-only; explicitly instructed that sequential-by-default and parallel-by-default are both acceptable (contested in the community — §2) so it flags *inconsistency*, not style.

**Falsifiable experiment:** Seeded-flaw eval: take 10 real historical epics that closed cleanly; generate mutated variants — re-sliced into layer-cake children, and with one ordering edge deleted where consumption exists. Pass condition: ≥7/10 mutants flagged, ≤1/10 originals flagged (i.e., ≥87% discrimination). This reuses the review machinery's batch runner; mutation is mechanical.

**FP assessment vs history:** G5 (389 findings) and G3/G4 (355/555+769 incl. dropped) show container criteria fire at moderate, absorbable rates. Genuine horizontal splits (infra epics, migration epics, refactor sweeps) are common in this store — the rubric must except containers whose parent intent is itself layer-scoped, and history provides a labeled corpus (epics whose children all closed without cross-child rework) to tune against before activation.

### R4 — Pass 1: necessity / baseline gate for behavior-change plans

**What:** For plans that fix or change existing behavior (bugs, behavior-change stories): does the plan (i) reproduce or cite the current behavior (a command, a failing test, an observed log), and (ii) state why existing mechanisms don't already satisfy the goal? FixedBench: agents propose undesirable changes on 35–65% of already-satisfied tasks; case 5886 in our own history planned a duplicate of an alert that already existed. Pairs with a policy change worth piloting: **a lightweight, DET+grounding-only review tier for bugs** (currently fully exempt), since the one bug in the post-claim sample was a MISS by exemption.

**Falsifiable experiment:** (i) Retrospective: rubric applied to 5886's frozen plan must surface the existing `fetcher.py` alert (its plan named the module; an A1-style grep is sufficient). (ii) Prospective A/B over one month: for bug/behavior-change tickets, measure the rate of post-claim "approach change / discovered existing mechanism" edits and of review findings later judged valid; pass condition: ≥1 confirmed necessity catch with blocking-FP rate on the new criterion ≤10% (Tricorder bar), advisory acceptance (finding applied before claim) ≥30%.

**FP assessment vs history:** additive-feature plans (most of the store) are out of scope by construction; among behavior-change plans the main FP risk is demanding reproduction ceremony for trivially-evident defects — keep it advisory and let pass 2's charitable-reading discipline drop cases where the plan already cites the defect. History check: of the 16 post-claim editors, none was an additive feature flagged for missing "necessity" — the signal appears only where it should.

### R5 — Pass 2: verifier must probe asserted capabilities before validating mechanism findings (agentic verifier default for G6/E4/T3 cohorts)

**What:** Pass 2's charitable-reading discipline is correct for FP control, but in both MISS cases criteria *fired* and the surviving findings didn't carry the decisive fact. Route findings in the G6/E4/T3 cohort through the agentic verifier variant (`plan_review_verifier_agentic`) with one added typed sub-answer: `asserted_capability_confirmed: yes|no|na` — "the plan relies on capability C of existing component X; does X provide C?" A `no` feeds the existing impact axes (`divergent_implementation`), lifting priority for exactly the finding class that later forces mid-flight redesigns. This changes *scoring inputs*, not discovery, so it belongs in pass 2.

**Falsifiable experiment:** Re-run pass 2 (agentic) over the persisted pass-1 findings of dc58 and db7b. Pass condition: at least one finding per ticket scores `asserted_capability_confirmed: no` with priority ≥ the advisory-cap waterline (so it surfaces prominently) — versus the historical runs where the decisive fact never appeared. Cost/FP control: measure verifier token cost and the delta in blocking rate over a 50-review replay; fail if overall blocking rate rises by >3 points (historical base: 45%) without corresponding confirmed-miss coverage.

**FP assessment vs history:** this *raises* precision rather than recall (a `no` requires reading the named module; `insufficient` leaves scores unchanged), so the main risk is cost, not FPs. The 30% drop rate gives headroom: better-grounded verification should convert some dropped-but-true findings into surfaced ones and some surfaced-but-vague ones into dropped.

### R6 — Pass 4: advisory-triage coaching move (attack the dominant leak)

**What:** Advisory latency — found-then-ignored-until-forced — was 4 of 8 assessable cases. Add a move to the registry (rendered deterministically, like all moves): *"Triage each surviving advisory into the plan before claiming: apply it, or record a one-line deferral rationale in the description (see c8cc's 'Plan-review remediations' section as the exemplar pattern)."* Attach it whenever ≥N advisories survive on a passing review (N≈5). This is pure coaching — no detection or scoring change — so it sits squarely in pass 4.

**Falsifiable experiment:** A/B over two months of reviews (the move is deterministic to toggle): measure (i) fraction of PASSing tickets whose description changes between review and claim (advisories applied pre-claim — should rise), and (ii) rate of post-claim edits classified CAUGHT-BUT-IGNORED (should fall from 4/8 toward 0). Pass condition: pre-claim application rate up by ≥50% relative, with no increase in review rounds per ticket (coaching must not induce churn loops).

**FP assessment vs history:** coaching notes are non-gating; the only cost is prompt/registry space. History shows the exemplar already occurs organically (c8cc) — the move institutionalizes the observed best practice.

### R7 — Cross-cutting instrumentation: close the gate-eval loop (prerequisite for measuring R1–R6)

**What:** The published field ships plan gates with zero effectiveness data (§1.3); this store can do better and already has the substrate. Add a small offline job (not a gate change): on every material post-claim description edit, link the edit to the ticket's latest REVIEW_RESULT and append a `gate-eval` record (sidecar-style, reducer-ignored) classifying it MISSED / CAUGHT-BUT-IGNORED / UNKNOWABLE — initially by LLM with human spot-audit, using §5.2's rubric. Report monthly: post-claim material-edit rate (baseline **3.2%**), miss rate among reviewed tickets (baseline **3/8**), advisory-latency rate (baseline **4/8**), blocking-FP proxy (blocked findings the author disputes or that vanish without plan change).

**Falsifiable experiment:** it *is* the experiment harness. Acceptance for the job itself: reproduces §5.2's classification on the 8 historical cases with ≥6/8 agreement against this report's human-judged labels.

**FP assessment:** no reviewer-visible output; zero FP surface. Without it, none of R1–R6's experiments has a durable measurement substrate. Prior art in-repo: `docs/experiments/plan-review-gate/` already holds a threshold-calibration harness (`calibrate_plan_review_thresholds.py`, `ab_impact_model.py`) driven off sidecar history — R7 extends that from *threshold* calibration against adjudicated findings to *outcome* calibration against post-claim drift, and IMPACT_MODEL_VERSION segmentation already exists to keep formula changes comparable.

### Priority order

R7 (measurement substrate, cheap) → R2 (deterministic, directly observed failure pair) → R1+R5 (the two halves of the asserted-capability fix — the only clean misses) → R6 (dominant leak, pure coaching) → R4 (necessity gate + bug-tier pilot) → R3 (decomposition-shape, highest tuning cost).

---

## 7. Selected primary sources

**Empirical research (verified this session unless noted):**
- CMU-CS-25-132, Xu, *Decomposing Complexity* (2025) — http://reports-archive.adm.cs.cmu.edu/anon/anon/usr0/ftp/usr/ftp/2025/CMU-CS-25-132.pdf
- Runtime-Structured Task Decomposition (IBM/Zoom, 2026) — https://arxiv.org/abs/2605.15425
- CodePlan (Microsoft, FSE 2024) — https://arxiv.org/abs/2309.12499 · code: https://github.com/microsoft/CodePlan
- Agentless (FSE 2025) — https://arxiv.org/abs/2407.01489 · code: https://github.com/OpenAutoCoder/Agentless
- SWE-agent (NeurIPS 2024) — https://arxiv.org/abs/2405.15793
- CooperBench — https://arxiv.org/abs/2601.13295 · https://cooperbench.com
- CAID, *Effective Strategies for Asynchronous SE Agents* — https://arxiv.org/abs/2603.21489 · code: https://github.com/JiayiGeng/CAID
- Shepherd — https://arxiv.org/abs/2605.10913 · code: https://github.com/shepherd-agents/shepherd
- FixedBench, *Coding Agents Don't Know When to Act* — https://arxiv.org/abs/2605.07769
- GitTaskBench (AAAI 2026) — https://arxiv.org/abs/2508.18993
- ProjDevBench — https://arxiv.org/abs/2602.01655 · SWE-CI — https://arxiv.org/abs/2603.03823 · SlopCodeBench — https://arxiv.org/abs/2603.24755 **[numbers unverified]**
- MASFT, *Why Do Multi-Agent LLM Systems Fail?* — https://arxiv.org/abs/2503.13657
- AgentErrorTaxonomy — https://arxiv.org/abs/2509.25370 · Robotouille — https://arxiv.org/abs/2502.05227
- Wang et al., *LLMs are not Fair Evaluators* (ACL 2024) — https://arxiv.org/abs/2305.17926 · Zheng et al., MT-Bench judge — https://arxiv.org/abs/2306.05685 · judge overconfidence — https://arxiv.org/abs/2508.06225
- Google Tricorder (ICSE 2015 / CACM) — https://research.google/pubs/tricorder-building-a-program-analysis-ecosystem/
- AQUSA (Lucassen et al., *Requirements Engineering* 2016) — https://link.springer.com/article/10.1007/s00766-016-0250-x
- METR Time Horizon 1.1 — https://metr.org/blog/2026-1-29-time-horizon-1-1/

**OSS prompt/template files quoted in §2 (fetched at file level, July 2026):** GitHub Spec Kit (`templates/tasks-template.md`, `templates/commands/tasks.md`) — https://github.com/github/spec-kit; OpenSpec (`docs/writing-specs.md`) — https://github.com/Fission-AI/OpenSpec; claude-task-master (`src/prompts/parse-prd.json`, `expand-task.json`) — https://github.com/eyaltoledano/claude-task-master; BMAD-METHOD (`bmad-core/templates/story-tmpl.yaml`, `tasks/create-next-story.md`) — https://github.com/bmad-code-org/BMAD-METHOD; obra/superpowers (`skills/writing-plans/SKILL.md`) — https://github.com/obra/superpowers; OpenAI Agents Python `PLANS.md` — https://github.com/openai/openai-agents-python/blob/main/PLANS.md; Aider (`aider/coders/architect_prompts.py`) — https://github.com/Aider-AI/aider; OpenHands — https://github.com/OpenHands/OpenHands; Kiro — https://kiro.dev/docs/specs/; Anthropic engineering — https://www.anthropic.com/engineering/multi-agent-research-system, https://www.anthropic.com/engineering/writing-tools-for-agents; Claude Code plan mode — https://code.claude.com/docs/en/common-workflows; Cursor plan mode — https://cursor.com/blog/plan-mode; Fowler on Kiro/SDD — https://martinfowler.com/exploring-gen-ai/sdd-3-tools.html

## 8. Closing note

The strongest single takeaway from setting this repo's gate beside the 2026 state of the art: rebar's architecture (DET floor → recall-first find → skeptical verify → arithmetic decide → templated coaching, advisory-by-default, signed attestations, sidecar history) independently matches or anticipates every FP-control mechanism the literature validates — and its residual failures are narrow and now measured. The misses aren't about decomposition *shape* (the community's loudest topic) but about **grounding asserted capabilities of existing code** and **latency between finding and plan change**. Improve those two loops, keep blocking under the 10% trust cliff, and instrument the gate against its own store — which no published system currently does.

