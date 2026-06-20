import json, os, time, threading, random
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
import harness as h

TMP = h.TMP
RUNS = os.path.join(TMP, 'exp1_substance.jsonl')
MODEL = "claude-sonnet-4-6"
random.seed(99)
client = anthropic.Anthropic()

CRIT = json.load(open(os.path.join(TMP, 'criteria_v2.json')))  # 12 fully-specified single-turn criteria
TICKETS = list(h.TICKETS)  # trivial, moderate, complex_leaf, container_epic, dogfood_epic
N_VALUES = [2, 4, 6, 12]
PARTITIONS, REPEATS = 2, 2

def build_user(chunk):
    lines = ["## Review criteria to apply (apply EACH; one verdict entry per id)"]
    for c in chunk:
        lines.append(f"\n- [{c['id']}] {c['name']} — {c['scenario']}")
    lines.append("\nCall submit_review with exactly one entry per criterion id above.")
    return "\n".join(lines)

def call_cached(plan_title, plan_text, chunk):
    # system carries the stable prefix (instructions + plan) and is CACHED;
    # the user message carries only the varying criteria chunk.
    system = [
        {"type": "text", "text": h.SYSTEM},
        {"type": "text", "text": f"# Ticket plan under review\nTitle: {plan_title}\n\n## Plan (description + acceptance criteria)\n{plan_text}",
         "cache_control": {"type": "ephemeral"}},
    ]
    t0 = time.time()
    resp = client.messages.create(
        model=MODEL, max_tokens=4000, system=system,
        tools=h.TOOL, tool_choice={"type": "tool", "name": "submit_review"},
        messages=[{"role": "user", "content": build_user(chunk)}],
    )
    dt = time.time() - t0
    findings = None
    for b in resp.content:
        if b.type == "tool_use":
            findings = b.input.get("criteria", [])
    u = resp.usage
    return {"findings": findings or [], "latency_s": dt,
            "in_tok": u.input_tokens, "out_tok": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "ids_asked": [c['id'] for c in chunk]}

def random_partition(n):
    idx = list(range(len(CRIT)))
    random.shuffle(idx)
    return [[CRIT[i] for i in idx[i:i+n]] for i in range(0, len(idx), n)]

def make_jobs():
    jobs = []
    for tk in TICKETS:
        for n in N_VALUES:
            for part in range(PARTITIONS):
                for ci, chunk in enumerate(random_partition(n)):
                    for rep in range(REPEATS):
                        jobs.append({'ticket': tk, 'N': n, 'partition': part, 'chunk_idx': ci, 'repeat': rep, 'chunk': chunk})
    return jobs

lock = threading.Lock()
def run_job(j):
    tk = j['ticket']
    for attempt in range(4):
        try:
            r = call_cached(tk['title'], tk['plan'], j['chunk'])
            rec = {'model': MODEL, 'ticket_key': tk['key'], 'ticket_type': tk['type'],
                   'N': j['N'], 'partition': j['partition'], 'chunk_idx': j['chunk_idx'], 'repeat': j['repeat'],
                   'ids_asked': r['ids_asked'], 'findings': r['findings'], 'latency_s': r['latency_s'],
                   'in_tok': r['in_tok'], 'out_tok': r['out_tok'], 'cache_read': r['cache_read'], 'cache_write': r['cache_write']}
            with lock:
                with open(RUNS, 'a') as f:
                    f.write(json.dumps(rec) + '\n')
            return True
        except Exception as e:
            if attempt == 3:
                with lock:
                    open(RUNS, 'a').write(json.dumps({'ERROR': str(e), 'ticket_key': tk['key'], 'N': j['N']}) + '\n')
                return False
            time.sleep(2 * (attempt + 1))

if __name__ == '__main__':
    jobs = make_jobs()
    print(f"EXP1 substantive single-turn: {len(CRIT)} rich criteria, tickets={len(TICKETS)}, N={N_VALUES}, calls={len(jobs)}")
    open(RUNS, 'w').close()
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(run_job, j) for j in jobs]
        for fu in as_completed(futs):
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(jobs)}", flush=True)
    print("ALL DONE", len(jobs))
