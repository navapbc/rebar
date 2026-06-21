#!/usr/bin/env python3
"""FINAL GATE RUN — the converged v7 plan (epic 5fd2) through the full review, ALL overlays.

The capstone: run the gate as designed on its own authoritative spec, with every overlay
enabled (not shed for budget), to demonstrate the finished criteria set end-to-end and
surface anything still wrong with the plan before implementation.

Tiers:
  DET    — check-ac + clarity-check (the deterministic floor).
  1-TURN — Opus (max consistency / big-plan stability, per the grounded default), facet-chunked
           at base_chunk(opus)=12 x size_factor(epic)=0.5 = 6; the epic-applicable base+judgment
           criteria + ALL overlays forced on (each returns PASS-not-applicable where it doesn't fire).
  AGENT  — the codebase-grounded set (G6 + the leaf-grounding E4/G1G2/A1, run here for completeness +
           T10/T11 + T1/T3/T8) and the container checks G3/G4, as tool-using agents vs the rebar repo.
  COH + BROAD — the cross-cutting coherence pass + the bounded broad open-ended pass.

Writes final_gate.jsonl to TMP and prints the per-criterion verdict table.
"""
import json, os, subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import gate_lib as G
import harness as h

TMP = h.TMP
OUT = os.path.join(TMP, "final_gate.jsonl")
REBAR = "/Users/joeoakhart/rebar"
EPIC = "5fd2-a7c2-0aec-48fa"
ST_MODEL = "claude-opus-4-8"
V7 = {c["id"]: c for c in G.load_criteria()}
G.ensure_agent_crit(list(V7.values()))

d = json.loads(subprocess.run(["rebar", "show", EPIC], capture_output=True, text=True, cwd=REBAR).stdout)
TITLE, PLAN = d["title"], d["description"]

# child context for the container checks (G3/G4)
CHILDREN = ["2f3c-682a-2105-4b8f", "8e3e-50ba-765c-4d2f", "2632-5741-090e-46c3", "6d7b-41ef-f869-40dd",
            "bfa8-aadd-6739-4904", "cb28-f531-66f2-49cb", "f20a-865f-6cb3-49e4", "fd92-4b4d-b24b-41da", "a473-8af4-a493-4e0e"]
child_ctx = ""
for tid in CHILDREN:
    cd = json.loads(subprocess.run(["rebar", "show", tid], capture_output=True, text=True, cwd=REBAR).stdout or "{}")
    if cd:
        child_ctx += f"\n- CHILD {tid}: {cd.get('title','')}"
EXTRA = "\n## Children (for container coverage):" + child_ctx

lock = threading.Lock()
def W(rec):
    with lock: open(OUT, "a").write(json.dumps(rec) + "\n")

# ----- tier partition: ALL overlays forced on -----
# epic-applicable single-turn/2-step criteria via applies_at, PLUS every overlay regardless of
# level/trigger (the "all optional overlays" instruction), minus the AGENT ones.
overlay_ids = {cid for cid, c in V7.items() if c.get("routing") == "overlay"}
st_crit = []
for cid, c in V7.items():
    if c.get("exec") in ("1-TURN", "2-STEP"):
        if cid in overlay_ids or G.applies(c, "epic", has_children=True, ttype="epic"):
            st_crit.append(c)
# COH runs as a single-turn cross-cutting pass (kept 1-TURN by design)
agent_ids = [cid for cid, c in V7.items() if c.get("exec") == "AGENT"]
BROAD = {"id": "BROAD", "name": "Bounded broad open-ended pass", "facet": "broad",
         "scenario": "ADVISORY broad pass: beyond the specific criteria, what is MISSING or RISKY in this plan "
         "that a checklist wouldn't catch? Unstated assumptions, a modality not covered, a design decision with "
         "no rationale, an integration or failure mode not addressed, scope that will surprise the implementer. "
         "Surface at most 3 prioritized concerns, each grounded in specific plan text; if nothing material, say so. "
         "Do NOT restate the other criteria. Return a single entry with id 'BROAD'."}

def st_chunk_job(chunk, idx):
    r = G.single_turn(TITLE, PLAN, chunk, model=ST_MODEL, extra=EXTRA)
    for f in r["findings"]:
        W({"tier": "1-TURN", "model": "opus", "chunk": idx, **({"criterion_id": f.get("criterion_id"),
            "verdict": f.get("verdict"), "severity": f.get("severity"),
            "finding": (f.get("finding") or "")[:240], "confidence": f.get("confidence")})})
    print(f"  1-TURN chunk{idx}: {len(r['findings'])} verdicts (status={r['status']})", flush=True)

def agent_job(cid):
    plan = PLAN + (EXTRA if cid in ("G3", "G4") else "")
    r = G.agent(TITLE, plan, cid, REBAR)
    f = r["findings"][0] if r["findings"] else {}
    W({"tier": "AGENT", "model": "sonnet", "criterion_id": cid, "verdict": f.get("verdict"),
       "severity": f.get("severity"), "finding": (f.get("finding") or "")[:240],
       "tool_calls": r.get("tool_calls"), "iters": r.get("iters"), "lat": round(r.get("latency_s", 0), 1),
       "status": r.get("status")})
    print(f"  AGENT {cid:5}: {f.get('verdict')} ({r.get('tool_calls')} tools, {round(r.get('latency_s',0))}s)", flush=True)

if __name__ == "__main__":
    open(OUT, "w").close()
    print(f"FINAL GATE on epic {EPIC} ({len(PLAN)} char plan)")
    print(f"  single-turn (Opus): {len(st_crit)} criteria  | AGENT (vs rebar): {agent_ids}")

    # DET floor
    ac = subprocess.run(["rebar", "check-ac", EPIC], capture_output=True, text=True, cwd=REBAR).stdout.strip()
    cl = subprocess.run(["rebar", "clarity-check", EPIC], capture_output=True, text=True, cwd=REBAR).stdout.strip()
    W({"tier": "DET", "check_ac": ac, "clarity": cl})
    print(f"  DET: check-ac={ac[:60]} | clarity={cl[:60]}")

    chunks = G.chunk_by_facet(st_crit + [BROAD], ST_MODEL, "epic")
    # run single-turn chunks + agentic criteria concurrently
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(st_chunk_job, ch, i) for i, ch in enumerate(chunks)]
        futs += [ex.submit(agent_job, cid) for cid in agent_ids]
        for fu in as_completed(futs):
            try: fu.result()
            except Exception as e: print("  ERR", e, flush=True)

    # ----- summary -----
    rows = [json.loads(l) for l in open(OUT) if l.strip()]
    print("\n" + "=" * 72)
    print("FINAL GATE VERDICTS (epic 5fd2, full v7 suite, all overlays)")
    print("=" * 72)
    from collections import Counter
    crit_rows = [r for r in rows if r.get("criterion_id")]
    vc = Counter(r["verdict"] for r in crit_rows)
    print(f"verdict totals: {dict(vc)}  ({len(crit_rows)} criterion-verdicts)\n")
    for tier in ("1-TURN", "AGENT"):
        print(f"--- {tier} ---")
        for r in sorted([x for x in crit_rows if x["tier"] == tier], key=lambda x: (x["verdict"] != "FAIL", x["verdict"] != "AMBIGUOUS", x.get("criterion_id"))):
            mark = {"FAIL": "✗", "AMBIGUOUS": "?", "PASS": "✓"}.get(r["verdict"], "·")
            extra = f"  [{r.get('tool_calls')}t]" if tier == "AGENT" else ""
            note = f"  — {r['finding']}" if r["verdict"] in ("FAIL", "AMBIGUOUS") and r.get("finding") else ""
            print(f"  {mark} {r.get('criterion_id'):6} {r['verdict']:9} {r.get('severity',''):8}{extra}{note}")
    print("\nFINAL GATE DONE")
