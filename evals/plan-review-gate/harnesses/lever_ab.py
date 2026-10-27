#!/usr/bin/env python3
"""Decisiveness-lever A/B — does the v7 SYSTEM lever fix AMBIGUOUS-on-clean without the AGENT tier?

The round-5 scorecard's one precision wart: codebase-grounded / overlay criteria hedge AMBIGUOUS
on a CLEAN (well-specified) good plan when run single-turn without tools. Two candidate fixes:
  (a) DECISIVENESS LEVER (gate_lib.SYSTEM): 'a well-specified plan you can't execute is a PASS,
      not an AMBIGUOUS' — cheap, single-turn.
  (b) route the criterion to the AGENT tier (85x cost).
This A/B isolates (a): run each seeded BAD (want FAIL = recall) and GOOD (want PASS = precision)
case under the v6 SYSTEM (no lever) vs the v7 SYSTEM (lever), single-turn, with the v7 descriptor
(checklist-aware). If the lever flips GOOD AMBIGUOUS->PASS while keeping BAD->FAIL, we DON'T need
to pay the AGENT tier for the self-contained-plan case — informing the T10/T11 routing decision
and confirming COH/T9 can stay single-turn.

Writes lever_ab.jsonl to TMP and prints the recall/clean table.
"""
import json, os, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
import harness as h
import gate_lib as G
from seedpilot import CASES   # {cid: (bad_plan, good_plan)} for G6/T10/T11/T12/E5/COH/T9/A1

TMP = h.TMP
OUT = os.path.join(TMP, "lever_ab.jsonl")
MODEL = "claude-sonnet-4-6"
REPEATS = 3
client = anthropic.Anthropic()
V7 = {c["id"]: c for c in G.load_criteria()}
SYS = {"v6_nolever": h.SYSTEM, "v7_lever": G.SYSTEM}

def run(cid, label, plan, sysname, rep):
    crit = V7[cid]
    system = [{"type": "text", "text": SYS[sysname]},
              {"type": "text", "text": f"# Ticket plan under review\nTitle: (seeded {label} case)\n\n## Plan\n{plan}"}]
    user = ("## Review criterion to apply (one verdict entry for id %s)\n\n%s\n\n"
            "Call submit_review with exactly one entry for this criterion id." % (cid, G.crit_block(crit)))
    r = client.messages.create(model=MODEL, max_tokens=1500, system=system, tools=h.TOOL,
                               tool_choice={"type": "tool", "name": "submit_review"},
                               messages=[{"role": "user", "content": user}])
    findings, status = G.robust_findings(r, expected_ids=[cid])
    f = findings[0] if findings else {}
    return {"crit": cid, "label": label, "system": sysname, "repeat": rep,
            "verdict": f.get("verdict"), "severity": f.get("severity"),
            "finding": (f.get("finding") or "")[:140], "status": status}

lock = threading.Lock()
def job(args):
    for a in range(3):
        try:
            rec = run(*args)
            with lock: open(OUT, "a").write(json.dumps(rec) + "\n")
            return
        except Exception as e:
            if a == 2:
                with lock: open(OUT, "a").write(json.dumps({"ERR": str(e), "crit": args[0]}) + "\n")
            time.sleep(2)

if __name__ == "__main__":
    open(OUT, "w").close()
    jobs = []
    for cid, (bad, good) in CASES.items():
        for sysname in SYS:
            for rep in range(REPEATS):
                jobs.append((cid, "BAD", bad, sysname, rep))
                jobs.append((cid, "GOOD", good, sysname, rep))
    print(f"lever A/B: {len(CASES)} criteria x BAD/GOOD x {len(SYS)} systems x {REPEATS} reps = {len(jobs)} runs")
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(as_completed([ex.submit(job, j) for j in jobs]))

    # ---- summarize ----
    rows = [json.loads(l) for l in open(OUT) if l.strip() and '"ERR"' not in l]
    print("\nDecisiveness-lever A/B (verdict distribution; want BAD->FAIL recall, GOOD->PASS clean)\n")
    print(f"{'crit':5} {'system':12} {'BAD verdicts':22} {'GOOD verdicts':22}")
    for cid in CASES:
        for sysname in SYS:
            def dist(lbl):
                vs = [r["verdict"] for r in rows if r["crit"] == cid and r["system"] == sysname and r["label"] == lbl]
                from collections import Counter
                return ",".join(f"{k}:{v}" for k, v in sorted(Counter(vs).items()))
            print(f"{cid:5} {sysname:12} {dist('BAD'):22} {dist('GOOD'):22}")
    # aggregate clean-AMBIGUOUS reduction
    def agg(sysname, lbl, want):
        vs = [r["verdict"] for r in rows if r["system"] == sysname and r["label"] == lbl]
        return sum(1 for v in vs if v == want), len(vs)
    for sysname in SYS:
        rb, nb = agg(sysname, "BAD", "FAIL")
        gp, ng = agg(sysname, "GOOD", "PASS")
        ga = sum(1 for r in rows if r["system"] == sysname and r["label"] == "GOOD" and r["verdict"] == "AMBIGUOUS")
        print(f"\n{sysname}: recall(BAD->FAIL)={rb}/{nb}  clean(GOOD->PASS)={gp}/{ng}  GOOD->AMBIGUOUS={ga}")
    print("\nLEVER A/B DONE")
