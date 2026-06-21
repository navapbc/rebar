#!/usr/bin/env python3
"""T5c experiment: can a security review judge implications WITHOUT codebase access?

Hypothesis (Joe): no — the domain-inappropriate FP (imposing 'access level' on rebar, flagging 'leakage'
of already-in-repo data) came from the reviewer NOT seeing the broader context; an AGENT with codebase
tools should ground in the application's ACTUAL security model and avoid it. Test: run the REFIT T5c both
SINGLE-TURN (no tools) and AGENTIC (grep/read the real repo) on:
  - rebar epic 5fd2 (the plan that FP'd; rebar = a git-backed lib/CLI, NO web/auth surface)
  - snap task 0d0c (stand up an OAuth auth host; snap = a Rails app, REAL security surface)
Compare: does single-turn still import domain-inappropriate requirements? does the agent ground + avoid
the FP + find the REAL issues? Writes t5c_experiment.jsonl + prints a comparison.
"""
import json, os, subprocess
import gate_lib as G
import harness as h
import exp2_agentic as e2

TMP = h.TMP
OUT = os.path.join(TMP, "t5c_experiment.jsonl")
V8 = {c["id"]: c for c in G.load_criteria("/Users/joeoakhart/rebar/docs/experiments/plan-review-gate/criteria/criteria_v8.json")}
T5C = V8["T5c"]
# make the REFIT T5c runnable in the agent harness
e2.AGENT_CRIT["T5c"] = f"T5c — {T5C['name']}. {T5C['scenario']}"

REBAR = "/Users/joeoakhart/rebar"
SNAP = os.path.expanduser("~/snap-oakhart-manual/snap-oakhart-manual")

def show(tid, cwd):
    return json.loads(subprocess.run(["rebar", "show", tid], capture_output=True, text=True, cwd=cwd).stdout or "{}")

TARGETS = [
    ("rebar/5fd2 (NO web/auth surface; the plan that FP'd)", "5fd2-a7c2-0aec-48fa", REBAR),
    ("snap/0d0c (REAL security: OAuth auth host)", "0d0c-ebd3-5eb7-4567", SNAP),
]

open(OUT, "w").close()
print("T5c experiment: refit T5c, single-turn vs agentic, on 2 targets\n")
for label, tid, repo in TARGETS:
    t = show(tid, repo)
    title, plan = t["title"], t["description"]
    # single-turn (no tools)
    st = G.single_turn(title, plan, [T5C], model="claude-sonnet-4-6")
    sf = st["findings"][0] if st["findings"] else {}
    # agentic (codebase tools vs the real repo)
    ag = G.agent(title, plan, "T5c", repo)
    af = ag["findings"][0] if ag["findings"] else {}
    rec = {"target": label, "ticket": tid,
           "single_turn": {"verdict": sf.get("verdict"), "severity": sf.get("severity"), "finding": (sf.get("finding") or "")[:400]},
           "agentic": {"verdict": af.get("verdict"), "severity": af.get("severity"), "finding": (af.get("finding") or "")[:400],
                       "tool_calls": ag.get("tool_calls"), "iters": ag.get("iters"), "lat": round(ag.get("latency_s", 0), 1)}}
    open(OUT, "a").write(json.dumps(rec) + "\n")
    print(f"### {label}")
    print(f"  SINGLE-TURN: {sf.get('verdict')} ({sf.get('severity')})")
    print(f"     {(sf.get('finding') or '(no finding / PASS)')[:300]}")
    print(f"  AGENTIC ({ag.get('tool_calls')} tools, {round(ag.get('latency_s',0))}s): {af.get('verdict')} ({af.get('severity')})")
    print(f"     {(af.get('finding') or '(no finding / PASS)')[:300]}\n")
print("T5C EXPERIMENT DONE")
