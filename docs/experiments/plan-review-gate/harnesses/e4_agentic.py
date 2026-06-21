#!/usr/bin/env python3
"""E4 generalization (AGENT tier) + the real-ticket tier A/B.

The agentic half of E4: run the codebase-grounded AGENT criteria on real NON-DSO leaf tasks
against their REAL repos (rebar=Python, snap=Rails/Ruby), to confirm (a) the agent tools
ground on a held-out polyglot codebase and (b) the AGENT tier resolves the AMBIGUOUS the
single-turn tier hedges. For each (ticket, criterion) we run BOTH modes:
    single_turn (no tools, v7 SYSTEM)   vs   agent (grep/read/glob against the real repo)
and compare verdicts -> the tier A/B on real data (stronger than the synthetic seedpilot).

Targets: 2 rebar + 2 snap leaf tasks (incl. snap's OAuth infra task for T10).
Criteria: G6 (approach soundness), E4 (assumption verification), A1 (anti-slop), G1G2 (edit-set),
+ T10 (IaC) on the infra ticket.

Writes e4_agentic.jsonl to TMP and prints the resolution table.
"""
import json, os, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import gate_lib as G
import harness as h

TMP = h.TMP
OUT = os.path.join(TMP, "e4_agentic.jsonl")
_RAW = json.load(open(os.path.join(TMP, "corpus_sample.json")))
CORPUS = {t["id"]: t for t in _RAW}
def _resolve(prefix):
    for t in _RAW:
        if t["id"].startswith(prefix):
            return t["id"]
    raise KeyError(prefix)
V7 = {c["id"]: c for c in G.load_criteria()}
G.ensure_agent_crit(list(V7.values()))   # populate exp2.AGENT_CRIT with v7 AGENT descriptors

# (ticket_id_prefix, [criteria]) — leaf tasks with concrete code targets
TARGETS = [
    (_resolve("e249-1034"), ["G6", "E4", "A1", "G1G2"]),       # rebar: NDJSON import core (Python)
    (_resolve("05ac-9905"), ["G6", "E4", "A1", "G1G2"]),       # rebar: delete bash dispatcher (named artifacts)
    (_resolve("e46e-f886"), ["G6", "E4", "A1", "G1G2"]),       # snap: matcher robustness (Ruby)
    (_resolve("0d0c-ebd3"), ["G6", "E4", "A1", "G1G2", "T10"]),# snap: central auth host (OAuth/infra)
]

lock = threading.Lock()
def W(rec):
    with lock: open(OUT, "a").write(json.dumps(rec) + "\n")

def run_pair(tid, cid):
    t = CORPUS[tid]; crit = V7[cid]
    # single-turn (no tools)
    st = G.single_turn(t["title"], t["plan"], [crit], model="claude-sonnet-4-6")
    stf = st["findings"][0] if st["findings"] else {}
    # agentic (tools, real repo)
    ag = G.agent(t["title"], t["plan"], cid, t["repo_root"])
    agf = ag["findings"][0] if ag["findings"] else {}
    W({"ticket": tid, "repo": t["repo"], "criterion": cid,
       "single_turn": {"verdict": stf.get("verdict"), "severity": stf.get("severity"),
                       "finding": (stf.get("finding") or "")[:160], "status": st["status"]},
       "agentic": {"verdict": agf.get("verdict"), "severity": agf.get("severity"),
                   "finding": (agf.get("finding") or "")[:160], "tool_calls": ag.get("tool_calls"),
                   "iters": ag.get("iters"), "lat": round(ag.get("latency_s", 0), 1), "status": ag.get("status")}})
    print(f"  {tid[:9]} {cid:5}: ST={stf.get('verdict'):9} -> AGENT={agf.get('verdict'):9} ({ag.get('tool_calls')} tools)", flush=True)

if __name__ == "__main__":
    open(OUT, "w").close()
    jobs = [(tid, cid) for tid, cids in TARGETS for cid in cids]
    print(f"E4 agentic + tier A/B: {len(jobs)} (ticket,criterion) pairs x 2 modes (single-turn + agentic)")
    with ThreadPoolExecutor(max_workers=5) as ex:
        list(as_completed([ex.submit(run_pair, *j) for j in jobs]))

    rows = [json.loads(l) for l in open(OUT) if l.strip()]
    print("\nTier A/B on real non-DSO tickets (does AGENT resolve single-turn AMBIGUOUS?)\n")
    resolved = same = 0
    for r in rows:
        stv = r["single_turn"]["verdict"]; agv = r["agentic"]["verdict"]
        if stv == "AMBIGUOUS" and agv in ("PASS", "FAIL"):
            resolved += 1
        if stv == agv:
            same += 1
    n = len(rows)
    amb_st = sum(1 for r in rows if r["single_turn"]["verdict"] == "AMBIGUOUS")
    amb_ag = sum(1 for r in rows if r["agentic"]["verdict"] == "AMBIGUOUS")
    print(f"pairs={n}  single-turn AMBIGUOUS={amb_st}  agentic AMBIGUOUS={amb_ag}  "
          f"AMBIGUOUS resolved by AGENT={resolved}  same-verdict={same}")
    print("\nE4 AGENTIC DONE")
