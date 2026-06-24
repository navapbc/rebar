#!/usr/bin/env python3
"""Run the converged 3-pass plan-review on EACH child of the foundation epic, with proportionate
scrutiny (level='story', has_children=False -> container criteria G3/G4 and task-only criteria drop).
Same harness as the epic run. rebar resolves via the PATH shim (-> .venv/bin/rebar)."""
import json, os, subprocess, traceback
import three_pass as TP
import gate_lib as G
import harness as h
from collections import Counter

REBAR = "/Users/joeoakhart/rebar"
EPIC = "da27-c916-f04e-4885"
V8 = "/Users/joeoakhart/rebar/docs/experiments/plan-review-gate/criteria/criteria_v8.json"
OUT = "/Users/joeoakhart/.claude/jobs/98d05d90/tmp/children_review.json"

ep = json.loads(subprocess.run(["rebar", "show", EPIC], capture_output=True, text=True, cwd=REBAR).stdout)
PARENT_CTX = (f"\n## Parent epic context (this ticket is ONE child of epic {EPIC}: '{ep['title']}').\n"
              "Vision: editor lets a human compose workflows by selecting contract-bearing prompts + "
              "scripted ops from a typed palette, and author prompts in place. Review THIS child's plan.")

_all = json.loads(subprocess.run(["rebar", "list", "--status=open,in_progress"], capture_output=True, text=True, cwd=REBAR).stdout or "[]")
CHILDREN = [t for t in _all if t.get("parent_id") == EPIC]

ALL = [c for c in G.load_criteria(V8) if c["id"] != "ISF"]
summary = {}
out = {}
print(f"PER-CHILD review on {len(CHILDREN)} children of {EPIC}\n", flush=True)
for t in CHILDREN:
    tid, ttype = t["ticket_id"], t.get("ticket_type", "story")
    cd = json.loads(subprocess.run(["rebar", "show", tid], capture_output=True, text=True, cwd=REBAR).stdout or "{}")
    title, plan = cd.get("title", ""), cd.get("description") or ""
    crits = [c for c in ALL if G.applies(c, "story", has_children=False, ttype=ttype, plan=plan)]
    G.ensure_agent_crit(crits)
    print(f"=== {tid} [{ttype}] {title[:55]}  ({len(crits)} criteria after proportionate filter) ===", flush=True)
    ac = subprocess.run(["rebar", "check-ac", tid], capture_output=True, text=True, cwd=REBAR).stdout.strip()
    cl = subprocess.run(["rebar", "clarity-check", tid], capture_output=True, text=True, cwd=REBAR).stdout.strip()
    try:
        res = TP.run_three_pass(title, plan, crits, repo_root=REBAR, model="claude-opus-4-8",
                                extra=PARENT_CTX, agentic_verify=True, ticket_size="moderate")
    except Exception as e:
        print(f"  ! review error: {e}\n{traceback.format_exc()[:400]}", flush=True)
        res = []
    dec = Counter(r["decision"] for r in res)
    summary[tid] = {"title": title, "decisions": dict(dec),
                    "block": [r for r in res if r["decision"] == "block"],
                    "advisory": [r for r in res if r["decision"] == "advisory"]}
    out[tid] = {"title": title, "det": {"ac": ac, "clarity": cl}, "findings": res}
    print(f"    DET: ac={ac[:40]} | clarity={cl[:40]}", flush=True)
    print(f"    decisions: {dict(dec)}", flush=True)
    for r in sorted(res, key=lambda x: {'block':0,'advisory':1,'dropped':2}.get(x['decision'],3)):
        if r["decision"] in ("block", "advisory"):
            print(f"      {r['decision'].upper():8} {str(r['criteria']):16} sev={r['severity']:8} conf={r['confidence']:.2f}", flush=True)
            print(f"        {r['finding'][:160]}", flush=True)
    print("", flush=True)
    json.dump(out, open(OUT, "w"), indent=1)  # checkpoint after each child

print("=" * 80, flush=True)
print("PER-CHILD SUMMARY", flush=True)
for tid, s in summary.items():
    print(f"  {tid}  {dict(s['decisions'])}  blocks={len(s['block'])} advisories={len(s['advisory'])}  {s['title'][:45]}", flush=True)
print(f"\nwrote {OUT}\nPER-CHILD REVIEW DONE", flush=True)
