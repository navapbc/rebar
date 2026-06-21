#!/usr/bin/env python3
"""FINAL GATE (three-pass) — the converged plan (epic 5fd2) through the adopted three-pass review,
ALL overlays enabled. The capstone: demonstrates the finalized v8 rubric + the evidence -> verify ->
deterministic-gate structure end-to-end, and surfaces anything still wrong with the plan.

  DET    floor: check-ac + clarity-check (the deterministic blocking floor).
  PASS 1: Opus, the FULL v8 rubric (all 31 criteria = every overlay forced on), facet-chunked; emits
          evidence records (no severity/confidence).
  PASS 2: independent verifier; AGENTIC against the real rebar repo for codebase-grounded findings,
          single-turn otherwise.
  PASS 3: deterministic veto -> confidence -> severity -> advisory (v1 posture; only the DET floor blocks).
"""
import json, os, subprocess
import three_pass as TP
import gate_lib as G
import harness as h
from collections import Counter

TMP = h.TMP
REBAR = "/Users/joeoakhart/rebar"
EPIC = "5fd2-a7c2-0aec-48fa"
OUT = os.path.join(TMP, "final_gate_3pass.json")
V8 = "/Users/joeoakhart/rebar/docs/experiments/plan-review-gate/criteria/criteria_v8.json"

d = json.loads(subprocess.run(["rebar", "show", EPIC], capture_output=True, text=True, cwd=REBAR).stdout)
TITLE, PLAN = d["title"], d["description"]
CHILDREN = ["2f3c-682a-2105-4b8f", "8e3e-50ba-765c-4d2f", "2632-5741-090e-46c3", "6d7b-41ef-f869-40dd",
            "bfa8-aadd-6739-4904", "cb28-f531-66f2-49cb", "f20a-865f-6cb3-49e4", "fd92-4b4d-b24b-41da", "a473-8af4-a493-4e0e"]
child_ctx = ""
for tid in CHILDREN:
    cd = json.loads(subprocess.run(["rebar", "show", tid], capture_output=True, text=True, cwd=REBAR).stdout or "{}")
    if cd: child_ctx += f"\n- CHILD {tid}: {cd.get('title','')}"
EXTRA = "\n## Children (for container coverage G3/G4):" + child_ctx

if __name__ == "__main__":
    crits = G.load_criteria(V8)
    G.ensure_agent_crit(crits)
    print(f"FINAL GATE (three-pass) on epic {EPIC}  ({len(PLAN)} char plan, {len(CHILDREN)} children)")
    print(f"  rubric = ALL {len(crits)} v8 criteria (every overlay forced on); Pass-1 Opus; Pass-2 agentic vs rebar\n")

    ac = subprocess.run(["rebar", "check-ac", EPIC], capture_output=True, text=True, cwd=REBAR).stdout.strip()
    cl = subprocess.run(["rebar", "clarity-check", EPIC], capture_output=True, text=True, cwd=REBAR).stdout.strip()
    print(f"  DET floor: check-ac -> {ac[:70]}")
    print(f"             clarity  -> {cl[:70]}\n")

    results = TP.run_three_pass(TITLE, PLAN, crits, repo_root=REBAR, model="claude-opus-4-8",
                                extra=EXTRA, agentic_verify=True, ticket_size="epic")
    json.dump({"epic": EPIC, "det": {"check_ac": ac, "clarity": cl}, "findings": results}, open(OUT, "w"), indent=1)

    dec = Counter(r["decision"] for r in results)
    print(f"\n{'='*86}\nFINAL GATE RESULT (three-pass) — epic 5fd2\n{'='*86}")
    print(f"PASS-1 findings: {len(results)}  ->  PASS-3 decisions: {dict(dec)}\n")
    order = {"block": 0, "advisory": 1, "dropped": 2}
    for r in sorted(results, key=lambda x: (order.get(x["decision"], 3), -x["confidence"])):
        mode = r.get("_mode", ""); tc = r.get("_tool_calls", 0)
        tag = f"[{mode}{'/'+str(tc)+'t' if tc else ''}]"
        print(f"  {r['decision'].upper():8} conf={r['confidence']:.2f} sev={r['severity']:8} {str(r['criteria']):20} {tag}")
        print(f"     {r['finding'][:150]}")
        if r["decision"] != "dropped" and r.get("evidence"):
            print(f"     evidence: {str(r['evidence'])[:150]}")
    print(f"\nwrote {OUT}\nFINAL GATE DONE")
