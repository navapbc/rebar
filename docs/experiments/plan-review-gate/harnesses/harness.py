import json, os, time, sys, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic

TMP = os.path.expanduser('~/.claude/jobs/3ef65161/tmp')
MODEL = "claude-sonnet-4-6"
REPEATS = 5
CHUNK_SIZES = [1, 3, 6, 12]   # 12 = ALL
RUNS_PATH = os.path.join(TMP, 'runs.jsonl')

client = anthropic.Anthropic()
CRITERIA = json.load(open(os.path.join(TMP, 'criteria.json')))
CID = {c['id']: c for c in CRITERIA}

# --- build the ticket set ---
dso = json.load(open(os.path.join(TMP, 'plans_dso.json')))
epic = json.load(open(os.path.join(TMP, 'epic.json')))
TICKETS = []
for band, t in dso.items():
    TICKETS.append({'key': band, 'id': t['id'], 'type': t['type'], 'title': t['title'], 'plan': t['plan']})
TICKETS.append({'key': 'dogfood_epic', 'id': epic['ticket_id'], 'type': epic['ticket_type'],
                'title': epic['title'], 'plan': epic['description']})

SYSTEM = """You are an expert software-plan reviewer. You review a ticket's implementation PLAN (its description and acceptance criteria) BEFORE an agent executes it. You are NOT the author of the plan.

Your job: apply EACH review criterion you are given to the plan and return a structured per-criterion verdict. This is ADVISORY coaching review — your findings help the author improve the plan; you do not block anything.

How to author every finding (follow strictly):
- GROUND each finding in a specific criterion and a specific location in the plan (quote the phrase / name the section / cite the acceptance-criterion).
- Be SPECIFIC and ACTIONABLE, never generic.
- Give a concrete SUGGESTED edit ONLY when you are confident; mark it as a suggestion. A wrong fix is worse than none.
- SEVERITY-rank: critical = agent can't proceed or will build the wrong thing; major = a required element is absent and will cause rework; minor = present but thin. Plan-risk is capped (no 'critical' merely because code isn't running yet).
- SEPARATE LANGUAGE FROM SUBSTANCE: interpret ambiguous LANGUAGE reasonably (don't manufacture a defect from phrasing that clearly has a sound meaning), but scrutinize SUBSTANCE skeptically — an unsubstantiated assurance or an unaddressed case is a real gap. Resolve doubt about substance by demanding evidence, not by trusting the plan (giving the plan's claims the benefit of the doubt is sycophancy, not charity).
- Report only findings you can ground in specific evidence. When a criterion is satisfied, return PASS with severity 'none' and no finding text — an accurate review reports exactly the real findings.
- For criteria that depend on live code you cannot see, return AMBIGUOUS and name the fact that would need checking; base every verdict only on what the plan and your evidence actually show.

Verdict per criterion: PASS (criterion satisfied), AMBIGUOUS (cannot decide / needs escalation or codebase access), FAIL (criterion not met — a real finding).
You MUST return exactly one entry per criterion you are given, using the criterion's id.

REASON BEFORE VERDICT: use the tool's `analysis` field to reason through the plan against the criteria,
THEN record verdicts — reaching a verdict before reasoning measurably degrades quality. Fill `analysis`
first. (This is about ordering reasoning ahead of the structured verdict; gathering information is always a
legitimate first step, not the thing to avoid.)
REVIEW QUALITY IS ACCURACY, NOT VOLUME: judge substance, not length — a terse plan that satisfies a
criterion PASSES, and a longer plan is not automatically better. The NUMBER of findings is likewise not a
measure of a good review: when the plan is sound, the best possible result is ZERO findings. Surface a
finding only where it would cause rework or the wrong thing to be built and it adds real value to the author."""

TOOL = [{
  "name": "submit_review",
  "description": "Submit the per-criterion review of the plan.",
  "input_schema": {
    "type": "object",
    "properties": {
      "analysis": {"type": "string", "description": "REASON FIRST: think through the plan against each criterion here BEFORE the verdicts (scratchpad; fill this before `criteria`). Reasoning-before-verdict measurably beats emitting the verdict first."},
      "criteria": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "criterion_id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["PASS", "AMBIGUOUS", "FAIL"]},
            "severity": {"type": "string", "enum": ["none", "minor", "major", "critical"]},
            "location": {"type": "string", "description": "Where in the plan (quote/section/AC ref); empty if PASS."},
            "finding": {"type": "string", "description": "The specific actionable finding; empty if PASS."},
            "suggested_edit": {"type": "string", "description": "Concrete suggested fix, or empty."},
            "confidence": {"type": "number", "description": "0..1 self-reported confidence in this finding."}
          },
          "required": ["criterion_id", "verdict", "severity", "location", "finding", "confidence"]
        }
      }
    },
    "required": ["analysis", "criteria"]
  }
}]

def build_user(plan_title, plan_text, chunk_criteria):
    lines = ["# Ticket plan under review", f"Title: {plan_title}", "", "## Plan (description + acceptance criteria)", plan_text, "",
             "## Review criteria to apply (apply EACH; one verdict entry per id)"]
    for c in chunk_criteria:
        lines.append(f"- [{c['id']}] {c['name']}: {c['scenario']}")
    lines.append("")
    lines.append("Call submit_review with exactly one entry per criterion id above.")
    return "\n".join(lines)

def chunks(lst, n):
    return [lst[i:i+n] for i in range(0, len(lst), n)]

write_lock = threading.Lock()

def call_one(plan_title, plan_text, chunk_criteria):
    t0 = time.time()
    resp = client.messages.create(
        model=MODEL, max_tokens=4000, system=SYSTEM,
        tools=TOOL, tool_choice={"type": "tool", "name": "submit_review"},
        messages=[{"role": "user", "content": build_user(plan_title, plan_text, chunk_criteria)}],
    )
    dt = time.time() - t0
    findings = None
    for block in resp.content:
        if block.type == "tool_use":
            findings = block.input.get("criteria", [])
    return {
        "findings": findings or [],
        "latency_s": dt,
        "in_tok": resp.usage.input_tokens,
        "out_tok": resp.usage.output_tokens,
        "ids_asked": [c['id'] for c in chunk_criteria],
    }

def make_jobs():
    jobs = []
    for tk in TICKETS:
        for cs in CHUNK_SIZES:
            for rep in range(1, REPEATS + 1):
                for ci, chunk in enumerate(chunks(CRITERIA, cs)):
                    jobs.append({"ticket": tk, "chunk_size": cs, "repeat": rep, "chunk_idx": ci, "chunk": chunk})
    return jobs

def run_job(j):
    tk = j["ticket"]
    for attempt in range(4):
        try:
            r = call_one(tk["title"], tk["plan"], j["chunk"])
            rec = {
                "ticket_key": tk["key"], "ticket_id": tk["id"], "ticket_type": tk["type"],
                "chunk_size": j["chunk_size"], "repeat": j["repeat"], "chunk_idx": j["chunk_idx"],
                "ids_asked": r["ids_asked"], "findings": r["findings"],
                "latency_s": r["latency_s"], "in_tok": r["in_tok"], "out_tok": r["out_tok"],
            }
            with write_lock:
                with open(RUNS_PATH, "a") as f:
                    f.write(json.dumps(rec) + "\n")
            return True
        except Exception as e:
            if attempt == 3:
                with write_lock:
                    with open(RUNS_PATH, "a") as f:
                        f.write(json.dumps({"ERROR": str(e), "ticket_key": tk["key"],
                                            "chunk_size": j["chunk_size"], "repeat": j["repeat"],
                                            "chunk_idx": j["chunk_idx"]}) + "\n")
                return False
            time.sleep(2 * (attempt + 1))

if __name__ == "__main__":
    jobs = make_jobs()
    # count API calls and run-cells
    cells = len(TICKETS) * len(CHUNK_SIZES) * REPEATS
    print(f"tickets={len(TICKETS)} chunk_sizes={CHUNK_SIZES} repeats={REPEATS}")
    print(f"run-cells (ticket x chunk x repeat) = {cells}; total API calls = {len(jobs)}")
    open(RUNS_PATH, "w").close()
    done = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(run_job, j) for j in jobs]
        for fu in as_completed(futs):
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(jobs)} calls done", flush=True)
    print("ALL DONE", len(jobs))
