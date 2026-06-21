#!/usr/bin/env python3
"""Build criteria_v8.json from v7 — adopt the THREE-PASS review structure (epic 9da1 / log 9dba).

The user's directive: the plan-review gate adopts the three-pass standard, REPLACING per-criterion
model-emitted severity. v8 makes the registry three-pass-native:

  - severity_by="pass3": severity is NOT emitted by the Pass-1 reviewer; it is computed
    deterministically in Pass-3 from the Pass-2 verifier's severity ATTRIBUTES. The descriptor's
    prose "SEVERITY: ..." hints are retained only as priors for the verifier / the Pass-1 `impact`
    free-text; the Pass-1 tool has no severity field, so the model cannot emit one.
  - block_threshold + default_posture="advisory": the Pass-3 block|advisory decision is per-criterion
    and project-overridable; defaults start HIGH so almost everything is advisory while calibration
    data is gathered (9da1 AC). The DET floor (P1-P7, separate harness) is the only blocking tier.
  - trigger fix (E4-confirmed): T10/T11/T12 are LLM-ROUTED for PLAN review, not deterministic. E4
    showed the deterministic keyword over-fires on plans — rebar 'migration' = the bash->Python
    strangler (NOT data-migration), 'deploy'/'rollback' are ubiquitous: det T11 fired 2/19 (both
    false), T12 fired 7/19 (all false), LLM router fired 0 (correct). (On a code-review DIFF the
    file-glob triggers in the 9da1 catalog are high-precision; on a PLAN there is no diff, so route
    by LLM.)

Run:  python make_crit_v8.py   ;   python ../harnesses/check_registry_coverage.py criteria_v8.json
"""
import json, os

HERE = os.path.dirname(__file__)
V7 = json.load(open(os.path.join(HERE, "criteria_v7.json")))

# E4-confirmed: route these overlays by the LLM router on plans (deterministic keyword over-fires).
LLM_ROUTED = {"T10", "T11", "T12", "T8", "T6", "G6", "T9"}
LLM_TRIGGER_NOTE = {
    "T10": "infrastructure/IaC intent (LLM-routed for plans — deterministic IaC keywords over-fire on prose; "
           "on a code DIFF use the *.tf/Dockerfile/k8s file-glob triggers from the 9da1 IaC catalog instead)",
    "T11": "schema/data-shape change or backfill over PERSISTED data (LLM-routed — E4: deterministic 'migration' "
           "false-fires on rebar's bash->Python strangler; the LLM router correctly distinguishes data-migration)",
    "T12": "changes runtime behavior of a deployed/long-running system (LLM-routed — E4: deterministic "
           "'deploy'/'rollback' fired 7/19 plans, all false; LLM router fired 0)",
}

# Tier reclassification (the agent-vs-single-turn bright line, applied):
# G3/G4 (container checks: parent ACs vs sibling tickets — text we hold) are single-turn: no codebase probe.
RECLASSIFY_EXEC = {"G3": "1-TURN", "G4": "1-TURN"}
RECLASSIFY_NOTE = {
    "G3": "ticket-analysis: parent ACs vs child tickets (artifacts already held), fed as context; not tool-using",
    "G4": "ticket-analysis: cross-child consistency over the child tickets; code-grounding (consumer impact / residual refs) is owned by E4/G1G2/A1, not duplicated here",
}
# GROUNDING-AGENT overlays (the grounding experiment): an IMPLICATION overlay whose verdict depends on WHAT
# THE ACTUAL CODE DOES — the plan can mislead by omission OR assertion — is AGENT-tier. Settled by experiment:
#   T5c (security): single-turn speculated 'almost certainly env-var leakage'; the agent read the real
#                   Secrets-Manager + constant-time handling and correctly dismissed it.
#   T10 (infra):    same verdict, but the agent found a REAL committed HMAC secret in a zip the plan/ST missed.
#   T11 (migration): no real migration in the corpus to test -> AGENT by ANALOGY to T10 (verdict depends on the
#                   actual schema/migration); flagged untested, re-test in the eval suite.
# KEPT single-turn (experiment: verdict was PLAN-TEXT-EVIDENT, grounding non-decisive): T5b, T9, T4, T5e, T5a
#   (T5a/T5b borderline — perf/reliability CAN need grounding when a code mitigation exists; flag for eval).
GROUNDING_AGENT = {"T5c", "T10", "T11"}
GROUNDING_NOTE = {
    "T5c": "AGENT (experiment): a security verdict depends on the actual auth/secret implementation; single-turn speculates from plan text",
    "T10": "AGENT (experiment): an IaC verdict depends on the actual .tf — the agent found a real committed secret the plan/single-turn missed",
    "T11": "AGENT (by analogy to T10; UNTESTED — no real migration in the corpus): migration safety depends on the actual schema/migration; re-test in the eval suite",
}

# T5c security prompt refinement (review-process fix, not an epic fix): the single-turn security overlay
# HALLUCINATED domain-inappropriate requirements on rebar (a git-backed lib/CLI with no "access level"
# concept) and false-flagged "leakage" of data already in the repo. Refit: hard relevance-gate to the
# application's ACTUAL security surface, derive the security model from the domain (don't import generic
# web-app concepts the app lacks), and treat already-in-repo data as non-leakable.
# (Open: whether T5c/T5b/T10 need to be AGENTs to ground the security/ops MODEL in the codebase — a
# deliberate bright-line exception — is deferred to the brainstorm + the eval suite.)
SCENARIO_OVERRIDE = {
 "T5c": (
   "OVERLAY — apply ONLY if the plan actually adds a security surface in THIS application's domain: a new "
   "endpoint, network exposure, an authn/authz boundary, storage/transmission of sensitive data, PII, or a "
   "credential/secret/grant. If the application has no such surface (e.g. a local library / CLI / git-backed "
   "tool with no network or auth), PASS as not-applicable. DERIVE the security model from the application's "
   "ACTUAL domain — do NOT import generic web-app concepts (e.g. a 'declared access level', endpoint authn) "
   "that this application does not have; a finding that imposes a security requirement the application's "
   "domain does not contain is a FALSE POSITIVE, not a gap. Where a real surface exists, check (OWASP only "
   "where the category applies): (a) sensitive paths use the app's own auth mechanism; (b) data protection — "
   "encryption at rest/in transit where data is actually stored/transmitted; (c) LEAST-PRIVILEGE on any new "
   "credential/role/grant (no wildcard / admin-for-convenience); (d) SECRET LIFECYCLE — no plaintext secrets "
   "in code/IaC/logs; use a secrets manager. ANTI-FP: do NOT flag 'leakage' of data that is ALREADY in the "
   "ticket/repo — review findings that also live in the repo leak nothing; secrets sitting in tickets/the "
   "repo are an UPSTREAM concern, not this review's. SEVERITY priors: an undeclared sensitive surface or a "
   "plaintext secret is high. PASS if the application's actual security boundaries are explicit and sound."),
}

# Pass-3 per-criterion block thresholds. Start HIGH -> advisory (9da1: gather calibration data first).
# A criterion blocks only if Pass-3 confidence >= block_threshold AND computed severity is high enough.
# DEFAULT advisory for every LLM criterion in v1; the DET floor is the only hard blocker.
DEFAULT_BLOCK_THRESHOLD = 0.95


def main():
    out = []
    for c in V7:
        n = dict(c)
        n["severity_by"] = "pass3"          # severity computed deterministically downstream, not by Pass-1
        n["default_posture"] = "advisory"
        n["block_threshold"] = DEFAULT_BLOCK_THRESHOLD
        if c["id"] in LLM_ROUTED:
            n["overlay_routing"] = "llm"    # the relevance router decides applicability, not a keyword
            if c["id"] in LLM_TRIGGER_NOTE:
                n["trigger"] = LLM_TRIGGER_NOTE[c["id"]]
        elif c.get("routing") == "overlay":
            n["overlay_routing"] = "deterministic"
        if c["id"] in RECLASSIFY_EXEC:
            n["exec"] = RECLASSIFY_EXEC[c["id"]]
            n["_tier_note"] = RECLASSIFY_NOTE[c["id"]]
            n.pop("_v7_note", None)
        if c["id"] in SCENARIO_OVERRIDE:
            n["scenario"] = SCENARIO_OVERRIDE[c["id"]]
            n["_prompt_note"] = "scenario refined this session (review-process FP fix: domain-appropriate security, no already-in-repo leakage)"
        if c["id"] in GROUNDING_AGENT:   # grounding experiment: verdict depends on the actual code -> AGENT
            n["exec"] = "AGENT"
            n["_tier_note"] = GROUNDING_NOTE[c["id"]]
            n.pop("_v7_note", None)
        out.append(n)

    # APPROVED new criterion (this session): intent-source fidelity. NOT in v6/v7, appended here.
    # Catches silent descoping of an EXTERNALLY-expressed requirement (the gate's plan-internal blind spot).
    # Single-call/2-STEP, FED the linked session log + ticket graph (NOT agent — text-vs-text); frontier model;
    # fires ONLY when the ticket is linked to a session log.
    out.append({
        "id": "ISF", "exec": "2-STEP", "facet": "intent-provenance",
        "name": "Intent-source fidelity (plan vs linked design intent)",
        "scenario": (
            "Compare the plan against the EXTERNAL intent expressed in the ticket's LINKED SESSION LOG (the "
            "design/brainstorm of record), to catch requirements the plan SILENTLY DROPPED, descoped, or "
            "contradicted relative to what the user expressed — a defect no plan-internal check can catch (E3 "
            "compares plan-vs-its-own-title; this compares plan-vs-the-original-intent). 2-STEP: (1) extract "
            "the discrete expressed requirements/decisions/constraints from the linked session log; (2) check "
            "the plan + its ticket graph against each, flagging any dropped, narrowed/out-scoped-without-"
            "rationale, or contradicted. Runs on a FRONTIER model (large session-log context) and is FED the "
            "session log + the pre-resolved ticket graph as context — NOT agent/tool-using (deterministic "
            "if the linked log exceeds the escalated context window, evaluate against a SUMMARY of the log "
            "and RECORD that a summary was used — the finding then carries REDUCED CONFIDENCE). ANTI-FP: a "
            "requirement DELIBERATELY descoped WITH a "
            "stated rationale is not a finding; fire only on SILENT or unjustified divergence."),
        "trigger": "ticket is LINKED TO A SESSION LOG (else PASS not-applicable / skip — never fabricate an intent baseline)",
        "routing": "base",
        "applies_at": {"levels": ["epic", "story", "task"], "container_only": False,
                       "suppress_when": [], "suppress_types": ["bug"]},
        "checklist": [
            {"key": "requirements_extracted", "check": "Discrete expressed requirements/decisions/constraints are extracted from the linked session log."},
            {"key": "each_honored", "check": "Each expressed requirement is honored by the plan + ticket graph, or descoped WITH a stated rationale."},
            {"key": "no_silent_descope", "check": "No expressed requirement is silently dropped, narrowed, or contradicted (the visual-editing-deferred failure mode)."},
        ],
        "severity_by": "pass3", "default_posture": "advisory", "block_threshold": DEFAULT_BLOCK_THRESHOLD,
        "_tier_note": "frontier-model single-call/2-STEP, FED the linked session log + ticket graph; NOT agent (text-vs-text); fires only when a session log is linked",
    })

    path = os.path.join(HERE, "criteria_v8.json")
    json.dump(out, open(path, "w"), indent=1, ensure_ascii=False)
    llm = [c["id"] for c in out if c.get("overlay_routing") == "llm"]
    det = [c["id"] for c in out if c.get("overlay_routing") == "deterministic"]
    print(f"wrote {path}: {len(out)} descriptors (three-pass-native)")
    print(f"  severity_by=pass3 + default_posture=advisory on all {len(out)}")
    print(f"  overlay routing — LLM: {llm}")
    print(f"                  — deterministic: {det}")


if __name__ == "__main__":
    main()
