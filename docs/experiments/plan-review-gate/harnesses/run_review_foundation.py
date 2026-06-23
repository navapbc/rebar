#!/usr/bin/env python3
"""Adapted final_gate_3pass: run the converged 3-pass plan-review (v8 rubric, all overlays) against
the FOUNDATION epic da27 + its live children. Manual application of epic 5fd2's process to our plan.
Same structure as final_gate_3pass.py; only EPIC + OUT + labels changed. rebar resolves via the PATH shim
(-> .venv/bin/rebar) so the v2 store + live children/deps are readable by the top-level + Pass-2 agents."""
import json, os, subprocess
import three_pass as TP
import gate_lib as G
import harness as h
from collections import Counter

REBAR = "/Users/joeoakhart/rebar"
EPIC = "da27-c916-f04e-4885"
OUT = "/Users/joeoakhart/.claude/jobs/98d05d90/tmp/foundation_review_3pass.json"
V8 = "/Users/joeoakhart/rebar/docs/experiments/plan-review-gate/criteria/criteria_v8.json"

d = json.loads(subprocess.run(["rebar", "show", EPIC], capture_output=True, text=True, cwd=REBAR).stdout)
TITLE, PLAN = d["title"], d["description"]
_all = json.loads(subprocess.run(["rebar", "list", "--status=open,in_progress"], capture_output=True, text=True, cwd=REBAR).stdout or "[]")
CHILDREN = [t["ticket_id"] for t in _all if t.get("parent_id") == EPIC]
child_ctx = ""
for tid in CHILDREN:
    cd = json.loads(subprocess.run(["rebar", "show", tid], capture_output=True, text=True, cwd=REBAR).stdout or "{}")
    if cd:
        child_ctx += f"\n\n### CHILD {tid}: {cd.get('title','')}\n{cd.get('description') or ''}"
EXTRA = "\n## Children (full excerpts, for container coverage/consistency G3/G4):" + child_ctx

if __name__ == "__main__":
    crits = [c for c in G.load_criteria(V8) if c["id"] != "ISF"]
    G.ensure_agent_crit(crits)
    print(f"FOUNDATION review (three-pass) on epic {EPIC}  ({len(PLAN)} char plan, {len(CHILDREN)} children)", flush=True)
    print(f"  rubric = {len(crits)} v8 criteria (all overlays forced on); Pass-1 Opus; Pass-2 aggregate/agentic Sonnet\n", flush=True)
    ac = subprocess.run(["rebar", "check-ac", EPIC], capture_output=True, text=True, cwd=REBAR).stdout.strip()
    cl = subprocess.run(["rebar", "clarity-check", EPIC], capture_output=True, text=True, cwd=REBAR).stdout.strip()
    print(f"  DET floor: check-ac -> {ac[:70]}", flush=True)
    print(f"             clarity  -> {cl[:70]}\n", flush=True)

    results = TP.run_three_pass(TITLE, PLAN, crits, repo_root=REBAR, model="claude-opus-4-8",
                                extra=EXTRA, agentic_verify=True, ticket_size="epic")
    json.dump({"epic": EPIC, "children": CHILDREN, "det": {"check_ac": ac, "clarity": cl}, "findings": results}, open(OUT, "w"), indent=1)

    dec = Counter(r["decision"] for r in results)
    print(f"\n{'='*86}\nFOUNDATION REVIEW RESULT (three-pass) — epic {EPIC}\n{'='*86}", flush=True)
    print(f"PASS-1 findings: {len(results)}  ->  PASS-3 decisions: {dict(dec)}\n", flush=True)
    order = {"block": 0, "advisory": 1, "dropped": 2}
    for r in sorted(results, key=lambda x: (order.get(x["decision"], 3), -x["confidence"])):
        mode = r.get("_mode", ""); tc = r.get("_tool_calls", 0)
        tag = f"[{mode}{'/'+str(tc)+'t' if tc else ''}]"
        print(f"  {r['decision'].upper():8} conf={r['confidence']:.2f} sev={r['severity']:8} {str(r['criteria']):20} {tag}", flush=True)
        print(f"     {r['finding'][:200]}", flush=True)
        if r["decision"] != "dropped" and r.get("evidence"):
            print(f"     evidence: {str(r['evidence'])[:180]}", flush=True)
    print(f"\nwrote {OUT}\nFOUNDATION REVIEW DONE", flush=True)
