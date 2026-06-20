import json, os, subprocess
import exp1_substance as e1
import exp2_agentic as e2
import harness as h

TMP = h.TMP
OUT = os.path.join(TMP, 'gate_run.jsonl')
REBAR = "/Users/joeoakhart/rebar"

# current (revised) epic plan
d = json.loads(subprocess.run(['rebar', 'show', '5fd2-a7c2-0aec-48fa'], capture_output=True, text=True, cwd=REBAR).stdout)
TITLE, PLAN = d['title'], d['description']

CID = {c['id']: c for c in e1.CRIT}
# facet-coherent chunks of 6 (epic -> size_factor 0.5 x base 12 = 6); 2 chunks cover all 12 single-turn criteria
CHUNK_A = [CID[i] for i in ['F1', 'E2', 'E6', 'F4', 'E3', 'G5']]      # ac-text-quality + scope-intent
CHUNK_B = [CID[i] for i in ['E1', 'E5', 'T5a', 'T5b', 'T5c', 'T5e']]  # coherence + testing + overlays
BROAD = {"id": "BROAD", "name": "Bounded broad open-ended pass (unknown-unknowns)",
         "scenario": "ADVISORY broad pass: beyond the specific criteria, what is MISSING or RISKY in this plan that a checklist wouldn't catch? Consider unstated assumptions, a modality not covered, a design decision with no rationale, an integration or failure mode not addressed, or scope that will surprise the implementer. Surface at most 3 prioritized concerns, each grounded in specific plan text; if nothing material, say so. Do NOT restate the other criteria. Return a single entry with id 'BROAD'."}

open(OUT, 'w').close()
recs = []

def write(rec):
    recs.append(rec)
    with open(OUT, 'a') as f:
        f.write(json.dumps(rec) + '\n')

# --- DET tier (deterministic floor) ---
ac = subprocess.run(['rebar', 'check-ac', '5fd2-a7c2-0aec-48fa'], capture_output=True, text=True, cwd=REBAR).stdout.strip()
cl = subprocess.run(['rebar', 'clarity-check', '5fd2-a7c2-0aec-48fa'], capture_output=True, text=True, cwd=REBAR).stdout.strip()
write({"tier": "DET", "check_ac": ac, "clarity": cl})
print("DET:", ac, "|", cl)

# --- SINGLE-TURN tier: 2 facet-packed chunks x 2 repeats (Opus, cached) + broad pass ---
for label, chunk in [("chunkA", CHUNK_A), ("chunkB", CHUNK_B)]:
    for rep in range(2):
        r = e1.call_cached(TITLE, PLAN, chunk) if False else None
        # use Opus for the high-reliability single-turn default
        import time, anthropic
        client = anthropic.Anthropic()
        system = [{"type": "text", "text": h.SYSTEM},
                  {"type": "text", "text": f"# Ticket plan under review\nTitle: {TITLE}\n\n## Plan\n{PLAN}",
                   "cache_control": {"type": "ephemeral"}}]
        t0 = time.time()
        resp = client.messages.create(model="claude-opus-4-8", max_tokens=4000, system=system,
                                      tools=h.TOOL, tool_choice={"type": "tool", "name": "submit_review"},
                                      messages=[{"role": "user", "content": e1.build_user(chunk)}])
        findings = next((b.input.get("criteria", []) for b in resp.content if b.type == "tool_use"), [])
        write({"tier": "1-TURN", "model": "opus", "chunk": label, "repeat": rep, "ids": [c['id'] for c in chunk],
               "findings": findings, "in": resp.usage.input_tokens, "out": resp.usage.output_tokens,
               "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0) or 0, "lat": time.time() - t0})
        print(f"  1-TURN {label} rep{rep}: {len(findings)} verdicts")

# broad pass (Opus, 1)
import time, anthropic
client = anthropic.Anthropic()
system = [{"type": "text", "text": h.SYSTEM},
          {"type": "text", "text": f"# Ticket plan under review\nTitle: {TITLE}\n\n## Plan\n{PLAN}", "cache_control": {"type": "ephemeral"}}]
resp = client.messages.create(model="claude-opus-4-8", max_tokens=3000, system=system,
                              tools=h.TOOL, tool_choice={"type": "tool", "name": "submit_review"},
                              messages=[{"role": "user", "content": e1.build_user([BROAD])}])
bf = next((b.input.get("criteria", []) for b in resp.content if b.type == "tool_use"), [])
write({"tier": "BROAD", "model": "opus", "findings": bf})
print(f"  BROAD: {len(bf)}")

# --- AGENT tier: E4, G1G2, A1 as individual tool-using agents vs rebar ---
for cid in ["E4", "G1G2", "A1"]:
    r = e2.run_agentic(TITLE, PLAN, cid, REBAR)
    write({"tier": "AGENT", "model": "sonnet", "criterion": cid, "findings": r['findings'],
           "tool_calls": r['tool_calls'], "iters": r['iters'], "lat": r['latency_s'],
           "in": r['in_tok'], "out": r['out_tok'], "cache_read": r.get('cache_read', 0)})
    print(f"  AGENT {cid}: {r['tool_calls']} tool calls, {len(r['findings'])} verdicts")

print("GATE RUN DONE")
