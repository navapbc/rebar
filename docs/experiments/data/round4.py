import json, os, re, time, threading, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
import harness as h
import exp2_agentic as e2

TMP = h.TMP
REBAR = "/Users/joeoakhart/rebar"
DSO = os.path.expanduser("~/digital-service-orchestra")
MODEL = "claude-sonnet-4-6"
client = anthropic.Anthropic()
CRIT = json.load(open(os.path.join(TMP, 'criteria_v3.json')))   # 13 single-turn incl EXP
CID = {c['id']: c for c in CRIT}

# facet-coherent chunks of ~6-7 covering all 13 single-turn criteria (incl overlays + EXP)
CHUNKS = [
    [CID[i] for i in ['F1', 'E2', 'E6', 'F4', 'E3', 'G5', 'EXP']],          # ac-text + scope-intent + risk-validation
    [CID[i] for i in ['E1', 'E5', 'T5a', 'T5b', 'T5c', 'T5e']],             # coherence + testing + overlays
]

def build_user(chunk):
    lines = ["## Review criteria to apply (apply EACH; one verdict entry per id)"]
    for c in chunk:
        lines.append(f"\n- [{c['id']}] {c['name']} — {c['scenario']}")
    lines.append("\nCall submit_review with exactly one entry per criterion id above.")
    return "\n".join(lines)

def single_turn(title, plan, chunk, model=MODEL, extra=""):
    system = [{"type": "text", "text": h.SYSTEM},
              {"type": "text", "text": f"# Ticket plan under review\nTitle: {title}\n{extra}\n## Plan\n{plan}",
               "cache_control": {"type": "ephemeral"}}]
    t0 = time.time()
    r = client.messages.create(model=model, max_tokens=4000, system=system, tools=h.TOOL,
                               tool_choice={"type": "tool", "name": "submit_review"},
                               messages=[{"role": "user", "content": build_user(chunk)}])
    f = next((b.input.get("criteria", []) for b in r.content if b.type == "tool_use"), [])
    return {"findings": f, "in": r.usage.input_tokens, "out": r.usage.output_tokens,
            "cr": getattr(r.usage, "cache_read_input_tokens", 0) or 0, "lat": time.time() - t0}

# ---------- overlay deterministic trigger rules (low-FP keyword/signal) ----------
DET_RULES = {
 "T1":  r"\b(api|sdk|third.?party|integrat|oauth|credential|token|secret|migrat|backward.?compat|deprecat|novel|new (library|package|dependency|architecture|pattern))\b",
 "T5a": r"\b(latency|throughput|performance|scal|N\+1|batch|loop|cache|memory|compute|LLM call|concurren)\b",
 "T5b": r"\b(retry|timeout|failover|idempoten|error.handling|circuit|fail.open|fail.clos|graceful|external (api|service)|write op)\b",
 "T5c": r"\b(auth|oauth|credential|token|secret|PII|endpoint|encrypt|signature|sign(ing|ed)?|access control|forgery|replay)\b",
 "T5d": r"\b(UI|button|form|screen|page|modal|dashboard|keyboard|wcag|aria|accessib|color|contrast)\b",
 "T5e": r"\b(refactor|coupl|abstraction|cross.component|new (pattern|interface)|config|ADR|maintainab|module boundary)\b",
 "T6":  r"\b(UI|button|form|screen|user.facing|non.happy|empty state|validation|error message|flow)\b",
 "T7":  r"\b(\bdoc\b|docs|README|CLAUDE\.md|ADR|guide|documentation)\b",
 "T8":  r"\b(agent|prompt|LLM|sub.?agent|model|reviewer|skill|instruction)\b",
 "T9":  r"\b(shared|global|singleton|state|cache|config key|lifecycle|concurren)\b",
}
def deterministic_overlays(plan):
    p = plan.lower()
    return {ov: bool(re.search(rx, p, re.I)) for ov, rx in DET_RULES.items()}

ROUTER_TOOL = [{"name": "route", "description": "Pick which review overlays are RELEVANT to this plan.",
  "input_schema": {"type": "object", "properties": {"overlays": {"type": "array", "items": {"type": "object",
    "properties": {"id": {"type": "string"}, "relevant": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["id", "relevant"]}}}, "required": ["overlays"]}}]
OVERLAY_DESC = ("T1 prior-art/novel-arch, T5a performance, T5b reliability, T5c security, T5d accessibility, "
                "T5e maintainability, T6 UX non-happy-path, T7 documentation, T8 LLM/prompt-antipatterns, T9 shared-state lifecycle")
def llm_router(title, plan):
    sys = ("You are the relevance router for a plan-review gate. Given a ticket plan, decide which optional review "
           "OVERLAYS are RELEVANT (worth running) vs not-applicable. Be precise — only mark an overlay relevant if the "
           "plan actually has surface for it. Overlays: " + OVERLAY_DESC)
    r = client.messages.create(model=MODEL, max_tokens=1500, system=sys, tools=ROUTER_TOOL,
                               tool_choice={"type": "tool", "name": "route"},
                               messages=[{"role": "user", "content": f"Title: {title}\n\nPlan:\n{plan[:6000]}"}])
    out = next((b.input.get("overlays", []) for b in r.content if b.type == "tool_use"), [])
    return {o['id']: o.get('relevant', False) for o in out}

# ---------- T8 bot-psychologist-style structural-gap probe (agentic, for PIL comparison) ----------
T8_CRIT = ("T8 — LLM/prompt structural-completeness probe (AGENT). This plan defines an LLM/agent system (prompts, "
           "sub-agents, reviewers, output schemas, enums). Probe for STRUCTURAL GAPS a generic checklist misses: "
           "(a) an output schema/enum referenced but whose value vocabulary is never defined; (b) a processing protocol "
           "or decision rule referenced but not co-located with the schema that needs it; (c) a counter/state increment "
           "whose placement is ambiguous; (d) a fallback path for an incomplete/failed sub-step that is unspecified; "
           "(e) instruction-locality or pink-elephant antipatterns in prompt text. Use Grep/Read over the repo to check "
           "whether referenced agents/skills/enums exist and are fully specified. Report each PROVEN gap with evidence.")

lock = threading.Lock()
def W(path, rec):
    with lock:
        open(os.path.join(TMP, path), 'a').write(json.dumps(rec) + '\n')

def ticket_plan(tid, cwd):
    d = json.loads(subprocess.run(['rebar', 'show', tid], capture_output=True, text=True, cwd=cwd).stdout)
    return d['title'], d['description']

# ===================== STREAMS =====================
def stream_A():  # epic + 9 children, single-turn suite
    epic_children = ["2f3c-682a-2105-4b8f", "8e3e-50ba-765c-4d2f", "2632-5741-090e-46c3", "6d7b-41ef-f869-40dd",
                     "bfa8-aadd-6739-4904", "cb28-f531-66f2-49cb", "f20a-865f-6cb3-49e4", "fd92-4b4d-b24b-41da", "a473-8af4-a493-4e0e"]
    targets = ["5fd2-a7c2-0aec-48fa"] + epic_children
    # child summaries for the epic's container (G3/G4) context
    child_ctx = ""
    for tid in epic_children:
        t, p = ticket_plan(tid, REBAR)
        child_ctx += f"\n- CHILD {tid}: {t}"
    for tid in targets:
        t, p = ticket_plan(tid, REBAR)
        extra = ("\n## Children (for container coverage):" + child_ctx) if tid == "5fd2-a7c2-0aec-48fa" else ""
        for ci, chunk in enumerate(CHUNKS):
            r = single_turn(t, p, chunk, extra=extra)
            W('r4_A.jsonl', {"ticket": tid, "chunk": ci, "findings": r['findings'], "in": r['in'], "cr": r['cr'], "out": r['out']})
        print(f"  A {tid} done", flush=True)

def stream_B():  # 12 DSO sample, single-turn suite (incl EXP + overlays)
    sample = json.load(open(os.path.join(TMP, 'dso_sample.json')))
    for s in sample:
        for ci, chunk in enumerate(CHUNKS):
            r = single_turn(s['plan'][:200] and s['id'], s['plan'], chunk) if False else single_turn(s['id'], s['plan'], chunk)
            W('r4_B.jsonl', {"ticket": s['id'], "type": s['type'], "bc": s['brainstorm_complete'], "chunk": ci, "findings": r['findings']})
        print(f"  B {s['id']} done", flush=True)

def stream_C():  # overlay triggering: deterministic vs LLM router, on DSO sample + epic
    sample = json.load(open(os.path.join(TMP, 'dso_sample.json')))
    items = [(s['id'], s['plan']) for s in sample]
    t, p = ticket_plan("5fd2-a7c2-0aec-48fa", REBAR); items.append(("epic-5fd2", p))
    for tid, plan in items:
        det = deterministic_overlays(plan)
        llm = llm_router(tid, plan)
        W('r4_C.jsonl', {"ticket": tid, "det": det, "llm": llm})
        print(f"  C {tid} done", flush=True)

def stream_D():  # PIL comparison: our suite + T8 probe on 3 PIL epics
    pil = {"b575-ac1c-f720-4839": 12, "4100-95a4-07ce-41ab": 5, "e7f3-2b45-8d7d-4c68": 8}
    for tid, dso_n in pil.items():
        t, p = ticket_plan(tid, DSO)
        allf = []
        for ci, chunk in enumerate(CHUNKS):
            r = single_turn(t, p, chunk)
            allf += r['findings']
        # T8 structural-gap agentic probe over the DSO repo
        ag = e2.run_agentic(t, p, "T8", DSO) if False else None
        W('r4_D.jsonl', {"ticket": tid, "dso_findings": dso_n, "our_singleturn": allf})
        print(f"  D {tid} single-turn done", flush=True)

if __name__ == '__main__':
    for f in ['r4_A.jsonl', 'r4_B.jsonl', 'r4_C.jsonl', 'r4_D.jsonl']:
        open(os.path.join(TMP, f), 'w').close()
    print("STREAM A: epic + children")
    stream_A()
    print("STREAM B: DSO sample")
    stream_B()
    print("STREAM C: overlay triggering")
    stream_C()
    print("STREAM D: PIL single-turn")
    stream_D()
    print("ROUND4 DONE")
