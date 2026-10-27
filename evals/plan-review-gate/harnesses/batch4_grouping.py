import json, os, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import harness as h
from batch3_opus import call_model  # reuse generic caller

TMP = h.TMP
RUNS4 = os.path.join(TMP, 'runs4_group.jsonl')
MODEL = "claude-sonnet-4-6"  # grouping question is about Sonnet's ceiling

# 6 tickets (batch2 set incl small_epic)
TICKETS = list(h.TICKETS)
ex = json.load(open(os.path.join(TMP, 'plan_extra.json')))
TICKETS.append({'key': 'small_epic', 'id': ex['id'], 'type': ex['type'], 'title': ex['title'], 'plan': ex['plan']})

CID = {c['id']: c for c in h.CRITERIA}
def crit(ids):
    return [CID[i] for i in ids]

# --- COHERENT (affinity) groupings: cluster criteria that examine the same plan facet ---
# facet ACQ = acceptance-criteria/requirement quality; INT = intent/scope; CODE = implementation grounding
COHERENT = {
    4: [crit(['F1', 'E1', 'E2', 'E6']),          # AC quality
        crit(['F4', 'E3', 'G5', 'A1']),          # intent/scope
        crit(['E5', 'E4', 'G1G2', 'COH'])],      # code grounding
    6: [crit(['F1', 'E1', 'E2', 'E6', 'F4', 'E3']),     # requirements + intent
        crit(['G5', 'A1', 'E5', 'E4', 'G1G2', 'COH'])], # sizing + grounding
}
# --- ANTI-AFFINITY: each chunk spans all facets (maximal context-switching) ---
ANTI = {
    4: [crit(['F1', 'F4', 'E5', 'E1']),
        crit(['E2', 'E3', 'E4', 'A1']),
        crit(['E6', 'G5', 'G1G2', 'COH'])],
    6: [crit(['F1', 'E1', 'F4', 'E3', 'E5', 'E4']),
        crit(['E2', 'E6', 'G5', 'A1', 'G1G2', 'COH'])],
}
STRATEGIES = {'coherent': COHERENT, 'anti': ANTI}
N_VALUES = [4, 6]
REPEATS = 3

def make_jobs():
    jobs = []
    for tk in TICKETS:
        for strat, groups in STRATEGIES.items():
            for n in N_VALUES:
                for ci, chunk in enumerate(groups[n]):
                    for rep in range(REPEATS):
                        jobs.append({'ticket': tk, 'strategy': strat, 'N': n,
                                     'chunk_idx': ci, 'repeat': rep, 'chunk': chunk})
    return jobs

write_lock = threading.Lock()
def run_job(j):
    tk = j['ticket']
    for attempt in range(4):
        try:
            r = call_model(tk['title'], tk['plan'], j['chunk'], MODEL)
            rec = {'model': MODEL, 'ticket_key': tk['key'], 'ticket_id': tk['id'], 'ticket_type': tk['type'],
                   'strategy': j['strategy'], 'N': j['N'], 'chunk_idx': j['chunk_idx'], 'repeat': j['repeat'],
                   'ids_asked': r['ids_asked'], 'findings': r['findings'],
                   'latency_s': r['latency_s'], 'in_tok': r['in_tok'], 'out_tok': r['out_tok']}
            with write_lock:
                with open(RUNS4, 'a') as f:
                    f.write(json.dumps(rec) + '\n')
            return True
        except Exception as e:
            if attempt == 3:
                with write_lock:
                    with open(RUNS4, 'a') as f:
                        f.write(json.dumps({'ERROR': str(e), 'ticket_key': tk['key'],
                                            'strategy': j['strategy'], 'N': j['N']}) + '\n')
                return False
            time.sleep(2 * (attempt + 1))

if __name__ == '__main__':
    jobs = make_jobs()
    print(f"GROUPING (Sonnet): tickets={len(TICKETS)} strategies={list(STRATEGIES)} N={N_VALUES} calls={len(jobs)}")
    open(RUNS4, 'w').close()
    done = 0
    with ThreadPoolExecutor(max_workers=10) as ex_:
        futs = [ex_.submit(run_job, j) for j in jobs]
        for fu in as_completed(futs):
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(jobs)} done", flush=True)
    print("ALL DONE", len(jobs))
