# Plan-review criteria authoring guide

GENERATED from the criteria registry (`python -m rebar.llm.plan_review.registry regenerate-criteria-guide`) — do not hand-edit. One `## <criterion-id>` section per criterion; `rebar explain <criterion-id>` prints a section, and coach deep-links anchor to `#<criterion-id lower-cased>` (the heading slug).

## A1
**Anti-slop / over-engineering / NIH [agent]** — exec:AGENT, advisory, facet:codebase-grounding

For each proposed abstraction/dependency/config, Grep the codebase to check: Rule-of-Three (>=3 existing call-sites or it's premature); YAGNI (serves a current done-definition, not a hypothetical); NIH (doesn't rebuild functionality already in the codebase or an imported dependency); no config-surface proliferation. Every finding cites concrete codebase evidence. ANTI-FP: Justified-Complexity needs affirmative evidence, not absence-of-disqualifier. ALSO screen the full anti-pattern set (DSO decider): golden-hammer (one tool/pattern forced everywhere), cargo-cult (copied without understanding why), resume-driven (trendy tech with no requirement), premature-optimization (optimizing before evidence), in addition to NIH, premature-abstraction/Rule-of-Three, and config-surface-proliferation. ANTI-FP (designated experiment/reference, FP7): before flagging rebuild-vs-extend NIH, check whether the "existing" implementation is a designated experiment/POC/reference — signals: a path under docs/experiments/, a *_poc.* name, or an explicit "reference, not deliverable / POC" designation in the plan or its linked brainstorm. If so, rebuilding it for production is the INTENDED lifecycle — do NOT flag NIH. When production-vs-reference status is ambiguous from location, COACH ("confirm whether the existing impl is production-to-extend or a reference-to-rebuild") rather than ASSERT NIH. SCALE CALIBRATION (G-9 — this constrains YOUR OWN reasoning, not just detection): apply a small-scale default — assume small scale unless the plan cites a scale estimate, a profiling result, or an explicit AC. This is a two-directional bar: raise a scale/optimization finding ONLY when such evidence exists, and equally do NOT demand scale-handling the evidence does not support. Prohibited reasoning (do not interpolate scale upward from the subject matter): "government/enterprise portals typically handle millions of requests" is NOT a usable estimate; scale sensitivity is orthogonal to volume — a sensitive domain does not imply high load.

Checklist:
- A proposed abstraction has >=3 existing call-sites or is premature (cite grep hits).
- Each abstraction serves a current done-definition, not a hypothetical.
- Doesn't rebuild functionality already in the codebase or an imported dependency (grep for it).
- No config-surface proliferation (a config key may already capture the toggle).
- Screen golden-hammer / cargo-cult / resume-driven / premature-optimization (DSO decider set), each cited.

## COH
**Cross-section coherence pass (cross-cutting)** — exec:1-TURN, blocking, facet:coherence

CROSS-CUTTING coherence pass (distinct from E1's criteria<->description check): a single structured scan for CONTRADICTIONS BETWEEN SECTIONS of the plan — e.g. the testing strategy contradicts the decomposition; the sequencing contradicts the declared dependencies; the context/problem contradicts the success criteria; an approach choice contradicts a stated constraint. One pass, not a debate. SEVERITY: a contradiction that would send the implementer in two directions = MAJOR. ANTI-FP: only flag genuine cross-section contradictions, not within-section nitpicks (those belong to E1/E2).

Checklist:
- No contradiction BETWEEN sections (testing vs decomposition; sequencing vs declared deps; context/problem vs success criteria; approach vs a stated constraint).
- ANTI-FP: only genuine cross-section contradictions, not within-section nitpicks (those belong to E1/E2).

## E1
**Criteria↔description coherence + terminology + duplicates** — exec:2-STEP, advisory, facet:coherence

Audit internal coherence of the requirement set (this is naturally a two-pass check: first map each criterion to the described work, then cross-check terminology and duplicates). Binary checks: (a) every acceptance criterion maps to something described in the plan body, and every described deliverable has a covering criterion (no orphan criteria, no uncovered work); (b) terminology is consistent — the same concept is named the same way throughout, and a criterion's verify step references the SAME entity its text names (e.g. 'cycle' vs 'review_cycle'); (c) no duplicate or near-duplicate requirements; (d) for migrations, criteria verify BOTH removal and replacement. SEVERITY: a deliverable with no covering criterion, or a criterion measuring nothing, is MAJOR; terminology drift / near-dup is MINOR. ANTI-FP: cite-or-omit — ground every finding in specific quoted plan text; prefer AMBIGUOUS over a hand-waved FAIL; consumers named in the spec are covered-by-definition. PASS if the set is coherent and non-redundant.

Checklist:
- Every criterion maps to described work AND every described deliverable has a covering criterion (no orphans, no uncovered work).
- Same concept named the same way throughout; a criterion's verify step references the same entity its text names.
- No duplicate or near-duplicate requirements.
- For migrations, criteria verify BOTH removal and replacement.

## E2
**Ambiguity / executable-without-clarification** — exec:1-TURN, blocking, facet:ac-text-quality

Decide whether an executing agent could act on this plan WITHOUT stopping to ask a clarifying question. Run the 6-signal ambiguity scan: (1) undefined scope boundaries ('improve performance' — of what, by how much); (2) implicit acceptance criteria (types/size limits unstated); (3) conflicting signals (title says X, body Y); (4) missing persona (admin vs end-user); (5) unstated constraints (an API with no auth/rate-limit mention); (6) ambiguous priority (essential vs nice-to-have unranked). Plus flag any scope bullet that is a PLACEHOLDER not a decision: contains 'verify whether', 'check if', 'TBD', 'figure out', 'depends on investigation', or defers a real design choice to the executor ('choose an appropriate X'). SEVERITY: an ambiguity that BLOCKS planning ('cannot proceed without this') is MAJOR; a defaultable gap ('assume X unless told') is MINOR. ANTI-FP: never flag something clearly inferrable from the parent epic or an obvious convention. PASS if the plan is executable without clarification.

DEFAULTABLE-GAPS COACHING: a MINOR defaultable gap ('assume X unless told otherwise') is coached as 'state the default in the plan', NOT as 'answer this question' — the productive fix is a stated default the executor can act on, not a round-trip back to the author. OPERATOR-ATTESTED (ADR 0043): a criterion tagged with the exact case-insensitive prefix `[operator-attested]` is NOT ambiguous merely because its 'done' evidence lives outside the codebase; it is met by a recorded attestation (change id / vote / timestamp) — do not flag it on that basis.

Checklist:
- No undefined scope boundaries ('improve performance' — of what, by how much).
- Acceptance criteria / types / size limits are stated, not implicit.
- Title and body agree; no conflicting signals.
- The persona (admin vs end-user) is identified where it matters.
- Constraints stated (e.g. an API's auth / rate-limit).
- Essential vs nice-to-have is ranked.
- No scope bullet is a placeholder ('verify whether','check if','TBD','figure out','choose an appropriate X') deferring a real decision to the executor.

## E3
**Intent fidelity** — exec:2-STEP, advisory, facet:scope-intent

Judge whether the plan faithfully serves its stated title/goal (a blind-restate-then-compare check: first restate what the plan actually does, then compare to what the title promises). Binary checks: (a) the body's work matches the headline intent — no scope drift doing MORE or LESS than the title promises; (b) each non-deferred goal has a faithful, end-state-observable proof; (c) no step contradicts the stated intent; (d) where the plan changes existing behavior, it acknowledges callers that depend on the old behavior. SEVERITY: the plan builds something materially different from its stated intent = CRITICAL (agent will build the wrong thing); partial drift = MAJOR; minor mismatch = MINOR. ANTI-FP: mixed or absent signals → AMBIGUOUS, not a forced FAIL; 'no implementation found yet' is not itself intent-contradiction. PASS if the plan is faithful to its goal.

Checklist:
- Body work matches headline intent — no scope drift doing MORE or LESS than the title promises.
- Each non-deferred goal has a faithful, end-state-observable proof.
- No step contradicts the stated intent.
- Where behavior changes, callers depending on old behavior are acknowledged.

## E4
**Assumption/premise verification [agent]** — exec:AGENT, blocking, facet:codebase-grounding

Scan the plan for assertions about the codebase ('X already exists', 'Y does Z', hedges/confident-assertions) and FORCE a Grep/Read probe per assertion; cached/training knowledge is not a substitute. Fail-closed on absent evidence (unverifiable assertion = gap). ANTI-FP: read the named implementation file before flagging a contract-doc-only claim.

CONFIDENT-ASSERTION SCAN PROTOCOL (G-7a): enumerate the assertion-shaped sentences and probe each — do not eyeball. Trigger frames: "X already {does/handles/returns/supports} Y", "X is {safe/idempotent/atomic/thread-safe}", "there is no X", "X guarantees Y", "X can't/never Z". Each such frame is an empirically-checkable claim: Grep/Read for it and treat an unverifiable one as a gap. A committed element resting on such an unbacked claim is graded in Pass-2 via committed_work_relies_on_unbacked_claim.

SCOPE-EXCLUSION SUB-CHECK (G-4): a descoping claim used to EXCLUDE work ("OUT: X — already exists", "handled by Y", "covered by Z") is where a false premise deletes work invisibly (nothing downstream references it), so probe it like any other assertion. DISCRIMINATION (co-located rule): FIRE on an empirically-checkable / codebase exclusion ("X already exists / is handled in code") that a Grep/Read can and does refute; ABSTAIN-with-coverage on an external-fact exclusion the tools cannot settle ("another team owns X", "the vendor already does Y") — record it as covered-but-unverifiable rather than asserting a gap you cannot ground. THIRD-PARTY SYMBOLS: an existence/capability claim about an INSTALLED dependency's symbol ("library.Thing exists / is importable") is settleable by `resolve_symbol` (the installed environment), not by a Grep of the repo — resolve it there and treat an environment-resolved symbol as verified rather than an unbacked assertion.

Checklist:
- Each codebase assertion ('X exists','Y does Z', hedges/confident-assertions) is verified by a Grep/Read — training knowledge is not a substitute.
- Fail-closed: an unverifiable assertion is a gap (no benefit of the doubt).
- ANTI-FP: read the named implementation file before flagging a contract-doc-only claim.
- SCOPE EXCLUSIONS (G-4): a descoping claim ('OUT: X — already exists / handled by Y / covered by Z') gets a Grep/Read probe like any assertion — a false exclusion deletes work invisibly. FIRE on a codebase exclusion the tools refute; ABSTAIN-with-coverage on an external-fact exclusion they cannot settle.

## E5
**Testing-plan completeness (retuned v7)** — exec:1-TURN, advisory, facet:testing

Assess whether the plan makes the work testable by construction. FIRST apply the applicability gate: fire ONLY if the plan INTRODUCES new logic/behavior that is testable. If the change is internal/mechanical (refactor, rename, config, dep-bump, doc), or testing is explicitly deferred to child tickets, PASS as not-applicable. RAISED BAR (round-4/5 over-fire fix): thin-but-present coverage is PASS, not a finding; only flag when (a) a NEW user-facing flow has happy-path-only tests with no failure/timeout/invalid/empty path; (b) a changed/deleted behavior gets no modify/remove-test work; (c) the SELF-AUTHORED-ORACLE / change-detector anti-pattern is present — tests that snapshot current (possibly wrong) output, tautological tests, or source-greps masquerading as behavioral tests (these lock in the bug; always MAJOR); or (d) the PROXY-VALIDATION anti-pattern — the changed or newly-DEFAULTED risky path is exercised ONLY through a mock/offline substitute that BYPASSES the new behavior (canned/fake agents that never invoke the real boundary, a stub that short-circuits the live call/dependency), so the suite goes green WITHOUT the new path ever running as it will in production. This is highest-leverage for a CUTOVER/MIGRATION that makes a new code path the DEFAULT: an acceptance criterion satisfiable by offline-only coverage that never exercises that defaulted path end-to-end (live) lets the work close green while the live path is broken — always MAJOR; the productive fix is to add a live/end-to-end acceptance criterion for the path being defaulted to. Boundary scenarios (oversized/malformed/non-Latin/back-button) and observable (not 'works correctly') outcomes are rewarded but their absence on an internal change is NOT a finding. ANTI-FP: structural greps and command-output assertions ARE a legitimate cross-language test pattern; valid TDD exemptions exist (no conditional logic, pure scaffolding, cited existing test); proxy-validation does NOT fire when the new path has no live/external boundary (pure in-process logic the offline test fully exercises), when the live exercise is explicitly deferred to a NAMED child ticket, or when a documented rationale explains why the substitute is faithful to production. SEVERITY: missing failure-path on a new user-facing flow = MAJOR; change-detector/tautology = MAJOR; proxy-validation of a defaulted/cutover path = MAJOR; everything else PASS. This criterion runs on LEAF tickets only (no children): a container defers tests to its children, and a test-authoring leaf IS the test.

Checklist:
- GATE: the plan introduces new logic/behavior that is testable (else PASS not-applicable — refactor/config/doc/mechanical).
- For new user-facing behavior, failure/timeout/invalid/empty paths and a caller-facing contract are addressed.
- Boundary scenarios considered (oversized input, malformed payload, non-Latin, back-button).
- No self-authored-oracle / change-detector anti-pattern: snapshot-of-current-output, tautology, or source-grep masquerading as a behavioral test (MAJOR — locks in the bug).
- Changed/deleted behaviors get corresponding modify/remove-test work.
- No proxy-validation: a changed or newly-DEFAULTED risky path is not validated SOLELY through a mock/offline substitute that bypasses the new behavior (canned/fake agents that never hit the real boundary). For a cutover that defaults to a new path, offline-green coverage that never exercises that path end-to-end (live) = MAJOR; add a live/end-to-end acceptance criterion for the defaulted path.

## E6
**Verification/termination + end-state reachability** — exec:2-STEP, advisory, facet:ac-text-quality

Check that the work has a clear way to be verified done and that the steps actually reach the stated end-state (a two-step check: identify the proving command for each claim, then confirm the union of steps proves every criterion). Binary checks: (a) every completion-relevant claim has a concrete proving command/check that would produce evidence on success (not 'should work'); (b) the claim is free of red-flag hedges ('should', 'probably', 'seems'); (c) every acceptance criterion maps to at least one described step; (d) the UNION of the steps actually reaches the stated end-state — no gap between 'what we'll do' and 'what done looks like'; (e) user-facing flows have an end-to-end check or a documented rationale for its absence; (f) PROVING-COMMAND-EXERCISES-THE-CHANGE: when the plan CUTS OVER or DEFAULTS to a new code path, at least one acceptance criterion EXERCISES that path as it will run in production (e.g. end-to-end against the live model/dependency) — a proving command that passes on a mock/offline substitute which BYPASSES the new behavior does NOT prove the criterion, because the AC can be satisfied without the changed risky path ever executing (green offline tests plus a signed completion verdict are necessary but NOT sufficient). SEVERITY: a criterion no step reaches, or a claimed-but-unmeasured outcome, is MAJOR; a cutover/defaulted path with no criterion that exercises it end-to-end (live) is MAJOR; a hedge without a proving command is MINOR. ANTI-FP: universal lint/format/test commands do not by themselves prove a specific criterion; (f) is not-applicable when the new path has no live/external boundary, when the cutover stays behind a non-default opt-in flag, or when the end-to-end exercise is explicitly deferred to a named child with rationale. PASS if done-ness is verifiable and the end-state is reachable.

VERIFY-COMMAND DEFECT TAXONOMY — nine ways a PRESENT, plausible-looking proving command silently lies (flag any): (1) substring false-positive — anchor the pattern with a word-boundary / quoted key (grep -E '"cycle_count"[[:space:]]*:' not grep cycle); (2) shell-expansion leakage — a $VAR / $? inside a grep pattern expands before grep runs; (3) wrong helper-script argv — cross-check flag names against --help (a --label vs --labels mismatch ships a silent runtime failure); (4) fixture invalidity — the fixture does not actually exercise the claim; (5) missing cardinality assertion when the criterion names a count (assert `jq '.events | length'` -eq N); (6) prose-vs-structured assertion target — assert on the structured artifact with `jq -e`, not on prose output; (7) missing prerequisite-state ordering — a command assumes state a prior step must establish; (8) conflicting ACs (tests-pass vs test-fails-RED) needing a sequencing AC; (9) references a sibling ticket's files with no dependency edge. Coach the corrected command with a concrete anchor. OPERATOR-ATTESTED EXCEPTION (ADR 0043): when an acceptance criterion's checkbox text begins with the exact case-insensitive tag `[operator-attested]`, its proving 'command' is a concrete attestation RECORDED ON THE TICKET (a change id / vote outcome / timestamp) rather than an in-session command — do not flag the absence of an in-session proving command for such a criterion.

Checklist:
- Every completion-relevant claim has a concrete proving command/check that produces evidence on success.
- No red-flag hedges ('should','probably','seems') standing in for a proving command.
- Every acceptance criterion maps to at least one described step.
- The union of steps actually reaches the stated end-state (no gap between 'what we'll do' and 'what done looks like').
- User-facing flows have an end-to-end check or a documented rationale for its absence.
- A cutover/default-flip to a new code path has an acceptance criterion that EXERCISES that path end-to-end as in production (e.g. against the live model/dependency), not only offline/mocked proxies that bypass the new behavior; green offline tests + a signed completion verdict are necessary but not sufficient.

## F1
**Measurability & in-session completability** — exec:1-TURN, blocking, facet:ac-text-quality

Examine each acceptance/success criterion for measurability and whether an agent can complete it within ONE working session. Apply these binary checks: (a) the criterion states a specific OBSERVABLE outcome (what changes for the user/system), not effort ('implement the service') or a subjective term ('improved/better/sufficient'); (b) it is evaluable IN-SESSION via repo artifacts, the closing PR's CI, or a deterministic command against a reachable target — NOT post-sprint-only (multi-day telemetry, adoption %, survey feedback score ≤2); (c) it is a durable end-state, not a one-time transition (litmus: could it be false before this work and true only because of it?); (d) the unit is right-sized (a coherent single-outcome deliverable, not an epic-of-epics, not a one-line triviality). SEVERITY: outcome-vague or effort-framed criteria are MAJOR; post-sprint-only validation is MAJOR; thin-but-present is MINOR. ANTI-FP: evaluate the spec AS WRITTEN, not the current codebase; observability tooling itself is valid in-session work; 'post-deployment' is fine if the check is deterministic. PASS if all criteria are measurable and in-session completable. OPERATOR-ATTESTED EXCEPTION (ADR 0043): a criterion whose checkbox text begins with the exact case-insensitive tag `[operator-attested]` has "done" evidence that inherently lives OUTSIDE the codebase (a deploy, a live drill, a console setting) — it is MET by a concrete attestation recorded on the ticket (a change id / vote outcome / timestamp), NOT by an in-session repo/CI check. Do NOT flag a tagged criterion as post-sprint-only or in-session-uncompletable; that is by design.

Checklist:
- Each criterion states a specific observable outcome (what changes for user/system), not effort or a subjective term.
- Evaluable in-session via repo artifacts / closing PR CI / a deterministic command — not post-sprint-only (multi-day telemetry, adoption %, survey).
- A durable end-state, not a one-time transition (could be false before this work and true only because of it).
- A coherent single-outcome deliverable — not an epic-of-epics, not a one-line triviality.

## F4
**User/problem present (value)** — exec:1-TURN, advisory, facet:scope-intent

Check that the plan names WHO the work is for and WHAT problem they face, and that value is validatable. Binary checks: (a) the context names a specific user/stakeholder and the problem they have today; (b) the criteria collectively represent an observable improvement to that user or a measurable business outcome, not pure system internals; (c) it is NOT a bare technical task with no named beneficiary ('Refactor the service layer'); (d) at least one criterion carries a concrete validation mechanism (before/after workflow comparison, an operational metric target, dogfooding). SEVERITY: no named beneficiary AND no value-validation is MAJOR; missing only the validation mechanism is MINOR. ANTI-FP: an IMPLIED technical consumer counts for low-level/internal tasks (cleanup, dep upgrades, library internals) — do not flag those; backend work affecting latency/reliability scores normally via an operational signal. PASS if a beneficiary and the value are clear.

Checklist:
- Names a specific user/stakeholder and the problem they have today.
- Criteria represent an observable improvement to that user / a measurable business outcome, not pure system internals.
- Not a bare technical task with no named beneficiary.
- At least one criterion carries a concrete validation mechanism (before/after, operational metric, dogfooding).

## G1G2
**Edit-set / scope accuracy [agent]** — exec:AGENT, blocking, facet:codebase-grounding

Verify (via Glob/Grep) that every file/symbol the plan names actually exists; enumerate consumers/callers OUTSIDE the artifact's dir that a change would require updating; flag hallucinated/missing edit targets and unenumerated consumers; classify behavioral hunks in/ambiguous/out-of-scope (CREATION=new behavior->out-of-scope). High blast-radius alone is not a fail if acknowledged. ANTI-FP: report only high-confidence; STOP if scope too vague. Any symbol created by a ticket this ticket depends_on (evaluated recursively) is treated as if it EXISTS and is NOT MISSING. A symbol/import you cannot find via Glob/Grep may be a THIRD-PARTY/library symbol living in an installed dependency (site-packages) your repo-scoped tools cannot see — call `resolve_symbol` to check the installed environment and treat an environment-resolved symbol as EXISTING; when it is plausibly a library symbol you cannot ground, abstain rather than flag it hallucinated.

Checklist:
- Every file/symbol the plan names exists (Glob/Grep) — flag hallucinated/missing edit targets.
- Consumers/callers OUTSIDE the artifact's dir that a change requires updating are enumerated — flag unenumerated consumers.
- Behavioral hunks classified in/ambiguous/out-of-scope (CREATION=new behavior=out-of-scope); high blast-radius alone isn't a fail if acknowledged.

## G3
**Child coverage [agent, container]** — exec:AGENT, advisory, facet:container

CONTAINER-only (has_children): does the union of children cover the parent's acceptance/success criteria? 4-bucket audit per criterion (fully / partially / uncovered / structural) + a coverage map; an uncovered parent criterion is a finding. ANTI-FP: a criterion covered-by-definition by a named consumer counts.

THREE-PART COVERAGE STANDARD — a child covers a parent criterion only when ALL hold: (1) SAME OBSERVABLE OUTCOME (not a related one, not a precursor); (2) scope MATCHING-OR-EXCEEDING (no narrowing of conditions, users, data shapes, or environments); (3) measurable IN THE SAME TERMS. When in doubt, classify partial. THREE SC-CONTRADICTION PATTERNS a coverage map alone cannot see (each is a finding — the plan is structurally guaranteed to fail the completion verifier): bypass-annotation (a child plans to annotate/exclude items from the parent's metric instead of resolving them — 'SC says zero matches, the DD annotates exceptions'); scope-narrowing (a child covers a narrower condition set than the parent criterion); partial-without-remainder (a child covers part and does not name the uncovered remainder).

Checklist:
- The union of children covers each parent acceptance/success criterion — 4-bucket audit (fully/partially/uncovered/structural); an uncovered criterion is a finding.

## G4
**Child consistency [agent, container]** — exec:AGENT, advisory, facet:container

CONTAINER-only (has_children): check the 7 cross-child interaction modes — implicit shared state, conflicting assumptions, dependency gap, scope overlap, ordering violation, consumer impact, residual references. Each detected mode is a finding. ANTI-FP: high-confidence only; benign-reading filter.

CONSUMER-ENUMERATION PRECURSOR: before analyzing the consumer-impact mode, first ENUMERATE all consumers of the modified system (a worklist), then analyze each — a recall→worklist pass over the existing consumer-impact mode so no consumer is silently missed. SCOPE-GAP corollary: an item 'out of scope' for one child may be 'in scope' for NONE — flag an owned-by-none gap.

Checklist:
- Check the 7 cross-child modes: implicit shared state, conflicting assumptions, dependency gap, scope overlap, ordering violation, consumer impact, residual references — each detected mode is a finding.

## G5
**Decomposition judgment** — exec:1-TURN, blocking, facet:scope-intent

Judge whether the ticket is a single COHERENT unit of work or bundles work that belongs in
separate children. COHERENCE — not raw size — is the primary axis: a large, single-concern
vertical slice is correctly ONE unit.

PRIMARY — single-concern. A unit warrants decomposition when it bundles MORE THAN ONE
independently-valuable / independently-releasable OUTCOME, carries more than one "reason to
change" (a distinct actor/persona/concern), or MIXES heterogeneous change kinds (e.g. a
bug-fix AND a new feature AND an unrelated refactor). For an epic/parent that means it should
have children; the tell is a structural 'and' joining genuinely independent goals, spanning
independent personas, or a set of unrelated success criteria.

VALUE-PRESERVATION (a decomposition finding must satisfy this to stand). A unit is right-sized
as one piece when it delivers a SINGLE increment of value whose parts would be tightly coupled,
order-dependent, or individually worthless split apart — such a unit PASSES, and keeping it
whole is correct. A finding is warranted only when the unit would divide into pieces that are
EACH independently valuable, testable, and releasable on their own.

VERTICAL SLICE, not layers. A coherent slice deliberately touches multiple architectural layers
(UI + logic + storage) and often several files — that is the SHAPE OF A GOOD UNIT, not a
decomposition trigger. Do NOT flag a unit merely for spanning layers, for touching several
files, or for introducing an interface whose consumer ships in the same unit (keep a new API
and its first caller together). Splitting one feature horizontally by layer is an anti-pattern —
each layer alone delivers no value.

WEAKER PRIORS (advisory only — never the sole basis for a finding). Genuine DIFFUSION — the work
is scattered across many UNRELATED subsystems (not merely many files within one coherent area) —
and LOW SCOPE-CERTAINTY (the unit is exploratory / its final shape is not yet known) are soft
signals worth SURFACING, but neither by itself establishes a decomposition finding; weigh them
only alongside a real single-concern violation. Surface the concern as an observation; do not
prescribe a remedy.

LEAF: a leaf is right-sized when it executes coherently in one session (and is not a
one-criterion triviality). YAGNI / Rule-of-Three: proposed structure/abstraction is justified by
the CURRENT criteria (≥3 real call-sites for any new abstraction), not a hypothetical.

SEQUENCING: judge whether a thin vertical-slice / evidence-gated MVP de-risks the riskiest piece
first, versus a horizontal big-bang (decomposing into many parallel parts does not by itself
reduce big-bang risk).

ANTI-FP: an incidental 'and' does not fail single-concern; a file/layer count is a WEAK PRIOR,
not authoritative impact; a coherent multi-layer slice PASSES. Treat any deterministic size
signal (e.g. DET P4 oversize) as a coarse prior only — the test is coherence, not counts. PASS
when the unit is a single coherent concern whose parts could not each stand alone.

Checklist:
- COHERENCE is primary: flag decomposition only when the unit bundles >1 independently-releasable outcome, >1 reason-to-change (distinct actor/persona/concern), or mixes heterogeneous change kinds (fix + feature + unrelated refactor). An epic/parent that fails single-concern should have children.
- A decomposition finding stands ONLY if the unit would divide into pieces that are each independently valuable, testable, and releasable. A single increment of value whose parts would be coupled/order-dependent/worthless-apart is right-sized whole — PASS it.
- Do NOT flag a unit for spanning multiple architectural layers, for touching several files, or for introducing an interface whose consumer ships in the same unit — that is a coherent vertical slice, the shape of a good unit. Splitting one feature horizontally by layer is an anti-pattern.
- Genuine diffusion (scatter across UNRELATED subsystems, not many files in one coherent area) and low scope-certainty are advisory-only signals to SURFACE, never the sole basis for a finding; weigh them only alongside a real single-concern violation. Observe the concern; do not prescribe a remedy.
- A leaf is small enough to execute in one session (and not a one-criterion triviality).
- Proposed structure is justified by current criteria (>=3 real call-sites for any new abstraction).
- Sequencing has a thin vertical-slice / evidence-gated MVP de-risking the riskiest piece first, not a horizontal big-bang.

## G6
**Approach soundness, anti-patterns & alternative-selection [overlay]** — exec:AGENT, blocking, facet:approach-soundness

Judge whether the plan's chosen APPROACH is sound and the best available — the defect a well-formed plan can still have. (1) MECHANISM CORRECTNESS: reason through whether the proposed mechanism actually achieves the goal — logic/data-flow complete, edge/empty/concurrent/failure cases handled, no hidden ordering/atomicity assumption that breaks (e.g. a check-then-act idempotency that is really a TOCTOU race). (2) FITNESS-FOR-PURPOSE: does this solution actually solve the named problem (not a proxy)? (3) APPROACH SELECTION (alternatives WITHOUT negative priming): YOU (the reviewer) generate 1-2 plausible alternative approaches that differ structurally (data-layer / control-flow / dependency-graph / interface-boundary) and judge whether the plan's chosen approach is defensibly at-least-as-good on codebase-alignment, blast-radius, testability, simplicity, robustness. If defensible -> PASS and DISCARD your generated alternatives (never write them into the plan). If a clearly-superior alternative was missed -> a FINDING coaching the PLANNER to adopt the better approach ('consider X because Y') — the implementer's plan still contains only ONE approach. (4) Confirm the plan states a POSITIVE rationale for the chosen approach (why it fits) — its ABSENCE is a finding; do NOT require a rejected-alternatives section (that primes implementers with rejected behavior). SEVERITY: a mechanism that won't work or a clearly-wrong approach = CRITICAL (agent builds the wrong thing); a missed clearly-better alternative = MAJOR; missing positive rationale = MINOR. ANTI-FP: mechanical/well-understood changes have no real design choice -> PASS not-applicable; do not manufacture alternatives for a forced solution; ground correctness reasoning in the actual code via the tools.

Checklist:
- The proposed mechanism actually achieves the goal — logic/data-flow complete; edge/empty/concurrent/failure handled; no hidden ordering/atomicity assumption (e.g. check-then-act idempotency that is a TOCTOU race).
- The solution solves the named problem, not a proxy.
- Reviewer generates 1-2 structurally-different alternatives and judges the chosen approach defensible on codebase-alignment/blast-radius/testability/simplicity/robustness; a clearly-superior missed alternative is a coaching finding.
- The plan states a POSITIVE rationale for the chosen approach (its absence is a finding); do NOT require a rejected-alternatives section (anti-priming).

## G7
**Leaf-parent containment [agent, leaf]** — exec:AGENT, advisory, facet:leaf

LEAF-with-parent only: is the leaf's declared scope a SUBSET of its parent's plan? The parent's plan is the containing contract; the leaf may deliver PART of it (consistent narrowing), but it may NOT step outside it. This criterion maps its severity onto the existing `divergent_implementation` plan axis — a leaf diverging from its parent IS exactly that signal.

FETCH THE PARENT. The parent's id (`parent_id`) is provided in the ticket-graph context. Call `show_ticket(<parent_id>)` to read the parent's plan (its What/Scope/Success Criteria/Acceptance Criteria). Optionally also read the grandparent (`show_ticket(<grandparent_id>)`) when the parent is thin and the real contract lives one level up.

FIRE A FINDING when the leaf is NOT a subset of the parent — specifically when the leaf:
- (a) delivers something the parent's plan does not contain, or that the parent implies is out of scope;
- (b) contradicts a parent acceptance/success criterion; or
- (c) redefines a deliverable the parent specifies differently.
Consistent NARROWING — a leaf that does PART of what the parent describes, faithfully and without contradiction — is NOT a finding.

CONFLICT RULE — the PARENT WINS. On any conflict between the leaf and the parent, the parent's plan is authoritative. The productive move is to realign the leaf to the parent. If you believe the parent is genuinely wrong, do NOT silently diverge the leaf — instead update the parent first (which stales the parent's own plan-review attestation and forces its re-review), and only then re-review the leaf against the corrected parent. Realigning the leaf to a subset of the parent, or updating the parent, are the only acceptable resolutions.

Checklist:
- The leaf's What/Scope/ACs are a SUBSET of the parent's declared scope — a leaf that delivers something the parent's plan does not contain, contradicts a parent AC/success criterion, or redefines a parent deliverable is a finding; consistent narrowing (a leaf doing PART of the parent) is NOT a finding.

## ISF
**Intent-source fidelity (plan vs linked design intent)** — exec:2-STEP, advisory, facet:intent-provenance

Compare the plan against the EXTERNAL intent expressed in the ticket's LINKED SESSION LOG (the design/brainstorm of record), to catch requirements the plan SILENTLY DROPPED, descoped, or contradicted relative to what the user expressed — a defect no plan-internal check can catch (E3 compares plan-vs-its-own-title; this compares plan-vs-the-original-intent). 2-STEP: (1) extract the discrete expressed requirements/decisions/constraints from the linked session log; (2) check the plan + its ticket graph against each, flagging any dropped, narrowed/out-scoped-without-rationale, or contradicted. Runs on a FRONTIER model (large session-log context) and is FED the session log + the pre-resolved ticket graph as context — NOT agent/tool-using (deterministic if the linked log exceeds the escalated context window, evaluate against a SUMMARY of the log and RECORD that a summary was used — the finding then carries REDUCED CONFIDENCE). ANTI-FP: a requirement DELIBERATELY descoped WITH a stated rationale is not a finding; fire only on SILENT or unjustified divergence.

Checklist:
- Discrete expressed requirements/decisions/constraints are extracted from the linked session log.
- Each expressed requirement is honored by the plan + ticket graph, or descoped WITH a stated rationale.
- No expressed requirement is silently dropped, narrowed, or contradicted (the visual-editing-deferred failure mode).

## T1
**Prior-art / novel-architecture justification [overlay]** — exec:AGENT, blocking, facet:overlay-priorart

OVERLAY — apply when the plan crosses a bright-line (external integration, unfamiliar dependency, security/auth, a novel architectural pattern, a performance/scalability target, or a migration). Tool-grounded where possible (web/codebase). Checks: (a) is there relevant PRIOR ART the plan should consider before committing, or is it reinventing/repackaging something that exists? (b) for a novel pattern: is the novelty justified vs an established approach (anti-repackaging, Rule-of-Three)? (c) are unverified capability assertions ('library supports X') resolved? SEVERITY: a novel architecture chosen with no consideration of prior art = MAJOR. ANTI-FP: a well-trodden pattern needs no prior-art search; not-applicable when no bright-line fires.

Checklist:
- Relevant prior art is considered before committing — not reinventing/repackaging something that exists.
- A novel pattern's novelty is justified vs an established approach (anti-repackaging, Rule-of-Three).
- Unverified capability assertions ('library supports X') are resolved.

## T10
**Infrastructure / IaC [overlay]** — exec:AGENT, advisory, facet:overlay-infra

OVERLAY — apply only when the plan provisions or configures infrastructure (cloud resources, IaC: Terraform/CloudFormation/CDK/Pulumi/Ansible, Kubernetes/Helm); else PASS not-applicable. Binary checks: (a) STATE: remote state + locking (no local state); plan-before-apply discipline. (b) LEAST-PRIVILEGE IAM: roles/policies scoped to the minimum, no wildcard `*:*`/admin-for-convenience, no long-lived credentials committed. (b2) ENDPOINT ACCESS CONTRACT: for every network-reachable service the plan STANDS UP (a daemon / web UI / API / SSH or admin console), the plan must state how HUMAN principals (users AND admins) authenticate to THAT service — a named identity mechanism OR an explicit, justified no-auth rationale (loopback-only / behind an authenticating gateway / single-tenant private network). Service-to-service credentials (deploy keys, webhook/API tokens, SSM secrets) authenticate machines, NOT human/admin access, so they do not satisfy this. A stood-up internet-/untrusted-network-reachable service whose human/admin auth is left unspecified is an under-specified contract (deny-by-default) — flag it. (c) IDEMPOTENCY & DRIFT: changes are idempotent; drift / out-of-band manual changes considered. (d) BLAST RADIUS & ENV ISOLATION: dev/stage/prod separation; destroy/replace safety — does an apply risk data loss (RDS deletion, S3 force-destroy, instance/volume replacement)? `prevent_destroy` on stateful resources? (e) SECRETS: no plaintext secrets in IaC/vars; use a secrets manager / SSM / vault. (f) COST & SIZING: obviously-expensive or unbounded resources flagged; limits/autoscaling/quotas considered. (g) OBSERVABILITY & OWNERSHIP: logging/metrics/alarms for new infra; the resource is reproducible (as-code) with a clear teardown. SEVERITY: a destructive apply with no safeguard, a wildcard-admin grant, a plaintext secret, or an internet-reachable service with no specified human/admin authentication = MAJOR. ANTI-FP: not-applicable for non-infra tickets; managed defaults that are documented are fine.

Checklist:
- Remote state + locking (no local state); plan-before-apply discipline.
- Roles/policies scoped to minimum; no wildcard *:* / admin-for-convenience; no long-lived creds committed.
- For EVERY network-reachable service the plan STANDS UP (each daemon / web UI / API / SSH or admin console — e.g. a code-review server, dashboard, or broker), the plan states how HUMAN principals (end users AND administrators) authenticate to THAT service: a named identity mechanism (OIDC/OAuth/SSO/LDAP/HTTP-auth) OR an explicit, justified no-auth rationale (loopback-only, behind an authenticating gateway, single-tenant private network). Service-to-service credentials (deploy keys, webhook/API tokens, SSM secrets) do NOT satisfy this — they authenticate machines, not human/admin access to the service. A stood-up service reachable from the internet or an untrusted network whose human/admin authentication is left unspecified is an under-specified contract (deny-by-default) — flag it.
- Changes idempotent; drift / out-of-band manual changes considered.
- Dev/stage/prod separation; destroy/replace safety; prevent_destroy on stateful resources (RDS/S3/volumes).
- No plaintext secrets in IaC/vars; secrets manager / SSM / vault.
- Obviously-expensive or unbounded resources flagged; limits/autoscaling/quotas considered.
- Logging/metrics/alarms for new infra; reproducible as-code with a clear teardown.

## T11
**Data-migration / backfill safety [overlay]** — exec:AGENT, advisory, facet:overlay-migration

OVERLAY — apply only when the plan changes a schema / persisted format or backfills data; else PASS not-applicable. This is migration-EXECUTION safety (distinct from T4 which is breakage-acknowledgement). Binary checks: (a) ONLINE / EXPAND-CONTRACT: the migration runs without downtime and via expand-contract (add nullable -> backfill -> enforce), not a single blocking DDL that locks a large table. (b) BATCHING & SCALE: large backfills are batched/throttled, not one giant transaction. (c) RESUMABILITY: a partially-completed migration is resumable/idempotent (re-runnable without double-applying). (d) DUAL-WRITE WINDOW: rows written DURING the migration are handled (no lost writes between backfill and cutover). (e) ROLLBACK / DATA-LOSS: there is a back-out path and data loss is impossible on partial failure. SEVERITY: an irreversible single-shot migration with no rollback, or a long blocking lock on a large table = MAJOR. ANTI-FP: not-applicable for non-persisted/in-memory changes.

POST-ROLLBACK SCHEMA STATE: the plan must assert on the schema STATE AFTER rollback, not merely that 'rollback exits 0' — a rollback can exit 0 while leaving the schema in an incorrect intermediate state. Where destructiveness is ambiguous, default to compensating-forward and attach a literal `DATA LOSS RISK` note rather than assuming reversibility.

Checklist:
- Migration runs without downtime via expand-contract (add nullable -> backfill -> enforce), not a single blocking DDL on a large table.
- Large backfills are batched/throttled, not one giant transaction.
- A partially-completed migration is resumable/idempotent.
- Rows written DURING the migration are handled (no lost writes between backfill and cutover).
- There is a back-out path and no data loss on partial failure.

## T12
**Rollout / rollback / reversibility [overlay]** — exec:1-TURN, advisory, facet:overlay-rollout

OVERLAY — apply only when the plan changes the runtime behavior of a deployed or long-running system; else PASS not-applicable (e.g. a library/CLI with no deploy surface). Binary checks: (a) STAGED ROLLOUT: a behavior change reaches production via a flag / canary / staged rollout, not a single 100%-traffic flip. (b) ROLLBACK: there is an explicit, cheap, tested way to undo the change quickly without data cleanup. (c) DEPLOY ORDERING: if producers/consumers or coordinated services change, the deploy order (and coexistence of old+new during rollout) is specified. SEVERITY: a one-shot behavior change to all traffic with no flag and no rollback path = MAJOR. ANTI-FP: not-applicable for non-deployed code; an internal-only change with trivial revert is fine.

Checklist:
- A behavior change reaches prod via flag/canary/staged rollout, not a single 100%-traffic flip.
- An explicit, cheap, tested way to undo quickly without data cleanup.
- If producers/consumers/coordinated services change, deploy order and old+new coexistence is specified.

## T13
**Behavioral-prohibition consumer scan** — exec:AGENT, advisory, facet:overlay-prohibition

OVERLAY — apply only when the plan NEWLY FORBIDS a previously-permitted action: it introduces an
enforcement/gate that will start rejecting something that used to be allowed. Trigger lexicon:
"block", "reject", "require … before", "enforce", "must pass", "cannot merge until", "deny",
"fail the build if". If the plan introduces no new prohibition, PASS as not-applicable.

ENUMERATE THE INVISIBLE AFFECTED SET. A new prohibition silently breaks existing call sites that
perform the now-outlawed behavior — nothing in the remaining plan references them, so they are
invisible unless enumerated. Translate the prohibition into concrete grep patterns over EXISTING
call sites of the behavior being outlawed, then Grep/Read to find them. Worked example:
"require tests to pass before merge" → grep for `gh pr merge`, direct merge steps, and CI jobs
that merge without the new gate.

CLASSIFY each existing call site into exactly one bucket:
- MIGRATED — the plan already updates this site to satisfy the new prohibition.
- EXEMPTED — the plan (or an explicit rationale) carves this site out of the prohibition.
- UNCOVERED — the site performs the outlawed behavior and the plan neither migrates nor exempts
  it. Each UNCOVERED site is the finding: the plan will start rejecting it with no migration path.

PASS when every existing call site is MIGRATED or EXEMPTED (or there are none). Report each
UNCOVERED site with its location as the grounded evidence.

FAIL-OPEN (abstain-with-coverage): if the outlawed behavior cannot be reduced to a checkable grep
pattern, or the repository tools cannot enumerate its call sites, ABSTAIN — record the prohibition
as covered-but-unenumerable rather than asserting an ungroundable gap. Do not fabricate call sites.

Checklist:
- A NEW prohibition (block/reject/require-before/enforce/must-pass/cannot-merge-until) is translated into grep patterns over EXISTING call sites of the outlawed behavior, and each site is classified MIGRATED / EXEMPTED / UNCOVERED.
- Each UNCOVERED call site (performs the outlawed behavior; plan neither migrates nor exempts it) is the finding, cited by location.
- Fail-open: if the outlawed behavior cannot be reduced to a checkable grep pattern, ABSTAIN with coverage rather than assert an ungroundable gap; never fabricate call sites.

## T14
**CI-trigger / release-infrastructure coverage audit** — exec:AGENT, advisory, facet:overlay-citrigger

OVERLAY — apply only when the plan introduces a NEW git ref pattern (branch namespace, tag/merge
ref), a new event source or schedule, or otherwise changes what CI fires on; OR adds new
release-time-exercised infrastructure (a new package, a new CI job, a plugin entry point). If the
plan does none of these, PASS as not-applicable.

WORKFLOW-TRIGGER FILTER AUDIT. A new ref/event pattern silently fails to fire when existing
workflow trigger filters do not include it (the real defect: `branches: [main]` silently skipping
per-story PRs). Enumerate the repository's workflow files (Grep/Read `.github/workflows/*.yml`)
and, for the new pattern, classify EACH workflow's trigger filter into exactly one bucket:
- INCLUDED — the workflow's trigger filter matches the new pattern, so it will fire.
- EXCLUDED — the workflow's trigger filter explicitly does NOT match the new pattern, so it will
  silently skip. Each EXCLUDED workflow that SHOULD fire is the finding.
- NO_FILTER — the workflow has no ref/event filter, so the new pattern is trivially covered.
Require an affirmative per-workflow classification — do not assume coverage.

RELEASE-INFRASTRUCTURE SIBLING. A release-time-exercised change (new package, new CI job, plugin
entry point) must be reflected in the release script's dependency graph — this is an INTERNAL
script dependency, not an external-service shape, so the generic external-outcome classifier misses
it. Flag a release-infra change the release process does not account for.

PASS when every workflow is INCLUDED or NO_FILTER (or affirmatively EXEMPTED with rationale) and
release infra is accounted for.

FAIL-OPEN (abstain-with-coverage): if a workflow's trigger syntax is unknown/unparseable, or the CI
system is one the tools cannot read, ABSTAIN for that workflow — record it as covered-but-unverified
rather than asserting an EXCLUDED gap you cannot ground. Fail open, never fail closed on unknown CI.

Checklist:
- For a NEW git ref pattern / event source / schedule, each workflow's trigger filter is classified INCLUDED / EXCLUDED / NO_FILTER; an EXCLUDED workflow that should fire is the finding (the branches:[main] silent-skip defect).
- A release-time-exercised change (new package / CI job / plugin entry point) is reflected in the release script's dependency graph — an internal-script dependency the external-outcome classifier misses.
- Fail-open: an unknown/unparseable workflow trigger or CI system ABSTAINS with coverage — never fail-closed on unknown CI, never fabricate an EXCLUDED gap.

## T2
**Empirical probe (red->green / spike) [overlay]** — exec:1-TURN, advisory, facet:overlay-empirical

Decide whether this plan's RISK warrants empirical validation, and whether such validation is planned. STEP 1 — is the plan complex/novel/uncertain? Signals: a novel or unproven architecture/pattern; an unverified assumption about an external system, dependency, or performance/scale behavior; a default/threshold/heuristic that is ASSERTED rather than derived from data; a design choice the author could not settle by reasoning alone; behavior whose correctness cannot be established without running it. If NONE apply (a well-understood, mechanical, or low-risk change), PASS — not applicable. STEP 2 — if complex/novel, does the plan ALREADY include an empirical-validation step: a spike, probe, prototype, benchmark, measurement, experiment, A/B, a fixture/RED test that exercises the risky behavior, or a pilot with success metrics? If YES, PASS (suppressed — experimentation already present). Only FAIL/advise if the plan is complex/novel AND asserts unvalidated choices with NO plan to test them — then recommend the specific spike/probe/measurement/experiment that would de-risk it before full build-out. SEVERITY: a core design resting on an unvalidated assumption = MAJOR; a tunable default/threshold with no measurement plan = MINOR. ANTI-FP (critical): do NOT fire when the ticket already contains experimentation — a spike, a fixture/RED test, a pilot with metrics, a measurement step, or stated success criteria for a trial. A mechanical or fully-specified change is NOT complex. Treat an explicit 'TBD: measure X' / 'derive from fixture' as validation-present. PASS unless there is a real, unvalidated, high-uncertainty choice left untested. ANTI-FP (adopted-library contract, FP6): a capability that is an adopted, maintained library's ADVERTISED CONTRACT is PROVEN by adoption — do NOT demand the plan empirically re-prove it (that is testing code that isn't ours). Flag only the project's OWN code paths, or a SPECIFIC, newer integration point of that library whose maturity is genuinely in question (library-contract → PASS; library-feature-maturity → may warrant a probe).

Checklist:
- STEP 1: the plan is complex/novel/uncertain (novel architecture, unverified external/scale assumption, asserted-not-derived default, correctness un-establishable without running).
- STEP 2: if risky, the plan already includes a spike/probe/prototype/benchmark/RED-fixture/pilot-with-metrics — else recommend the specific one.
- ANTI-FP: do NOT fire when experimentation is already present; a mechanical/fully-specified change is not complex.

## T3
**Integration feasibility [overlay]** — exec:AGENT, advisory, facet:overlay-feasibility

OVERLAY — apply only when the plan integrates an external API/CLI/service/library or asserts a capability it has not used before; else PASS not-applicable. Binary checks (tool-grounded where possible): (a) technical_feasibility — is the integration achievable as described, or is there a capability gap? (b) for a CLI/API: do the named subcommands/endpoints actually exist (verify against --help / docs) — MATCH / MISMATCH / UNVERIFIED; (c) auth/HTTPS preconditions stated; (d) a critical capability gap should route to a SPIKE before committing the full plan. SEVERITY: an asserted-but-unverified external capability the plan depends on = MAJOR. ANTI-FP: verify before asserting a mismatch; an internal, already-used integration is not-applicable.

EMPIRICISM DEPTH — three axes: (a) per-command --help / flag-level empiricism — OBSERVE flag names from --help, do not infer them from memory or a prior version (flag-level mismatches like --label vs --labels cause silent runtime failures after ship); (b) endpoint granularity — the unit is a specific endpoint SURFACE, not the vendor: a new endpoint on an already-used vendor is a NEW integration (different path, possibly different OAuth scopes / rate limits); (c) environment preconditions — a platform-capability probe (an HTTP-only environment is a CONTRADICTED signal for any OAuth-callback flow regardless of API-capability verification). Do NOT mark a signal verified on general knowledge alone; if you recall a URL from training, treat it as unverified until confirmed. CLASSIFY each signal into one of FOUR EVIDENCE CLASSES, recorded in the Pass-1 evidence[] for Pass-2 to read: Verified (observed), Partially-verified (the integration exists but the specific surface is unconfirmed), Unverified (could not probe), Contradicted (the environment falsifies it).

Checklist:
- The integration is achievable as described, or a capability gap is named.
- Named subcommands/endpoints actually exist (verify against --help/docs) — MATCH/MISMATCH/UNVERIFIED.
- Auth/HTTPS preconditions stated.
- A critical capability gap routes to a SPIKE before committing the full plan.

## T4
**Compat / destructiveness as an explicit justified choice [overlay]** — exec:1-TURN, blocking, facet:overlay-compat

OVERLAY — apply when the plan changes existing behavior, an interface/schema/data shape, or performs a destructive/irreversible operation; else PASS not-applicable. BIDIRECTIONAL check: (a) UNACKNOWLEDGED breakage — does the plan change/remove something consumers rely on without acknowledging the break, an expand-contract sequence, or a rollback path? (b) GRATUITOUS compat — does it add backward-compat shims, feature flags, or version branches that aren't warranted? (c) is a destructive/irreversible step an EXPLICIT, justified choice (not incidental)? SEVERITY: unacknowledged breaking change with no migration/rollback = MAJOR. ANTI-FP: a purely additive change is not-applicable; an explicitly justified breaking change with a migration is fine. The REMEDY for a destructive/breaking change is an explicit ROLLBACK / back-out plan or expand-contract sequencing — checking only that breakage is *acknowledged* is insufficient; require the reversibility mechanism.

Checklist:
- A change/removal consumers rely on is acknowledged with expand-contract or a rollback path.
- No unwarranted backward-compat shims / feature flags / version branches.
- Any destructive/irreversible step is an explicit, justified choice.
- The remedy is an explicit rollback/back-out plan or expand-contract — acknowledgement alone is insufficient.

## T5a
**Performance (overlay)** — exec:1-TURN, advisory, facet:overlay-perf

OVERLAY — apply only if the plan introduces new I/O, data access, LLM/compute calls, batch ops, or shared resources; otherwise PASS as not-applicable. Binary checks: (a) latency — hot-path operations have time-bounded done-definitions and no synchronous blocking on a hot path; (b) resource_efficiency — no N+1 / redundant API or LLM calls / unbounded memory growth; (c) scalability — input-size limits, concurrency/rate-limit/pool handling, and load expectations are stated. SEVERITY: a user-facing operation with no latency target is MAJOR; an O(n) LLM-calls-per-item pattern is MAJOR — state the impact in Big-O terms. ANTI-FP: score normally only where the plan actually adds a performance-relevant path. PASS if performance characteristics are sound for the scope. ALSO assess COST/economics (not just latency): per-call $ (e.g. an LLM/embedding call per item), egress, always-on vs serverless, unbounded fan-out — a design can be fast and ruinously expensive. SCALE-INFERENCE ANCHOR (G-9 — small-scale default): assume small scale unless the plan supplies evidence (a scale estimate, a profiling result, or an explicit AC). Never assume higher scale than the evidence supports. Prohibited reasoning: do not interpolate volume from the subject matter — "handles millions" or "it's a government portal" are NOT usable estimates; scale sensitivity is orthogonal to volume. This bar is two-directional: fire a perf finding only on evidenced scale, and do NOT demand scale handling the plan never claims.

Checklist:
- Hot-path operations have time-bounded done-definitions; no synchronous blocking on a hot path.
- No N+1 / redundant API or LLM calls / unbounded memory growth.
- Input-size limits, concurrency/rate-limit/pool handling, and load expectations stated.
- Per-call $ economics sound (no per-item LLM/embedding call, egress, always-on, unbounded fan-out) — fast can still be ruinously expensive.

## T5b
**Reliability (overlay)** — exec:1-TURN, advisory, facet:overlay-reliability

OVERLAY — apply only if the plan adds failure points (external integration, file I/O, LLM calls), write operations, or stateful transitions; otherwise PASS as not-applicable. Binary checks: (a) error_handling — retry/backoff/circuit-breaker/graceful-degradation is present and error states are surfaced, not swallowed; (b) failover — recovery happens without data loss or corruption, writes are idempotent, partial state is safe/durable. SEVERITY: an external call with NO error handling is MAJOR; missing idempotency on a write is MAJOR. Blast-radius is a tiebreaker that only LOWERS severity, never raises it. ANTI-FP: failover is not-applicable (PASS) if there are no writes/state/external deps. PASS if the plan fails safely. ALSO check OBSERVABILITY (are new failure points instrumented with a metric/log/trace/alert so operators can see and debug them?) and DEPENDENCY-FAILURE blast radius (when a hard external dep is down/slow: timeout, circuit-breaker, fallback/degraded-mode, or does the feature — or an unrelated one — go down?).

Checklist:
- Retry/backoff/circuit-breaker/graceful-degradation present; error states surfaced, not swallowed.
- Recovery without data loss/corruption; writes idempotent; partial state safe/durable.
- New failure points instrumented with a metric/log/trace/alert.
- When a hard external dep is down/slow: timeout, circuit-breaker, fallback/degraded-mode — feature (and unrelated ones) stay up.

## T5c
**Security (overlay)** — exec:AGENT, advisory, facet:overlay-security

OVERLAY — SECURITY, scoped by an explicit TRUST-BOUNDARY gate. Apply the gate FIRST, then the per-dimension checks only where the gate opens.

TRUST-BOUNDARY SCOPE GATE (apply first). A security concern is in scope ONLY when the plan introduces or exposes a component that is REACHABLE BY A LOWER-TRUST ACTOR: the public internet, another tenant, an untrusted network, or an unauthenticated user. This is the reachability test (STRIDE-Spoofing / OWASP-ASVS enforcement-point / exploitability-over-category framing) — a category alone is never the finding; a crossed boundary is. DERIVE the boundary from THIS application's ACTUAL domain — do NOT import generic web-app concepts (a 'declared access level', endpoint authn) the application does not have; a requirement the domain does not contain is a FALSE POSITIVE, not a gap. If the plan crosses no such boundary (a pure library / in-process module / single-user local CLI / loopback-only / git-backed tool with no network or auth surface), PASS as not-applicable and demand no auth.

- MIXED-SCOPE plans: when a plan introduces BOTH a boundary-crossing surface AND purely local/in-process components, apply every sub-check ONLY to the boundary-crossing components; the local/in-process parts of the same plan stay not-applicable even though the plan as a whole opened the gate. Do NOT scrutinise in-process logic for auth/encryption because a sibling network surface exists.
- AMBIGUOUS reachability (fallback): if the plan does not state whether a component is network-reachable, treat it as NOT-applicable (do not assume exposure) and note the assumption in the finding rationale — the gate is deny-to-fire, not deny-by-default, so silence on exposure means out of scope, not a gap.

Where the gate OPENS, apply the SAME gate to each dimension — each fires ONLY at the point a lower-trust actor can reach the surface (OWASP only where the category applies): (a) AUTHN/AUTHZ — boundary-crossing sensitive paths use the app's own auth mechanism; ambiguity about the access level of a boundary-crossing path is itself the failure; (b) ENCRYPTION IN TRANSIT (and at rest where data is actually stored) — data crossing the boundary is protected; plaintext over an untrusted network is a gap; (c) LEAST-PRIVILEGE — any new credential/role/grant a boundary-crossing component holds is scoped to the minimum (no wildcard / admin-for-convenience); (d) SECRET LIFECYCLE — no plaintext secrets in code/IaC/logs on the boundary-crossing path; use a secrets manager.

ANTI-FP: do NOT flag 'leakage' of data that is ALREADY in the ticket/repo — review findings that also live in the repo leak nothing; secrets sitting in tickets/the repo are an UPSTREAM concern, not this review's. IN-PLAN ASSETS ONLY: a finding must name the plan section/step that CREATES, MODIFIES, or TRANSMITS the asset at issue; an asset the plan does not touch (an existing credential, service, or store that is merely adjacent — e.g. demanding rotation of a password when the plan only destroys a separate copy of it) is OUT OF SCOPE and must not be a finding — pre-existing posture is an upstream concern, not this plan's gap. SAME-ROUND DEDUP: emit ONE finding per (surface, defect) pair; when several dimensions above implicate the same surface and defect, fold the additional angles into that one finding's rationale rather than emitting them as separate findings. SEVERITY priors (this overlay does not set a severity field directly; these bias how hard you press): an undeclared sensitive surface reachable by a lower-trust actor, or a plaintext secret on that path, is HIGH. ZERO-TRUST CAVEAT: a single-tenant / private-network / internal-only deployment is NOT exempt — a boundary still exists at reduced blast radius, so raise it at LOWER severity (advisory, not a blocking gap) rather than passing it silently. PASS if every boundary the plan crosses has an explicit, sound security contract — and PASS not-applicable when the plan crosses no boundary at all. SCOPE NOTE: this is the T5c overlay ONLY; the infra overlay T10's endpoint-access-contract check stays focused on infra contract-completeness and is NOT governed by this general trust-boundary framing (no blurring between the two).

AMBIGUITY, NOT ABSENCE (access framing): the finding on a boundary-crossing path is an UNDECLARED access posture, not a missing control. Every new endpoint / data path should DECLARE its access class — public-with-rationale / authenticated-with-roles / internal-only; an undeclared posture is the finding. This framing catches more real defects (silent omissions) AND kills the false positive where the plan deliberately chose public/permissive and SAID SO.

Checklist:
- SCOPE GATE (apply first): a security concern is in scope ONLY when the plan exposes a component reachable by a LOWER-trust actor (public internet, another tenant, untrusted network, unauthenticated user). In-process / local-CLI / loopback-only surfaces → PASS not-applicable; a plan silent on network-reachability is treated as not-applicable (note the assumption). Mixed-scope plans apply the sub-checks below ONLY to the boundary-crossing components. The same gate governs every dimension below; a single-tenant / private network is LOWER severity (advisory), not exempt (zero-trust).
- Where a boundary is crossed, every new endpoint/path declares its access level; sensitive paths require the app's own auth (ambiguity about access level is itself the failure) — cite OWASP category.
- Encryption in transit for data crossing the boundary (and at rest where stored); PII/secrets identified; no secret-logging or data leakage.
- Any credential/role/grant a boundary-crossing component holds is scoped to the minimum; no wildcard *:* / admin-for-convenience.
- No plaintext secrets in code/IaC/logs on the boundary-crossing path; use a secrets manager.

## T5d
**Accessibility [overlay]** — exec:1-TURN, advisory, facet:overlay-a11y

OVERLAY — apply only if the plan introduces new user-facing UI; else PASS not-applicable. Binary checks: (a) wcag_compliance — does the scope address WCAG 2.1 AA with observable a11y done-definitions (keyboard, screen-reader, contrast)? (b) inclusive_ux — reduced motion, keyboard-only, screen-reader, touch-target sizing, not color-alone/mouse-only. SEVERITY: a new interactive surface with no keyboard nav = MAJOR — cite the WCAG criterion. ANTI-FP: not-applicable for backend/infra/data work.

Checklist:
- Scope addresses WCAG 2.1 AA with observable a11y done-definitions (keyboard, screen-reader, contrast) — cite the WCAG criterion.
- Reduced motion, keyboard-only, screen-reader, touch-target sizing; not color-alone/mouse-only.

## T5e
**Maintainability (overlay)** — exec:1-TURN, advisory, facet:overlay-maintainability

OVERLAY — apply only if the plan crosses component boundaries, adds business rules/thresholds/integration points, or introduces a new pattern/contract/pipeline stage; otherwise PASS as not-applicable. Binary checks: (a) coupling_risk — new cross-component dependencies are acknowledged, justified, and mitigated (via an interface or event boundary), not silently introduced; (b) changeability — rules/thresholds expected to evolve are configurable, not hardcoded; (c) documentation — a novel architectural decision is captured in an ADR / AGENTS.md / design doc update. SEVERITY: a new pipeline stage or cross-component coupling with no doc/ADR is MAJOR on documentation; hardcoded soon-to-change thresholds are MINOR. ANTI-FP: each sub-check is not-applicable (PASS) where the plan introduces no new coupling/rules/decisions. PASS if the change keeps the system maintainable.

Checklist:
- New cross-component dependencies acknowledged, justified, mitigated (interface/event boundary), not silently introduced.
- Rules/thresholds expected to evolve are configurable, not hardcoded.
- A novel architectural decision is captured in an ADR / AGENTS.md / design-doc update.

## T6
**UX non-happy-path [overlay]** — exec:1-TURN, advisory, facet:overlay-ux

OVERLAY — apply only if the plan introduces a user-facing interaction surface; else PASS not-applicable. Checks: (a) criticality — are the highest-stakes interactions named? (b) non_happy_path — validation/timeout/empty/partial-data/error states handled, not just the happy path? (c) flow_entry_exit — entry plus both success and abandon exit points covered? SEVERITY: a new interactive flow with only the happy path = MAJOR. ANTI-FP: not-applicable for backend/infra/data work.

Checklist:
- Highest-stakes interactions are named.
- Validation/timeout/empty/partial-data/error states handled, not just the happy path.
- Entry plus both success and abandon exit points are covered.

## T7
**Documentation [overlay]** — exec:1-TURN, advisory, facet:overlay-docs

OVERLAY — apply when the plan introduces something that needs documenting or invalidates existing docs; else PASS not-applicable. Checks: (a) NEW-needed — a new pattern/contract/config/CLI gets a doc/ADR? (b) INVALIDATED — does the change make existing docs/references stale (deleted/renamed artifacts still referenced)? (c) not-excessive / navigable — large docs have structure; no hot-path instruction-bloat. SEVERITY: a new architectural decision with no ADR/doc, or a change that strands stale references = MAJOR. ANTI-FP: trivial/internal changes need no doc.

Checklist:
- A new pattern/contract/config/CLI gets a doc/ADR.
- The change doesn't strand stale references (deleted/renamed artifacts still referenced).
- Large docs have structure; no hot-path instruction-bloat.

## T8
**LLM / prompt structural-completeness probe [overlay]** — exec:AGENT, blocking, facet:overlay-llm

OVERLAY — apply when the plan defines an LLM/agent system (prompts, sub-agents, reviewers, output schemas, enums). Probe (tool-grounded) for STRUCTURAL GAPS a generic checklist misses: (a) a schema/enum referenced but whose value vocabulary is never defined; (b) a processing protocol/decision rule referenced but not co-located with the schema that needs it; (c) a counter/state increment with ambiguous placement; (d) an unspecified fallback for an incomplete/failed sub-step; (e) instruction-locality / pink-elephant antipatterns. Use Grep/Read to confirm referenced agents/skills/enums exist and are fully specified. Report each PROVEN gap with evidence. SEVERITY: an undefined-but-referenced enum/protocol an executor needs = MAJOR. ANTI-FP: cite concrete evidence; this is the overlay that recovers the structural-gap signal a generic checklist misses.

Checklist:
- No schema/enum referenced whose value vocabulary is never defined.
- A processing protocol/decision rule is co-located with the schema that needs it.
- No counter/state increment with ambiguous placement.
- A fallback for an incomplete/failed sub-step is specified.
- No instruction-locality / pink-elephant antipatterns; referenced agents/skills/enums exist and are fully specified (tool-verified).

## T9
**Shared-state lifecycle [overlay]** — exec:1-TURN, advisory, facet:overlay-sharedstate

OVERLAY — apply when the plan introduces or mutates shared/global state (a cache, singleton, config key, shared file/record, or a stateful lifecycle); else PASS not-applicable. Check the full CREATE / UPDATE / CONSUME / RETIRE lifecycle: (a) who creates the state and when? (b) update concurrency / ownership clear? (c) consumers enumerated and tolerant of its absence/staleness? (d) is there a RETIRE/cleanup path, or does it leak/accumulate? SEVERITY: shared state with no defined ownership or no retirement path = MAJOR. ANTI-FP: not-applicable for purely local/stateless changes. ALSO assess CONCURRENCY SAFETY (distinct from lifecycle completeness): is shared/mutable state mutated atomically (lock/CAS/transaction, no check-then-act TOCTOU), and is the operation idempotent under retry / at-least-once delivery? A fully-specified lifecycle can still have a race.

Checklist:
- Who creates the state and when is defined.
- Update concurrency / ownership is clear.
- Consumers enumerated and tolerant of absence/staleness.
- There is a RETIRE/cleanup path — it doesn't leak/accumulate.
- Shared/mutable state mutated atomically (lock/CAS/transaction, no check-then-act TOCTOU); operation idempotent under retry/at-least-once.

## hedge
**Hedged-requirement provenance** — exec:1-TURN, advisory, facet:codebase-grounding

Scan the plan for HEDGED requirements or design premises — a committed element stated with a hedge
that signals unverified inference rather than established fact. Hedge frames: "probably",
"we assume", "should return", "presumably", "I think", "likely", "seems to", "as far as I know",
"in theory". Surface a finding when a COMMITTED element (an acceptance criterion, a task, an edit,
or a scope decision) rests on such a hedged assertion with no verification and no fallback — Pass-2
then grades its substance via `committed_work_relies_on_unbacked_claim` (a real dependence on an
unbacked claim upholds the finding; a hedge on a non-committed aside dissolves it).

Judge SUBSTANCE, not word-presence: a hedge on already-verified prose, on a premise the plan
explicitly flags as an assumption to test, or on a non-load-bearing aside is not a finding. When
no session log is linked, a hedged requirement is exactly the provenance-lite signal to route to
the riskiest-assumption coach move.

AC-CLAUSE SUPPRESSION (dedup vs E6.no_hedges — rubric-level, no pipeline change): if the hedge
sits inside an ACCEPTANCE-CRITERION clause where it stands in for a proving command ("should work",
"probably passes"), that is E6's `no_hedges` territory — mark THIS criterion not-applicable and do
NOT report it; E6 owns and reports that case. This criterion targets hedged requirements and design
premises OUTSIDE the AC proving-command surface, so the two never double-report the same hedge.

PASS (emit no finding) when the plan's committed elements rest only on verified premises or on
premises it explicitly flags as assumptions to validate.

Checklist:
- A COMMITTED element (AC/task/edit/scope) resting on a HEDGED assertion ('probably','we assume','should return') with no verification or fallback is surfaced; Pass-2 grades its substance via committed_work_relies_on_unbacked_claim.
- Judge substance, not word-presence: a hedge on already-verified prose, an explicitly-flagged assumption, or a non-load-bearing aside is not a finding.
- AC-clause dedup (rubric-level): a hedge inside an AC proving-command clause is E6.no_hedges territory — mark not-applicable; E6 owns it. This targets hedged requirements/premises outside the AC surface.

## removal-rationale
**Removal rationale (Chesterton's Fence)** — exec:AGENT, advisory, facet:codebase-grounding

OVERLAY / GATE — apply only when the plan REMOVES or WEAKENS something whose purpose may be
non-obvious. Fire if ANY of these bright-line triggers holds (a disjunction — no subjective
"is this incidental?" judgment):

1. The plan removes or weakens an EXTERNALLY-OBSERVABLE behavior or contract on ANY path —
   including failure / timeout / invalid / boundary / exception semantics. "Internal" is defined by
   observable-behavior PRESERVATION, not file locality: a refactor that swallows an error, changes
   an exception type, turns retry into fail-fast, or shrinks a timeout is NOT exempt — failure-mode
   behavior is outward-facing.
2. The plan removes or weakens a check / test / validation that GUARDS such a behavior. (Tests are
   in scope; any overlap with E5's changed-behavior-tests is resolved by the Pass-4 coaching pass
   grouping the two, not by partitioning the criteria.)
3. The plan removes an artifact carrying an EXPLICIT INTENT MARKER — an explanatory comment, a
   `# do not remove`, a referenced bug/ticket, or a test named after a bug. Use your tools to
   confirm the marker exists (Grep/Read/blame); this is objective, grep-able, not a vibe.

EXEMPT (PASS / not-applicable) — rebar values legitimate simplification, so this criterion must
never nag it: dead-code removal, a pure internal simplification that preserves ALL observable
behavior including error/failure semantics, and mechanical/config/doc changes with no behavioral
delta.

PASS DEMONSTRATION (the intent is "show we understand what we're changing," NOT "poke holes"): the
plan must supply a concrete TRIGGERING SCENARIO — the input/condition under which the removed
behavior or guard mattered — GROUNDED in evidence (the explanatory comment / a pinning test /
git-blame / a linked ticket / a spec-named input class), NOT invented — PLUS evidence the reason no
longer applies (handled elsewhere / precondition now guaranteed / contract intentionally changed and
updated). Verify the cited grounding with your tools; a scenario you cannot corroborate in the code
is ungrounded. This grounded scenario is a specification-by-example of the fence (coach move 6).

CHECKLIST SUB-ANSWERS (criterion-local):
- removes_external_behavior_or_guarded_fence {yes|no|insufficient} — the GATE (the disjunction
  above). `no` → not-applicable → PASS.
- removal_scenario_grounded {yes|no|insufficient} — only meaningful when gated in: does the plan
  give a concrete scenario where the removed behavior/guard mattered, GROUNDED in a
  comment/test/blame/linked-ticket (not invented), plus evidence the reason no longer applies? A
  fabricated or ungrounded justification is `no`.

ACCEPTED LIMITATION (log in coverage, do not hide): a purely-latent guard whose removal changes
behavior only for inputs never exercised today AND which carries no intent marker will NOT fire — it
is indistinguishable from dead code without an external signal, and chasing it is the un-scalable
nag we are avoiding. Record this as a coverage note, not a silent cap.

ADVISORY: this criterion errs toward surfacing and coaches; it does not block a claim.

Checklist:
- GATE (Chesterton's Fence): the plan removes/weakens an externally-observable behavior or contract on ANY path (incl. failure/timeout/invalid/boundary/exception semantics), OR removes a check/test/validation guarding one, OR removes an artifact with an explicit intent marker (comment / #do-not-remove / referenced bug / bug-named test). no -> not-applicable.
- Only when gated in: the plan supplies a concrete TRIGGERING SCENARIO where the removed behavior/guard mattered, GROUNDED in a comment/test/blame/linked-ticket (verify with tools, not invented), plus evidence the reason no longer applies. A fabricated/ungrounded justification is no (low validity). Coach via move 6 (specification-by-example).
