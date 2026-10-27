# DSO-grounded criterion specifications (mined from ~/digital-service-orchestra)

Fully-specified review logic behind the plan-review gate's Layer-2 criteria, extracted from DSO's
skills/agents/prompts (read-only). Each carries its exec tier, the concern/facet it examines, the real
binary checklist DSO uses, severity rule, and anti-FP discipline, with `file:line` citations under
`~/digital-service-orchestra/plugins/dso/`. These replace the bare-bones one-liners used in the first
experiment round; the single-turn ones are compiled into `criteria_v2.json`.

## Single-turn / 2-step judgment tier

### F1 — Measurability & in-session completability  [1-TURN · facet: ac-text-quality]
Checks each criterion: (a) observable outcome not effort/subjective; (b) evaluable in-session (repo/CI/deterministic cmd), not post-sprint-only (multi-day telemetry/adoption% ≤2); (c) durable end-state vs one-time transition (litmus); (d) right-sized milestone. SEVERITY: outcome-vague or post-sprint = MAJOR. ANTI-FP: evaluate spec-as-written; observability tooling is valid work. SRC: agent-clarity.md:24-28, verifiable-sc-check.md:11-66, scope.md:23.

### F4 — User/problem present (value)  [1-TURN · facet: scope-intent]
(a) names specific user + problem; (b) criteria = observable improvement not internals; (c) not a bare technical task; (d) ≥1 validation mechanism. ANTI-FP: implied technical consumer counts for internal/cleanup/dep work (N/A allowed). SRC: value.md:24-25,42-48.

### E1 — Coherence + terminology + duplicates  [2-STEP · facet: coherence]
DD→SC coverage map then cross-check: (a) every criterion maps to described work and vice-versa; (b) verify-cmd references the same entity the text names (cycle vs review_cycle); (c) no dup requirements; (d) migrations verify removal+replacement. ANTI-FP: cite-or-omit; prefer AMBIGUOUS over hand-waved FAIL. SRC: completeness.md:25-36, story-decomposer.md:199,270,436, verdict-rubric.md:51-67.

### E2 — Ambiguity / executable-without-clarification  [1-TURN · facet: ac-text-quality]
6-signal scan (undefined scope / implicit AC / conflicting signals / missing persona / unstated constraints / ambiguous priority) + scope-bullet placeholder check ("verify whether/check if/TBD/figure out" / deferred design choice). SEVERITY: blocks-planning = MAJOR; defaultable = MINOR. unsatisfiable-SC → REPLAN_ESCALATE. SRC: implementation-plan/SKILL.md:289-321, brainstorm/SKILL.md:244.

### E3 — Intent fidelity  [2-STEP · facet: scope-intent]
Blind-restate then compare to title: (a) body matches headline, no scope drift; (b) each goal has end-state-observable proof; (c) no contradiction; (d) acknowledges callers depending on changed behavior. SEVERITY: builds-wrong-thing = CRITICAL. ANTI-FP: mixed signals → AMBIGUOUS; "no impl found yet" ≠ contradiction. SRC: intent-search.md:127-178, verifiable-sc-check.md:68-93.

### E5 — Testing-plan completeness  [1-TURN · facet: testing]
(a) beyond happy-path (failure/timeout/invalid + caller contract); (b) boundary scenarios; (c) observable outcomes; (d) RED test per unit w/ dep edge; (e) changed/deleted behaviors get modify/remove tests; (f) meaningful assertions, isolated deps, no tautology/source-grep. SEVERITY: new flow happy-path-only = MAJOR. ANTI-FP: structural greps are valid tests; TDD exemptions exist. SRC: testing.md:23-27, tdd.md:31-86, test-quality.md:16-20.

### E6 — Verification/termination + end-state reachability  [2-STEP · facet: ac-text-quality]
claim→proving-command then union-reaches-end-state: (a) each claim has a proving command; (b) no hedges (should/probably/seems); (c) every criterion maps to a step; (d) union of steps reaches the end-state; (e) user-flows have e2e or rationale. ANTI-FP: universal lint/test cmds don't prove a specific criterion. SRC: verification-before-completion/SKILL.md:13-43, completeness.md:24-38.

### G5 — Decomposition judgment  [1-TURN · facet: scope-intent]
(a) sizing: >3 files OR ≥3 layers OR new interface OR low scope-certainty → too big; (b) epic single-concern test, multi-persona/UI+backend/migration/>6 SC → needs children; (c) decomposition present where size demands; (d) leaf small enough for one session; (e) YAGNI/Rule-of-Three (≥3 call-sites for new abstraction). ANTI-FP: incidental "and" ok; file list is a sample. SRC: complexity-evaluator.md:80-253, complexity-gate.md:26-114, scope.md:23,42.

## Triggered overlays (router selects relevance)

### T5a Performance [1-TURN · overlay-perf] — trigger: new I/O/data/LLM/compute/batch/shared-resource. latency target / resource_efficiency (no N+1) / scalability (limits, concurrency). <4 cites Big-O. SRC: performance.md:22-64.
### T5b Reliability [1-TURN · overlay-reliability] — trigger: new failure points/writes/state. error_handling (retry/circuit/degradation) / failover (idempotent, no data loss). blast_radius lowers only. SRC: reliability.md:22-70.
### T5c Security [1-TURN · overlay-security] — trigger: endpoints/data/PII/auth. access_classification (declared level; ambiguity=failure) / data_protection (encryption, no secret-logging). <4 cites OWASP. SRC: security.md:22-58.
### T5d Accessibility [1-TURN · overlay-a11y] — trigger: new user-facing UI (else null). wcag_compliance / inclusive_ux. <4 cites WCAG criterion. SRC: accessibility.md:22-58.
### T5e Maintainability [1-TURN · overlay-maintainability] — trigger: crosses boundaries/new rules/patterns. coupling_risk / changeability (configurable) / documentation (ADR). SRC: maintainability.md:22-66.
### T6 UX non-happy-path [AGENT · overlay-ux] — trigger: UI keyword ≥3 (or classifier). criticality / non_happy_path / flow_entry_exit (free-text probes). SRC: ux-probe-set.md:7-35, ui-keyword-trigger.md:5-36.
### T7 Documentation [AGENT · overlay-docs] — trigger: codebase-health pass. freshness (no stale/deprecated refs) / completeness (TODO budget) / navigability (TOC). SRC: documentation.md:14-61.
### T8 LLM/prompt antipatterns [AGENT · overlay-llm] — trigger: divergent LLM/agent behavior. 17-pt taxonomy + 5 RCA probes (Gold Context/Closed-Book/Perturbation/Sycophancy/State-Check); minimal-fix + pink-elephant audit. SRC: bot-psychologist.md:49-166.
### T1 Prior-art / novel-arch [AGENT (web) · overlay-priorart] — trigger: 6 bright-lines (external-integration/unfamiliar-dep/security-auth/novel-pattern/perf-scalability/migration) + agent-judgment. SRC: epic-scrutiny-pipeline.md:163-203.

## Agent (tool-using) tier — codebase-grounded

### E4 — Assumption/premise verification  [AGENT · facet: codebase-grounding]
TOOLS: per assertion, MUST Grep/Read to confirm; cached knowledge not a substitute; .md contract → read named impl file. CHECKLIST: scan hedging ('assume/likely/should be') + confident-assertion ('loaded from/reuses/already in repo') triggers; verify each; timeout→unverifiable=gap. SEVERITY: unverifiable = BLOCKS; fail-CLOSED on absent evidence. SRC: epic-scrutiny-pipeline.md:65-86, second-source-verifier.md:122.

### G1G2 — Edit-set / scope accuracy  [AGENT · facet: codebase-grounding]
TOOLS: Glob/Grep each named file/symbol to confirm existence; Grep consumers OUTSIDE artifact dir; emit (path,line,covered) tuples. CHECKLIST: named targets exist; every consumer covered_by_SC; no cross-task conflict/rename-of-referenced; behavioral hunks in/ambiguous/out-of-scope (CREATION→out). ANTI-FP: STOP if scope vague; high-confidence only. SRC: scope-drift-reviewer.md:32-122, epic-scrutiny-pipeline.md:88-116, gap-analysis.md:40-65.

### A1 — Anti-slop / over-engineering / NIH  [AGENT · facet: codebase-grounding]
TOOLS: Grep to COUNT existing call-sites of a proposed abstraction; Grep for rebuilt functionality (NIH); check config for existing key. CHECKLIST: Rule-of-Three (≥3 existing sites); YAGNI (current done-def); Dependency (>30 lines / correctness-security); NIH; no config-proliferation/golden-hammer/cargo-cult. ANTI-FP: cite concrete evidence; Justified-Complexity needs affirmative evidence; sandbagging prohibition. SRC: complexity-gate.md:26-114, approach-decision-maker.md:113-127.
