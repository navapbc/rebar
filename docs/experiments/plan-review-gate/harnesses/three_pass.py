#!/usr/bin/env python3
"""three_pass — the evidence -> binary-verify -> deterministic-gate review structure (epic 9da1 / log 9dba),
adopted for PLAN review (epic 5fd2) per the user directive: REPLACE per-criterion model-emitted severity.

PASS 1 (find):   the reviewer surfaces FINDINGS as records {finding, criteria[] (ids in the locked v8
                 rubric), evidence[] (flexible: plan quote / section / absence rationale / code citation),
                 scenarios[], impact}. NO model severity/confidence. Run per facet-chunk of the rubric.
PASS 2 (verify): a SEPARATE verifier, fresh context, re-grounds and emits per finding
                 (a) severity ATTRIBUTES {prod_impact, debt_impact, blast_radius, likelihood, reversibility}
                 (b) typed BINARY sub-answers {yes|no|insufficient}. Rules: atomic, INDEPENDENT (the
                 finding is presented as a CLAIM TO TEST, its conclusion not asserted), verdict-with-
                 citation not -with-fix, "insufficient" allowed. Single-turn by default; agentic (repo
                 tools) when the finding is codebase-grounded.
PASS 3 (decide): DETERMINISTIC. Veto = cited-reference-accuracy (ONLY when a code citation is present).
                 confidence = graded fraction of the binary sub-answers (yes=1, insufficient=.5, no=0).
                 severity = computed from attributes. decision = block|advisory|dropped vs per-criterion
                 block_threshold (start high -> advisory). No model holistic severity/confidence in the path.
"""
import json, os, re, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
import gate_lib as G
import harness as h
import exp2_agentic as e2

client = anthropic.Anthropic()
TMP = h.TMP
CODEBASE_GROUNDED = {"E4", "G1G2", "A1", "G6", "T8", "T1", "T3", "T10", "T11", "G3", "G4"}

# ------------------------------------------------------------------ PASS 1
PASS1_SYSTEM = (
    "You are an expert plan reviewer running PASS 1 of a three-pass review. You review a ticket's "
    "implementation PLAN before an agent executes it; you are NOT its author. Surface FINDINGS against "
    "the given rubric criteria. A finding is a specific, grounded concern that the plan should address.\n\n"
    "STRICT OUTPUT CONTRACT (this is pass 1 of 3):\n"
    "- Emit one record PER distinct finding: {finding, criteria[], evidence[], scenarios[], impact}.\n"
    "- criteria[] = the rubric id(s) the finding maps to (from the ids you are given).\n"
    "- evidence[] = flexible free text grounding the finding: a quoted phrase / named section / an "
    "ABSENCE rationale ('the plan never states X') / a code citation. Absence findings are valid and "
    "common in plan review.\n"
    "- scenarios[] = concrete situations where this bites. impact = the consequence if unaddressed.\n"
    "- DO NOT emit any severity, confidence, or priority — those are computed by later passes. Your job "
    "is to FIND and GROUND, not to rate.\n"
    "- Ground every finding; do not fabricate to look thorough. If the plan satisfies a criterion, emit "
    "no finding for it. It is fine to return an empty findings list for a clean chunk.\n"
    "- A benign reading that dissolves a concern means there is no finding."
)
PASS1_TOOL = [{"name": "emit_findings", "description": "Emit pass-1 evidence records (no severity/confidence).",
  "input_schema": {"type": "object", "properties": {"findings": {"type": "array", "items": {"type": "object",
    "properties": {
      "finding": {"type": "string"},
      "criteria": {"type": "array", "items": {"type": "string"}},
      "evidence": {"type": "array", "items": {"type": "string"}},
      "scenarios": {"type": "array", "items": {"type": "string"}},
      "impact": {"type": "string"}},
    "required": ["finding", "criteria", "evidence", "impact"]}}}, "required": ["findings"]}}]

def pass1_chunk(title, plan, rubric_chunk, model="claude-opus-4-8", extra=""):
    ids = [c["id"] for c in rubric_chunk]
    rub = "\n".join(G.crit_block(c) for c in rubric_chunk)
    system = [{"type": "text", "text": PASS1_SYSTEM},
              {"type": "text", "text": f"# Plan under review\nTitle: {title}\n{extra}\n## Plan\n{plan}",
               "cache_control": {"type": "ephemeral"}}]
    user = (f"## Locked rubric criteria for this pass (ids: {', '.join(ids)})\n{rub}\n\n"
            "Surface every grounded finding against these criteria. Call emit_findings. "
            "Remember: NO severity/confidence; ground each finding; empty list if the plan is clean here.")
    for attempt in range(3):
        try:
            r = client.messages.create(model=model, max_tokens=4000, system=system, tools=PASS1_TOOL,
                                       tool_choice={"type": "tool", "name": "emit_findings"},
                                       messages=[{"role": "user", "content": user}])
            fs = next((b.input.get("findings", []) for b in r.content if b.type == "tool_use"), [])
            # keep only findings whose criteria intersect this chunk (guard against id drift)
            clean = []
            for f in fs:
                if isinstance(f, dict) and f.get("finding"):
                    f["criteria"] = [c for c in (f.get("criteria") or []) if c in ids] or ids[:1]
                    f.setdefault("evidence", []); f.setdefault("scenarios", []); f.setdefault("impact", "")
                    clean.append(f)
            return clean
        except Exception as e:
            if attempt == 2:
                return [{"finding": f"(pass1 error: {e})", "criteria": ids[:1], "evidence": [], "scenarios": [], "impact": "", "_error": True}]
            time.sleep(2 * (attempt + 1))

# ------------------------------------------------------------------ PASS 2
GRADED = ["is_verifiable", "evidence_entails_finding", "path_reachable", "impact_follows_necessarily",
          "no_viable_alternative_explanation", "no_existing_mitigation", "severity_claim_justified"]
PASS2_SYSTEM = (
    "You are the independent VERIFIER (pass 2 of 3). A pass-1 reviewer has proposed a finding about a "
    "ticket plan. Your job is to TEST that claim against the plan (and code where cited) and report "
    "structured attributes + atomic binary sub-answers. You did NOT write the finding and must not assume "
    "it is correct.\n\nRULES (obey strictly):\n"
    "- ATOMICITY: answer each sub-question about ONE proposition only.\n"
    "- INDEPENDENCE: treat the finding as an UNPROVEN claim; do not let its assertion bias your answers. "
    "Judge from the plan/evidence yourself.\n"
    "- VERDICT-WITH-CITATION, not verdict-with-fix: justify each answer by what the plan/code does or does "
    "not say; do NOT propose how to fix it.\n"
    "- 'insufficient' is a valid, encouraged answer when the evidence does not let you decide.\n"
    "- cited_reference_accurate: answer 'yes/no' ONLY if the finding cites a specific code/file reference; "
    "otherwise 'na' (most plan findings are non-citable absence findings).\n"
    "- Severity ATTRIBUTES are coarse ordinals describing the consequence IF the finding is real; you are "
    "not deciding block/advisory (a deterministic pass does that)."
)
PASS2_TOOL = [{"name": "verify_finding", "description": "Verify a pass-1 finding: attributes + binary sub-answers.",
  "input_schema": {"type": "object", "properties": {
    "severity_attributes": {"type": "object", "properties": {
        "prod_impact": {"type": "string", "enum": ["none", "low", "medium", "high"]},
        "debt_impact": {"type": "string", "enum": ["none", "low", "medium", "high"]},
        "blast_radius": {"type": "string", "enum": ["local", "module", "system"]},
        "likelihood": {"type": "string", "enum": ["low", "medium", "high"]},
        "reversibility": {"type": "string", "enum": ["easy", "moderate", "hard"]}},
      "required": ["prod_impact", "debt_impact", "blast_radius", "likelihood", "reversibility"]},
    "binary": {"type": "object", "properties": {
        "cited_reference_accurate": {"type": "string", "enum": ["yes", "no", "insufficient", "na"]},
        **{q: {"type": "string", "enum": ["yes", "no", "insufficient"]} for q in GRADED}},
      "required": ["cited_reference_accurate"] + GRADED}},
    "required": ["severity_attributes", "binary"]}}]

def _verify_user(finding):
    return ("## Finding to TEST (an unproven claim from a pass-1 reviewer)\n"
            f"CLAIM: {finding['finding']}\n"
            f"Rubric criteria it cites: {', '.join(finding.get('criteria', []))}\n"
            f"Reviewer's evidence: {finding.get('evidence')}\n"
            f"Reviewer's asserted impact: {finding.get('impact')}\n\n"
            "Independently test this claim. Answer each binary sub-question atomically (yes/no/insufficient; "
            "cited_reference_accurate=na unless a specific code reference is cited), and assign the coarse "
            "severity attributes for the consequence IF the claim is real. Call verify_finding.")

def pass2_verify(title, plan, finding, repo_root=None, agentic=False):
    """Independent verification. agentic=True uses repo tools to re-ground codebase claims."""
    grounded = agentic and repo_root and any(c in CODEBASE_GROUNDED for c in finding.get("criteria", []))
    if grounded:
        return _verify_agentic(title, plan, finding, repo_root)
    system = [{"type": "text", "text": PASS2_SYSTEM},
              {"type": "text", "text": f"# Plan under review\nTitle: {title}\n## Plan\n{plan}"}]
    for attempt in range(3):
        try:
            r = client.messages.create(model="claude-sonnet-4-6", max_tokens=1500, system=system,
                                       tools=PASS2_TOOL, tool_choice={"type": "tool", "name": "verify_finding"},
                                       messages=[{"role": "user", "content": _verify_user(finding)}])
            v = next((b.input for b in r.content if b.type == "tool_use"), None)
            if v: return {"verify": v, "mode": "single-turn", "tool_calls": 0}
        except Exception as e:
            if attempt == 2: return {"verify": None, "mode": f"error:{e}", "tool_calls": 0}
            time.sleep(2)

def _verify_agentic(title, plan, finding, repo_root):
    system = [{"type": "text", "text": PASS2_SYSTEM + "\n\nYou have READ-ONLY repo tools (grep, read_file, glob). "
               "USE them to re-ground the claim in the ACTUAL code before answering — especially "
               "cited_reference_accurate, evidence_entails_finding, no_existing_mitigation. Aim for <8 calls."},
              {"type": "text", "text": f"# Plan under review\nTitle: {title}\n## Plan\n{plan}",
               "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": _verify_user(finding)}]
    tool_calls = 0
    for it in range(8):
        last = it >= 7
        kw = dict(model="claude-sonnet-4-6", max_tokens=1800, system=system, tools=e2.TOOLS[:-1] + PASS2_TOOL, messages=messages)
        if last: kw["tool_choice"] = {"type": "tool", "name": "verify_finding"}
        r = client.messages.create(**kw)
        tus = [b for b in r.content if b.type == "tool_use"]
        if not tus: break
        messages.append({"role": "assistant", "content": r.content})
        results, done, verify = [], False, None
        for b in tus:
            if b.name == "verify_finding":
                verify = b.input; done = True
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": "ok"})
            else:
                tool_calls += 1
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": e2.run_tool(b.name, b.input, repo_root)})
        messages.append({"role": "user", "content": results})
        if done: return {"verify": verify, "mode": "agentic", "tool_calls": tool_calls}
    return {"verify": None, "mode": "agentic-no-verdict", "tool_calls": tool_calls}

# ------------------------------------------------------------------ PASS 3 (deterministic)
_ORD = {"none": 0, "low": 1, "medium": 2, "high": 3, "local": 1, "module": 2, "system": 3,
        "easy": 1, "moderate": 2, "hard": 3}
def _grade(v):
    return {"yes": 1.0, "insufficient": 0.5, "no": 0.0}.get(v, 0.5)

def pass3_decide(verify, block_threshold=0.95, blocking_enabled=False):
    """Deterministic: veto -> confidence -> severity -> decision. No model judgment in this path.

    blocking_enabled reflects the criterion's posture: in v1 every LLM criterion is advisory
    (default_posture='advisory') so it can never 'block' — only the DET floor blocks. A project
    opts a criterion into blocking, which (with high confidence + severity) yields 'block'.
    """
    if not verify:
        return {"decision": "dropped", "reason": "no-verification", "confidence": 0.0, "severity": "none"}
    b = verify.get("binary", {}); a = verify.get("severity_attributes", {})
    # VETO: cited-reference-accuracy, only when a code citation was actually checked
    if b.get("cited_reference_accurate") == "no":
        return {"decision": "dropped", "reason": "veto:cited-reference-inaccurate", "confidence": 0.0, "severity": "none"}
    # confidence = graded fraction of the binary sub-answers (exclude any 'na')
    scores = [_grade(b.get(q)) for q in GRADED if b.get(q) in ("yes", "no", "insufficient")]
    confidence = round(sum(scores) / len(scores), 3) if scores else 0.0
    # severity from attributes (deterministic ordinal map)
    impact = max(_ORD.get(a.get("prod_impact"), 0), _ORD.get(a.get("debt_impact"), 0))
    blast = _ORD.get(a.get("blast_radius"), 1); like = _ORD.get(a.get("likelihood"), 1)
    rev = _ORD.get(a.get("reversibility"), 1)
    if impact == 0:
        severity = "none"
    else:
        score = impact + 0.5 * (blast - 1) + 0.5 * (like - 1) + 0.34 * (rev - 1)   # ~[1 .. 4.7]
        severity = "critical" if score >= 4.0 else "major" if score >= 2.5 else "minor"
    # decision: block only if high-confidence AND high-severity; else advisory; drop weak signals
    if confidence < 0.5:
        decision = "dropped"; reason = "low-confidence"
    elif blocking_enabled and confidence >= block_threshold and severity in ("major", "critical"):
        decision = "block"; reason = "high-confidence+severity (criterion opted into blocking)"
    else:
        decision = "advisory"; reason = "default-advisory (v1 posture)"
    return {"decision": decision, "reason": reason, "confidence": confidence, "severity": severity,
            "impact_ord": impact, "blast": blast, "likelihood": like}

# ------------------------------------------------------------------ orchestrator
def run_three_pass(title, plan, rubric, repo_root=None, model="claude-opus-4-8", extra="",
                   agentic_verify=True, ticket_size="moderate", log=print):
    chunks = G.chunk_by_facet(rubric, model, ticket_size)
    # PASS 1 — chunks in parallel
    findings = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for fs in ex.map(lambda ch: pass1_chunk(title, plan, ch, model, extra), chunks):
            findings.extend(fs)
    findings = [f for f in findings if not f.get("_error")]
    log(f"  PASS 1: {len(findings)} findings across {len(chunks)} rubric chunks")
    postures = {c["id"]: c.get("default_posture", "advisory") for c in rubric}
    thresholds = {c["id"]: c.get("block_threshold", 0.95) for c in rubric}
    # PASS 2 — verify each finding independently (agentic where codebase-grounded)
    def verify_one(f):
        res = pass2_verify(title, plan, f, repo_root, agentic=agentic_verify)
        blocking = any(postures.get(c) == "blocking" for c in f.get("criteria", []))
        bt = min([thresholds.get(c, 0.95) for c in f.get("criteria", [])] or [0.95])
        d = pass3_decide(res.get("verify"), block_threshold=bt, blocking_enabled=blocking)   # PASS 3 (deterministic)
        return {**f, "_verify": res.get("verify"), "_mode": res.get("mode"),
                "_tool_calls": res.get("tool_calls", 0), **d}
    out = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        out = list(ex.map(verify_one, findings))
    return out


if __name__ == "__main__":
    crits = G.load_criteria("/Users/joeoakhart/rebar/docs/experiments/plan-review-gate/criteria/criteria_v8.json")
    print(f"three_pass loaded {len(crits)} v8 criteria; codebase-grounded={sorted(CODEBASE_GROUNDED)}")
    # smoke test on a tiny synthetic plan
    demo = run_three_pass("Demo", "Add idempotent claim: check if not exists then write. AC: second claim is a no-op.",
                          [c for c in crits if c["id"] in ("G6", "T9", "E2")], agentic_verify=False, log=print)
    for f in demo:
        print(f"  [{f['decision']:8} conf={f['confidence']:.2f} sev={f['severity']:8}] {f['criteria']} :: {f['finding'][:90]}")
