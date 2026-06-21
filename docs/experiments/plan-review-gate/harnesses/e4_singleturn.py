#!/usr/bin/env python3
"""E4 generalization (single-turn tier) on the NON-DSO corpus + E5 v6/v7 A/B + overlay-trigger precision.

Three measurements from one corpus (corpus_sample.json: rebar + snap):
  (1) SUITE: run every applies_at-passing single-turn criterion (v7) on each ticket, chunked
      by facet, 2 repeats. -> per-criterion fire-rate by level/repo; does the routing behave
      on a held-out population; do criteria over-fire on a different style; not-applicable
      (PASS) rate for overlays (false-fire check).
  (2) E5 A/B: run E5 alone with the v6 scenario vs the v7 retuned scenario on each ticket where
      E5 applies. -> before/after fire-rate (the item-A retune validation).
  (3) TRIGGER: deterministic det_overlays vs an LLM router per ticket -> overlay-trigger
      precision on the new overlays (T10/T11/T12), esp. the rebar 'migration'(=bash->py) trap.

Writes e4_suite.jsonl, e4_e5ab.jsonl, e4_trigger.jsonl to TMP and prints a summary.
"""
import json, os, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import gate_lib as G
import round4 as r4   # for llm_router
import harness as h

TMP = h.TMP
MODEL = "claude-sonnet-4-6"
CORPUS = json.load(open(os.path.join(TMP, "corpus_sample.json")))
V7 = {c["id"]: c for c in G.load_criteria()}
V6 = {c["id"]: c for c in json.load(open("/Users/joeoakhart/rebar/docs/experiments/plan-review-gate/criteria/criteria_v6.json"))}
SINGLE_TURN_V7 = [c for c in V7.values() if c.get("exec") in ("1-TURN", "2-STEP")]

lock = threading.Lock()
def W(path, rec):
    with lock:
        open(os.path.join(TMP, path), "a").write(json.dumps(rec) + "\n")

def size_of(t):
    return "epic" if t["level"] == "epic" else ("large" if t["has_children"] else "moderate")

# ---------------- (1) SUITE ----------------
def suite_job(t, rep):
    crits = [c for c in SINGLE_TURN_V7
             if G.applies(c, t["level"], t["has_children"], t["type"], t["plan"])]
    chunks = G.chunk_by_facet(crits, MODEL, size_of(t))
    allf, statuses = [], []
    extra = ""
    for ch in chunks:
        r = G.single_turn(t["title"], t["plan"], ch, model=MODEL, extra=extra)
        allf += r["findings"]; statuses.append(r["status"])
        time.sleep(0.05)
    W("e4_suite.jsonl", {"id": t["id"], "repo": t["repo"], "type": t["type"], "level": t["level"],
                          "has_children": t["has_children"], "repeat": rep,
                          "n_criteria": len(crits), "findings": allf, "statuses": statuses})

# ---------------- (2) E5 A/B ----------------
def e5_job(t, version, rep):
    crit = dict(V7["E5"]) if version == "v7" else dict(V6["E5"])
    r = G.single_turn(t["title"], t["plan"], [crit], model=MODEL)
    f = r["findings"][0] if r["findings"] else {"verdict": "ERR", "severity": "none", "finding": ""}
    W("e4_e5ab.jsonl", {"id": t["id"], "repo": t["repo"], "type": t["type"], "version": version,
                         "repeat": rep, "verdict": f.get("verdict"), "severity": f.get("severity"),
                         "finding": (f.get("finding") or "")[:160], "status": r["status"]})

# ---------------- (3) TRIGGER ----------------
def trigger_job(t):
    det = G.det_overlays(t["plan"])
    llm = r4.llm_router(t["title"], t["plan"])
    W("e4_trigger.jsonl", {"id": t["id"], "repo": t["repo"], "type": t["type"],
                            "det": det, "llm": llm})

if __name__ == "__main__":
    for f in ("e4_suite.jsonl", "e4_e5ab.jsonl", "e4_trigger.jsonl"):
        open(os.path.join(TMP, f), "w").close()
    jobs = []
    for t in CORPUS:
        for rep in range(2):
            jobs.append(("suite", t, rep))
        # E5 applies only where the v7 filter keeps it (story+task, non-test, non-bug)
        if G.applies(V7["E5"], t["level"], t["has_children"], t["type"], t["plan"]):
            for ver in ("v6", "v7"):
                for rep in range(2):
                    jobs.append(("e5", t, ver, rep))
        jobs.append(("trigger", t, None))
    print(f"E4 single-turn: {len(CORPUS)} tickets -> {len(jobs)} job units")
    def run(j):
        try:
            if j[0] == "suite": suite_job(j[1], j[2])
            elif j[0] == "e5": e5_job(j[1], j[2], j[3])
            elif j[0] == "trigger": trigger_job(j[1])
        except Exception as e:
            W("e4_suite.jsonl", {"ERROR": str(e), "job": j[0]})
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(run, j) for j in jobs]
        for fu in as_completed(futs):
            done += 1
            if done % 20 == 0: print(f"  {done}/{len(jobs)}", flush=True)
    print("E4 SINGLE-TURN DONE")
