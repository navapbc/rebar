#!/usr/bin/env python3
"""Build criteria_v7.json from criteria_v6.json.

v7 adds three things the finalize round called for, all *declarative on the descriptor*
(so the future orchestrator reads them instead of hard-coding them in harness logic):

  (D) checklist[]      — the prose "Binary checks: (a)..(b).." lifted into a structured
                         array of {key, check} so scoring can be per-item binary (the Q10
                         follow-up; CheckEval/TICK basis).
  (A) E5 retune        — type/level suppression + a raised bar (thin-but-present -> PASS),
                         the round-4/5 over-fire (9/12) fix, now declarative.
      applies_at{}     — proportionate scrutiny lifted out of retune.py into the registry:
                         levels[] (epic|story|task), container_only, suppress_types[],
                         suppress_when[]  (e.g. test_task / mechanical_leaf).
  (B) exec routing     — the codebase-grounded criteria that hedge AMBIGUOUS without tools
                         routed to the AGENT tier where they can resolve to a concrete
                         verdict. Evidence-gated subset (see ROUTING below); validated by
                         the AGENT-vs-single-turn A/B in the finalize experiments.

Run:  python make_crit_v7.py   ->   writes criteria_v7.json next to this file.
Then: python ../harnesses/check_registry_coverage.py criteria_v7.json
"""
import json, os

HERE = os.path.dirname(__file__)
V6 = json.load(open(os.path.join(HERE, "criteria_v6.json")))

# ---------------------------------------------------------------------------
# (A) applies_at — proportionate scrutiny, lifted from retune.py into the registry.
#   levels:         which altitudes the criterion runs at (task == leaf).
#   container_only: only when has_children (the G3/G4 coverage/consistency checks).
#   suppress_types: ticket types the criterion never runs on.
#   suppress_when:  named conditions the orchestrator computes (test_task, mechanical_leaf).
# Bugs are exempt from the whole gate (epic design -> follow-on F6); encoded as
# suppress_types:["bug"] on every criterion for a single uniform filter.
# ---------------------------------------------------------------------------
ALL = ["epic", "story", "task"]
LEAF = ["task"]
STORY_TASK = ["story", "task"]

APPLIES = {
    # universal judgment — every altitude
    "F1":  {"levels": ALL},
    "E1":  {"levels": ALL},
    "E2":  {"levels": ALL},
    "E3":  {"levels": ALL},
    "G5":  {"levels": ALL},
    "COH": {"levels": ALL},
    # value/beneficiary: N/A on internal/mechanical leaf tasks (round-4 TYPE rule)
    "F4":  {"levels": ALL, "suppress_when": ["mechanical_leaf"]},
    # testing: story+task only (epics defer tests to children); off on test-tasks (the task IS the test)
    "E5":  {"levels": STORY_TASK, "suppress_when": ["test_task"]},
    # leaf/implementation-grain — suppress above task (round-4 LEAF_ONLY + the agent grounding set)
    "E6":   {"levels": LEAF},
    "G1G2": {"levels": LEAF},
    "E4":   {"levels": LEAF},
    "A1":   {"levels": LEAF},
    # container coverage/consistency — only when the ticket has children
    "G3":  {"levels": ["epic", "story"], "container_only": True},
    "G4":  {"levels": ["epic", "story"], "container_only": True},
    # maintainability: all levels (round-4 ALL_LEVEL); approach-soundness: all levels, trigger-gated
    "T5e": {"levels": ALL},
    "G6":  {"levels": ALL},
    # empirical probe: all levels, trigger-gated
    "T2":  {"levels": ALL},
    # leaf-grain overlays (round-4 LEAF_ONLY) — proving/impl detail
    "T5a": {"levels": LEAF},
    "T5b": {"levels": LEAF},
    "T5c": {"levels": LEAF},
    # work-property overlays — apply at any altitude where the trigger fires
    "T1":  {"levels": ALL},
    "T3":  {"levels": ALL},
    "T4":  {"levels": ALL},
    "T5d": {"levels": STORY_TASK},   # a11y is design+impl grain; not epic
    "T6":  {"levels": STORY_TASK},   # UX non-happy-path: story+task
    "T7":  {"levels": ALL},
    "T8":  {"levels": ALL},
    "T9":  {"levels": STORY_TASK},
    "T10": {"levels": ALL},
    "T11": {"levels": ALL},
    "T12": {"levels": ALL},
}

# ---------------------------------------------------------------------------
# (B) exec routing — flip codebase-grounded overlays that hedge AMBIGUOUS-without-tools
# to AGENT. The round-5 scorecard finding: "codebase-grounded criteria hedge AMBIGUOUS
# without tools; agentic grounding resolves them." Evidence-gated subset:
#   - T10 (IaC), T11 (migration): verifying remote-state / scoped-IAM / expand-contract
#     against the ACTUAL .tf / schema needs the repo. -> AGENT.
#   - G6/G1G2/E4/A1/T1/T3/T8/G3/G4: already AGENT in v6.
#   - T9 (shared-state) and COH (cross-section coherence) are DELIBERATELY kept 1-TURN:
#     COH is text-internal (contradictions BETWEEN PLAN SECTIONS — tools don't help);
#     T9's concurrency-safety is plan-text-judgable. Their AMBIGUOUS-on-clean is a
#     *decisiveness* problem, addressed by the SYSTEM decisiveness lever, not tooling.
#     (This corrects the STATUS list, which named COH/T9 for AGENT; validated in the
#     finalize AGENT-routing A/B.)
# ---------------------------------------------------------------------------
TO_AGENT = {"T10", "T11"}

# ---------------------------------------------------------------------------
# (D) checklist[] — the binary sub-checks, lifted from each scenario's prose.
# Each item: {key, check}. Authored to match the (a)(b).. enumerations in v6 scenarios.
# ---------------------------------------------------------------------------
CHECKLIST = {
 "F1": [
   {"key": "observable_outcome", "check": "Each criterion states a specific observable outcome (what changes for user/system), not effort or a subjective term."},
   {"key": "in_session_evaluable", "check": "Evaluable in-session via repo artifacts / closing PR CI / a deterministic command — not post-sprint-only (multi-day telemetry, adoption %, survey)."},
   {"key": "durable_end_state", "check": "A durable end-state, not a one-time transition (could be false before this work and true only because of it)."},
   {"key": "right_sized", "check": "A coherent single-outcome deliverable — not an epic-of-epics, not a one-line triviality."},
 ],
 "F4": [
   {"key": "named_beneficiary", "check": "Names a specific user/stakeholder and the problem they have today."},
   {"key": "observable_value", "check": "Criteria represent an observable improvement to that user / a measurable business outcome, not pure system internals."},
   {"key": "not_bare_task", "check": "Not a bare technical task with no named beneficiary."},
   {"key": "value_validation", "check": "At least one criterion carries a concrete validation mechanism (before/after, operational metric, dogfooding)."},
 ],
 "E1": [
   {"key": "bidirectional_coverage", "check": "Every criterion maps to described work AND every described deliverable has a covering criterion (no orphans, no uncovered work)."},
   {"key": "terminology_consistent", "check": "Same concept named the same way throughout; a criterion's verify step references the same entity its text names."},
   {"key": "no_duplicates", "check": "No duplicate or near-duplicate requirements."},
   {"key": "migration_both_sides", "check": "For migrations, criteria verify BOTH removal and replacement."},
 ],
 "E2": [
   {"key": "scope_boundaries", "check": "No undefined scope boundaries ('improve performance' — of what, by how much)."},
   {"key": "explicit_ac", "check": "Acceptance criteria / types / size limits are stated, not implicit."},
   {"key": "no_conflicting_signals", "check": "Title and body agree; no conflicting signals."},
   {"key": "persona_present", "check": "The persona (admin vs end-user) is identified where it matters."},
   {"key": "constraints_stated", "check": "Constraints stated (e.g. an API's auth / rate-limit)."},
   {"key": "priority_ranked", "check": "Essential vs nice-to-have is ranked."},
   {"key": "no_placeholder", "check": "No scope bullet is a placeholder ('verify whether','check if','TBD','figure out','choose an appropriate X') deferring a real decision to the executor."},
 ],
 "E3": [
   {"key": "matches_headline", "check": "Body work matches headline intent — no scope drift doing MORE or LESS than the title promises."},
   {"key": "goals_proven", "check": "Each non-deferred goal has a faithful, end-state-observable proof."},
   {"key": "no_contradiction", "check": "No step contradicts the stated intent."},
   {"key": "callers_acknowledged", "check": "Where behavior changes, callers depending on old behavior are acknowledged."},
 ],
 "E5": [
   {"key": "introduces_testable", "check": "GATE: the plan introduces new logic/behavior that is testable (else PASS not-applicable — refactor/config/doc/mechanical)."},
   {"key": "beyond_happy_path", "check": "For new user-facing behavior, failure/timeout/invalid/empty paths and a caller-facing contract are addressed."},
   {"key": "boundaries", "check": "Boundary scenarios considered (oversized input, malformed payload, non-Latin, back-button)."},
   {"key": "no_self_oracle", "check": "No self-authored-oracle / change-detector anti-pattern: snapshot-of-current-output, tautology, or source-grep masquerading as a behavioral test (MAJOR — locks in the bug)."},
   {"key": "changed_behavior_tests", "check": "Changed/deleted behaviors get corresponding modify/remove-test work."},
 ],
 "E6": [
   {"key": "proving_command", "check": "Every completion-relevant claim has a concrete proving command/check that produces evidence on success."},
   {"key": "no_hedges", "check": "No red-flag hedges ('should','probably','seems') standing in for a proving command."},
   {"key": "ac_to_step", "check": "Every acceptance criterion maps to at least one described step."},
   {"key": "end_state_reachable", "check": "The union of steps actually reaches the stated end-state (no gap between 'what we'll do' and 'what done looks like')."},
   {"key": "e2e_for_user_flows", "check": "User-facing flows have an end-to-end check or a documented rationale for its absence."},
 ],
 "G5": [
   {"key": "sizing_signals", "check": ">3 files OR >=3 layers OR a new interface OR low scope-certainty pushes toward decompose."},
   {"key": "parent_single_concern", "check": "An epic/parent passes single-concern (no structural 'and' joining independent goals; not multi-persona/UI+backend/with-migration/>6 criteria) or has children."},
   {"key": "decomposition_present", "check": "Decomposition into children is present and sensible where size demands it."},
   {"key": "leaf_coherent", "check": "A leaf is small enough to execute in one session (and not a one-criterion triviality)."},
   {"key": "yagni", "check": "Proposed structure is justified by current criteria (>=3 real call-sites for any new abstraction)."},
   {"key": "vertical_slice", "check": "Sequencing has a thin vertical-slice / evidence-gated MVP de-risking the riskiest piece first, not a horizontal big-bang."},
 ],
 "T5a": [
   {"key": "latency", "check": "Hot-path operations have time-bounded done-definitions; no synchronous blocking on a hot path."},
   {"key": "resource_efficiency", "check": "No N+1 / redundant API or LLM calls / unbounded memory growth."},
   {"key": "scalability", "check": "Input-size limits, concurrency/rate-limit/pool handling, and load expectations stated."},
   {"key": "cost", "check": "Per-call $ economics sound (no per-item LLM/embedding call, egress, always-on, unbounded fan-out) — fast can still be ruinously expensive."},
 ],
 "T5b": [
   {"key": "error_handling", "check": "Retry/backoff/circuit-breaker/graceful-degradation present; error states surfaced, not swallowed."},
   {"key": "failover", "check": "Recovery without data loss/corruption; writes idempotent; partial state safe/durable."},
   {"key": "observability", "check": "New failure points instrumented with a metric/log/trace/alert."},
   {"key": "dependency_blast_radius", "check": "When a hard external dep is down/slow: timeout, circuit-breaker, fallback/degraded-mode — feature (and unrelated ones) stay up."},
 ],
 "T5c": [
   {"key": "access_classification", "check": "Every new endpoint/path declares its access level; sensitive paths require auth (ambiguity about access level is itself the failure) — cite OWASP category."},
   {"key": "data_protection", "check": "Encryption at rest/in transit where applicable; PII/secrets identified; no secret-logging or data leakage."},
   {"key": "least_privilege", "check": "Grants scoped to the minimum; no wildcard *:* / admin-for-convenience."},
   {"key": "secret_lifecycle", "check": "No plaintext secrets in code/IaC/logs; use a secrets manager."},
 ],
 "T5e": [
   {"key": "coupling_risk", "check": "New cross-component dependencies acknowledged, justified, mitigated (interface/event boundary), not silently introduced."},
   {"key": "changeability", "check": "Rules/thresholds expected to evolve are configurable, not hardcoded."},
   {"key": "documentation", "check": "A novel architectural decision is captured in an ADR / CLAUDE.md / design-doc update."},
 ],
 "T2": [
   {"key": "risk_present", "check": "STEP 1: the plan is complex/novel/uncertain (novel architecture, unverified external/scale assumption, asserted-not-derived default, correctness un-establishable without running)."},
   {"key": "validation_planned", "check": "STEP 2: if risky, the plan already includes a spike/probe/prototype/benchmark/RED-fixture/pilot-with-metrics — else recommend the specific one."},
   {"key": "suppress_if_experimenting", "check": "ANTI-FP: do NOT fire when experimentation is already present; a mechanical/fully-specified change is not complex."},
 ],
 "T3": [
   {"key": "technical_feasibility", "check": "The integration is achievable as described, or a capability gap is named."},
   {"key": "surface_exists", "check": "Named subcommands/endpoints actually exist (verify against --help/docs) — MATCH/MISMATCH/UNVERIFIED."},
   {"key": "auth_preconditions", "check": "Auth/HTTPS preconditions stated."},
   {"key": "gap_routes_to_spike", "check": "A critical capability gap routes to a SPIKE before committing the full plan."},
 ],
 "T4": [
   {"key": "unacknowledged_breakage", "check": "A change/removal consumers rely on is acknowledged with expand-contract or a rollback path."},
   {"key": "gratuitous_compat", "check": "No unwarranted backward-compat shims / feature flags / version branches."},
   {"key": "destructive_is_explicit", "check": "Any destructive/irreversible step is an explicit, justified choice."},
   {"key": "reversibility_mechanism", "check": "The remedy is an explicit rollback/back-out plan or expand-contract — acknowledgement alone is insufficient."},
 ],
 "T5d": [
   {"key": "wcag_compliance", "check": "Scope addresses WCAG 2.1 AA with observable a11y done-definitions (keyboard, screen-reader, contrast) — cite the WCAG criterion."},
   {"key": "inclusive_ux", "check": "Reduced motion, keyboard-only, screen-reader, touch-target sizing; not color-alone/mouse-only."},
 ],
 "COH": [
   {"key": "cross_section_contradiction", "check": "No contradiction BETWEEN sections (testing vs decomposition; sequencing vs declared deps; context/problem vs success criteria; approach vs a stated constraint)."},
   {"key": "not_within_section", "check": "ANTI-FP: only genuine cross-section contradictions, not within-section nitpicks (those belong to E1/E2)."},
 ],
 "T1": [
   {"key": "prior_art", "check": "Relevant prior art is considered before committing — not reinventing/repackaging something that exists."},
   {"key": "novelty_justified", "check": "A novel pattern's novelty is justified vs an established approach (anti-repackaging, Rule-of-Three)."},
   {"key": "capability_assertions_resolved", "check": "Unverified capability assertions ('library supports X') are resolved."},
 ],
 "T6": [
   {"key": "criticality", "check": "Highest-stakes interactions are named."},
   {"key": "non_happy_path", "check": "Validation/timeout/empty/partial-data/error states handled, not just the happy path."},
   {"key": "flow_entry_exit", "check": "Entry plus both success and abandon exit points are covered."},
 ],
 "T7": [
   {"key": "new_needed", "check": "A new pattern/contract/config/CLI gets a doc/ADR."},
   {"key": "invalidated", "check": "The change doesn't strand stale references (deleted/renamed artifacts still referenced)."},
   {"key": "navigable", "check": "Large docs have structure; no hot-path instruction-bloat."},
 ],
 "T8": [
   {"key": "enum_vocab_defined", "check": "No schema/enum referenced whose value vocabulary is never defined."},
   {"key": "protocol_colocated", "check": "A processing protocol/decision rule is co-located with the schema that needs it."},
   {"key": "counter_placement", "check": "No counter/state increment with ambiguous placement."},
   {"key": "fallback_specified", "check": "A fallback for an incomplete/failed sub-step is specified."},
   {"key": "instruction_locality", "check": "No instruction-locality / pink-elephant antipatterns; referenced agents/skills/enums exist and are fully specified (tool-verified)."},
 ],
 "T9": [
   {"key": "create", "check": "Who creates the state and when is defined."},
   {"key": "update", "check": "Update concurrency / ownership is clear."},
   {"key": "consume", "check": "Consumers enumerated and tolerant of absence/staleness."},
   {"key": "retire", "check": "There is a RETIRE/cleanup path — it doesn't leak/accumulate."},
   {"key": "concurrency_safe", "check": "Shared/mutable state mutated atomically (lock/CAS/transaction, no check-then-act TOCTOU); operation idempotent under retry/at-least-once."},
 ],
 "T10": [
   {"key": "state", "check": "Remote state + locking (no local state); plan-before-apply discipline."},
   {"key": "least_privilege_iam", "check": "Roles/policies scoped to minimum; no wildcard *:* / admin-for-convenience; no long-lived creds committed."},
   {"key": "idempotency_drift", "check": "Changes idempotent; drift / out-of-band manual changes considered."},
   {"key": "blast_radius", "check": "Dev/stage/prod separation; destroy/replace safety; prevent_destroy on stateful resources (RDS/S3/volumes)."},
   {"key": "secrets", "check": "No plaintext secrets in IaC/vars; secrets manager / SSM / vault."},
   {"key": "cost_sizing", "check": "Obviously-expensive or unbounded resources flagged; limits/autoscaling/quotas considered."},
   {"key": "observability_ownership", "check": "Logging/metrics/alarms for new infra; reproducible as-code with a clear teardown."},
 ],
 "T11": [
   {"key": "expand_contract", "check": "Migration runs without downtime via expand-contract (add nullable -> backfill -> enforce), not a single blocking DDL on a large table."},
   {"key": "batching_scale", "check": "Large backfills are batched/throttled, not one giant transaction."},
   {"key": "resumability", "check": "A partially-completed migration is resumable/idempotent."},
   {"key": "dual_write_window", "check": "Rows written DURING the migration are handled (no lost writes between backfill and cutover)."},
   {"key": "rollback_no_data_loss", "check": "There is a back-out path and no data loss on partial failure."},
 ],
 "T12": [
   {"key": "staged_rollout", "check": "A behavior change reaches prod via flag/canary/staged rollout, not a single 100%-traffic flip."},
   {"key": "rollback", "check": "An explicit, cheap, tested way to undo quickly without data cleanup."},
   {"key": "deploy_ordering", "check": "If producers/consumers/coordinated services change, deploy order and old+new coexistence is specified."},
 ],
 "G6": [
   {"key": "mechanism_correctness", "check": "The proposed mechanism actually achieves the goal — logic/data-flow complete; edge/empty/concurrent/failure handled; no hidden ordering/atomicity assumption (e.g. check-then-act idempotency that is a TOCTOU race)."},
   {"key": "fitness_for_purpose", "check": "The solution solves the named problem, not a proxy."},
   {"key": "approach_selection", "check": "Reviewer generates 1-2 structurally-different alternatives and judges the chosen approach defensible on codebase-alignment/blast-radius/testability/simplicity/robustness; a clearly-superior missed alternative is a coaching finding."},
   {"key": "positive_rationale", "check": "The plan states a POSITIVE rationale for the chosen approach (its absence is a finding); do NOT require a rejected-alternatives section (anti-priming)."},
 ],
 "G1G2": [
   {"key": "targets_exist", "check": "Every file/symbol the plan names exists (Glob/Grep) — flag hallucinated/missing edit targets."},
   {"key": "consumers_enumerated", "check": "Consumers/callers OUTSIDE the artifact's dir that a change requires updating are enumerated — flag unenumerated consumers."},
   {"key": "scope_classified", "check": "Behavioral hunks classified in/ambiguous/out-of-scope (CREATION=new behavior=out-of-scope); high blast-radius alone isn't a fail if acknowledged."},
 ],
 "E4": [
   {"key": "assertions_probed", "check": "Each codebase assertion ('X exists','Y does Z', hedges/confident-assertions) is verified by a Grep/Read — training knowledge is not a substitute."},
   {"key": "fail_closed", "check": "Fail-closed: an unverifiable assertion is a gap (no benefit of the doubt)."},
   {"key": "read_before_flag", "check": "ANTI-FP: read the named implementation file before flagging a contract-doc-only claim."},
 ],
 "A1": [
   {"key": "rule_of_three", "check": "A proposed abstraction has >=3 existing call-sites or is premature (cite grep hits)."},
   {"key": "yagni", "check": "Each abstraction serves a current done-definition, not a hypothetical."},
   {"key": "nih", "check": "Doesn't rebuild functionality already in the codebase or an imported dependency (grep for it)."},
   {"key": "config_proliferation", "check": "No config-surface proliferation (a config key may already capture the toggle)."},
   {"key": "antipattern_set", "check": "Screen golden-hammer / cargo-cult / resume-driven / premature-optimization (DSO decider set), each cited."},
 ],
 "G3": [
   {"key": "coverage_map", "check": "The union of children covers each parent acceptance/success criterion — 4-bucket audit (fully/partially/uncovered/structural); an uncovered criterion is a finding."},
 ],
 "G4": [
   {"key": "interaction_modes", "check": "Check the 7 cross-child modes: implicit shared state, conflicting assumptions, dependency gap, scope overlap, ordering violation, consumer impact, residual references — each detected mode is a finding."},
 ],
}


def main():
    out = []
    for c in V6:
        cid = c["id"]
        n = dict(c)  # copy
        # (B) routing
        if cid in TO_AGENT:
            n["exec"] = "AGENT"
            n["_v7_note"] = "routed 1-TURN->AGENT in v7 (codebase-grounded; resolves AMBIGUOUS-without-tools)"
        # (A) applies_at
        if cid in APPLIES:
            ap = dict(APPLIES[cid])
            ap.setdefault("container_only", False)
            ap.setdefault("suppress_when", [])
            ap["suppress_types"] = ["bug"]  # gate is task/story/epic; bugs -> follow-on
            n["applies_at"] = ap
        # (D) checklist
        if cid in CHECKLIST:
            n["checklist"] = CHECKLIST[cid]
        out.append(n)

    # (A) E5 retune — rewrite the scenario to raise the bar + scope it (declarative bar lift)
    for n in out:
        if n["id"] == "E5":
            n["name"] = "Testing-plan completeness (retuned v7)"
            n["scenario"] = (
                "Assess whether the plan makes the work testable by construction. FIRST apply the applicability gate: "
                "fire ONLY if the plan INTRODUCES new logic/behavior that is testable. If the change is internal/mechanical "
                "(refactor, rename, config, dep-bump, doc), or testing is explicitly deferred to child tickets, PASS as "
                "not-applicable. RAISED BAR (round-4/5 over-fire fix): thin-but-present coverage is PASS, not a finding; "
                "only flag when (a) a NEW user-facing flow has happy-path-only tests with no failure/timeout/invalid/empty "
                "path; (b) a changed/deleted behavior gets no modify/remove-test work; or (c) the SELF-AUTHORED-ORACLE / "
                "change-detector anti-pattern is present — tests that snapshot current (possibly wrong) output, tautological "
                "tests, or source-greps masquerading as behavioral tests (these lock in the bug; always MAJOR). Boundary "
                "scenarios (oversized/malformed/non-Latin/back-button) and observable (not 'works correctly') outcomes are "
                "rewarded but their absence on an internal change is NOT a finding. ANTI-FP: structural greps and "
                "command-output assertions ARE a legitimate cross-language test pattern; valid TDD exemptions exist "
                "(no conditional logic, pure scaffolding, cited existing test). SEVERITY: missing failure-path on a new "
                "user-facing flow = MAJOR; change-detector/tautology = MAJOR; everything else PASS. This criterion does not "
                "run on epics (they defer tests to children) or on test-authoring tasks (the task IS the test)."
            )

    path = os.path.join(HERE, "criteria_v7.json")
    json.dump(out, open(path, "w"), indent=1, ensure_ascii=False)
    have_checklist = sum(1 for n in out if "checklist" in n)
    have_applies = sum(1 for n in out if "applies_at" in n)
    agent = [n["id"] for n in out if n.get("exec") == "AGENT"]
    print(f"wrote {path}: {len(out)} descriptors")
    print(f"  checklist[] present: {have_checklist}/{len(out)}")
    print(f"  applies_at present:  {have_applies}/{len(out)}")
    print(f"  AGENT-tier: {agent}")


if __name__ == "__main__":
    main()
