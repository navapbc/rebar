import json, os, time, threading, random
from concurrent.futures import ThreadPoolExecutor, as_completed
import harness as h
import anthropic

TMP = h.TMP
RUNS3 = os.path.join(TMP, 'runs3_opus.jsonl')
MODEL = "claude-opus-4-8"
random.seed(4242)
client = anthropic.Anthropic()

# 5 tickets (the batch1 set; all also in batch2). Opus is the variable.
TICKETS = list(h.TICKETS)  # trivial, moderate, complex_leaf, container_epic, dogfood_epic
ALL = h.CRITERIA[:]
N_VALUES = [2, 4, 6, 8, 12]
PARTITIONS = 2
REPEATS = 2

def call_model(plan_title, plan_text, chunk_criteria, model):
    t0 = time.time()
    resp = client.messages.create(
        model=model, max_tokens=4000, system=h.SYSTEM,
        tools=h.TOOL, tool_choice={"type": "tool", "name": "submit_review"},
        messages=[{"role": "user", "content": h.build_user(plan_title, plan_text, chunk_criteria)}],
    )
    dt = time.time() - t0
    findings = None
    for b in resp.content:
        if b.type == "tool_use":
            findings = b.input.get("criteria", [])
    return {"findings": findings or [], "latency_s": dt,
            "in_tok": resp.usage.input_tokens, "out_tok": resp.usage.output_tokens,
            "ids_asked": [c['id'] for c in chunk_criteria]}

def random_partition(n):
    idx = list(range(len(ALL)))
    random.shuffle(idx)
    return [[ALL[i] for i in idx[i:i+n]] for i in range(0, len(idx), n)]

def make_jobs():
    jobs = []
    for tk in TICKETS:
        for n in N_VALUES:
            for part in range(PARTITIONS):
                chunks = random_partition(n)
                for ci, chunk in enumerate(chunks):
                    for rep in range(REPEATS):
                        jobs.append({'ticket': tk, 'N': n, 'partition': part, 'chunk_idx': ci,
                                     'repeat': rep, 'chunk': chunk})
    return jobs

write_lock = threading.Lock()
def run_job(j):
    tk = j['ticket']
    for attempt in range(4):
        try:
            r = call_model(tk['title'], tk['plan'], j['chunk'], MODEL)
            rec = {'model': MODEL, 'ticket_key': tk['key'], 'ticket_id': tk['id'], 'ticket_type': tk['type'],
                   'N': j['N'], 'partition': j['partition'], 'chunk_idx': j['chunk_idx'], 'repeat': j['repeat'],
                   'ids_asked': r['ids_asked'], 'findings': r['findings'],
                   'latency_s': r['latency_s'], 'in_tok': r['in_tok'], 'out_tok': r['out_tok']}
            with write_lock:
                with open(RUNS3, 'a') as f:
                    f.write(json.dumps(rec) + '\n')
            return True
        except Exception as e:
            if attempt == 3:
                with write_lock:
                    with open(RUNS3, 'a') as f:
                        f.write(json.dumps({'ERROR': str(e), 'ticket_key': tk['key'], 'N': j['N']}) + '\n')
                return False
            time.sleep(3 * (attempt + 1))

if __name__ == '__main__':
    jobs = make_jobs()
    print(f"OPUS capacity: tickets={len(TICKETS)} N={N_VALUES} calls={len(jobs)}")
    open(RUNS3, 'w').close()
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(run_job, j) for j in jobs]
        for fu in as_completed(futs):
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(jobs)} done", flush=True)
    print("ALL DONE", len(jobs))
