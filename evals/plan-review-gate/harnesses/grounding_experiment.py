#!/usr/bin/env python3
"""Settle the deferred 'does this overlay need agentic grounding?' question for each candidate, by
replicating the T5c experiment across criteria: run the criterion SINGLE-TURN (no tools) vs AGENTIC
(grep/read the real repo) on a ticket where it applies + the implementation matters. The signal: does
grounding change the verdict — correct a speculative-from-text FP, or surface a real grounded issue the
single-turn missed? If yes -> AGENT; if single-turn matches -> single-turn suffices.

Candidates (the implication overlays whose verdict may depend on HOW something is implemented):
  T5a perf, T5b reliability, T5e maintainability, T9 shared-state, T10 infra, T11 migration, T4 compat.
"""
import json, os, subprocess
import gate_lib as G
import harness as h
import exp2_agentic as e2

TMP = h.TMP
OUT = os.path.join(TMP, "grounding_experiment.jsonl")
REBAR = "/Users/joeoakhart/rebar"
SNAP = os.path.expanduser("~/snap-oakhart-manual/snap-oakhart-manual")
V8 = {c["id"]: c for c in G.load_criteria("/Users/joeoakhart/rebar/docs/experiments/plan-review-gate/criteria/criteria_v8.json")}

# (criterion, ticket_prefix, repo, why-this-ticket)
PAIRS = [
    ("T5a", "fd00", SNAP, "perf/concurrency: post-commit parallel isolated envs"),
    ("T5b", "0d0c", SNAP, "reliability: OAuth auth host (external integration, writes, failure points)"),
    ("T5e", "4387", REBAR, "maintainability: delete bash dispatcher -> argparse cli (cross-component refactor)"),
    ("T9",  "fd00", SNAP, "shared-state/concurrency: cross-state isolated environments"),
    ("T10", "0d0c", SNAP, "infra/IaC: real Terraform auth host"),
    ("T11", "e249", REBAR, "data-shape (closest; no real DB migration in corpus): NDJSON import of persisted events"),
    ("T4",  "05ac", REBAR, "compat/destructive: delete the bash dispatcher + engine (removes behavior + consumers)"),
]

def resolve(prefix, repo):
    d = json.loads(subprocess.run(["rebar", "list", "--status=open,in_progress,closed"], capture_output=True, text=True, cwd=repo).stdout or "[]")
    for t in d:
        if t["ticket_id"].startswith(prefix):
            return t["ticket_id"], t["title"], t["description"]
    return None, None, None

# make each candidate runnable in the agent harness
for cid in {p[0] for p in PAIRS}:
    c = V8[cid]
    e2.AGENT_CRIT[cid] = f"{cid} — {c['name']}. {c['scenario']}"

open(OUT, "w").close()
print("grounding experiment: single-turn vs agentic, per candidate criterion\n")
for cid, prefix, repo, why in PAIRS:
    tid, title, plan = resolve(prefix, repo)
    if not tid:
        print(f"### {cid}: ticket {prefix} NOT FOUND — skipped"); continue
    c = V8[cid]
    st = G.single_turn(title, plan, [c], model="claude-sonnet-4-6")
    sf = st["findings"][0] if st["findings"] else {}
    ag = G.agent(title, plan, cid, repo)
    af = ag["findings"][0] if ag["findings"] else {}
    rec = {"crit": cid, "ticket": tid, "repo": "snap" if repo == SNAP else "rebar", "why": why,
           "single_turn": {"verdict": sf.get("verdict"), "severity": sf.get("severity"), "finding": (sf.get("finding") or "")[:300]},
           "agentic": {"verdict": af.get("verdict"), "severity": af.get("severity"), "finding": (af.get("finding") or "")[:300],
                       "tool_calls": ag.get("tool_calls"), "lat": round(ag.get("latency_s", 0), 1)}}
    open(OUT, "a").write(json.dumps(rec) + "\n")
    div = "DIVERGE" if sf.get("verdict") != af.get("verdict") else "same"
    print(f"### {cid} on {rec['repo']}/{prefix} ({why[:40]})  [{div}]")
    print(f"  ST:    {sf.get('verdict')} ({sf.get('severity')}) :: {(sf.get('finding') or '(PASS)')[:200]}")
    print(f"  AGENT: {af.get('verdict')} ({af.get('severity')}) [{ag.get('tool_calls')} tools] :: {(af.get('finding') or '(PASS)')[:200]}\n")
print("GROUNDING EXPERIMENT DONE")
