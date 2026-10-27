import json, os, time, threading, random
from concurrent.futures import ThreadPoolExecutor, as_completed
import harness as h  # reuse client, SYSTEM, TOOL, build_user, call_one, CRITERIA

TMP = h.TMP
RUNS2 = os.path.join(TMP, 'runs2.jsonl')
random.seed(1729)

# ---- ticket set: 5 from batch1 + 1 new (smaller epic) ----
TICKETS = list(h.TICKETS)
ex = json.load(open(os.path.join(TMP, 'plan_extra.json')))
TICKETS.append({'key': 'small_epic', 'id': ex['id'], 'type': ex['type'], 'title': ex['title'], 'plan': ex['plan']})

ALL = h.CRITERIA[:]              # 12 criterion descriptors
NIDS = [c['id'] for c in ALL]

# ---- design: vary criteria-per-turn N with RANDOM groupings (decouple size from grouping) ----
N_VALUES = [2, 4, 6, 8, 10, 12]
PARTITIONS = 2     # random partitions of the 12 criteria per N
REPEATS = 2        # repeats per chunk

def random_partition(n):
    """Partition the 12 criteria into chunks of size n (last chunk may be smaller)."""
    idx = list(range(len(ALL)))
    random.shuffle(idx)
    chunks = [idx[i:i+n] for i in range(0, len(idx), n)]
    return [[ALL[i] for i in ch] for ch in chunks]

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

# ---- add solo (N=1) baseline for the NEW ticket only (others already in runs.jsonl batch1) ----
def make_solo_jobs():
    jobs = []
    tk = TICKETS[-1]  # small_epic
    for c in ALL:
        for rep in range(5):
            jobs.append({'ticket': tk, 'N': 1, 'partition': 0, 'chunk_idx': NIDS.index(c['id']),
                         'repeat': rep, 'chunk': [c]})
    return jobs

write_lock = threading.Lock()

def run_job(j):
    tk = j['ticket']
    for attempt in range(4):
        try:
            r = h.call_one(tk['title'], tk['plan'], j['chunk'])
            rec = {'ticket_key': tk['key'], 'ticket_id': tk['id'], 'ticket_type': tk['type'],
                   'N': j['N'], 'partition': j['partition'], 'chunk_idx': j['chunk_idx'], 'repeat': j['repeat'],
                   'ids_asked': r['ids_asked'], 'findings': r['findings'],
                   'latency_s': r['latency_s'], 'in_tok': r['in_tok'], 'out_tok': r['out_tok']}
            with write_lock:
                with open(RUNS2, 'a') as f:
                    f.write(json.dumps(rec) + '\n')
            return True
        except Exception as e:
            if attempt == 3:
                with write_lock:
                    with open(RUNS2, 'a') as f:
                        f.write(json.dumps({'ERROR': str(e), 'ticket_key': tk['key'], 'N': j['N']}) + '\n')
                return False
            time.sleep(2 * (attempt + 1))

if __name__ == '__main__':
    jobs = make_jobs() + make_solo_jobs()
    print(f"tickets={len(TICKETS)} N={N_VALUES} partitions={PARTITIONS} repeats={REPEATS}")
    print(f"total API calls = {len(jobs)}")
    open(RUNS2, 'w').close()
    done = 0
    with ThreadPoolExecutor(max_workers=10) as ex_:
        futs = [ex_.submit(run_job, j) for j in jobs]
        for fu in as_completed(futs):
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(jobs)} done", flush=True)
    print("ALL DONE", len(jobs))
