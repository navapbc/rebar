import json, os, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic, harness as h

TMP = h.TMP
REPO = "/Users/joeoakhart/rebar/docs/experiments/plan-review-gate"
OUT = os.path.join(TMP, 'expA.jsonl')
MODEL = "claude-sonnet-4-6"
client = anthropic.Anthropic()
CRIT = {c['id']: c for c in json.load(open(f"{REPO}/criteria/criteria_v6.json"))}
DSO = json.load(open(f"{REPO}/runs/dso_sample.json"))

# Criteria to measure precision/false-fire on (new + never-run + the extended roll-ins)
EVAL = ['G6','T10','T11','T12','T1','T3','T4','T5d','COH','T9','T5a','T5b','T5c','E5','A1']

def build_user(c):
    return (f"## Review criterion to apply (one verdict entry for id {c['id']})\n\n- [{c['id']}] {c['name']} — {c['scenario']}\n\n"
            "Call submit_review with exactly one entry for this criterion id.")

def call(cid, title, plan):
    c = CRIT[cid]
    system=[{"type":"text","text":h.SYSTEM},
            {"type":"text","text":f"# Ticket plan under review\nTitle: {title}\n\n## Plan\n{plan}","cache_control":{"type":"ephemeral"}}]
    r=client.messages.create(model=MODEL,max_tokens=1200,system=system,tools=h.TOOL,
                             tool_choice={"type":"tool","name":"submit_review"},
                             messages=[{"role":"user","content":build_user(c)}])
    f=next((b.input.get("criteria",[]) for b in r.content if b.type=="tool_use"),[])
    f=f[0] if f and isinstance(f[0],dict) else {}
    return f.get("verdict"), f.get("severity")

lock=threading.Lock()
def job(args):
    cid,tid,title,plan=args
    for a in range(3):
        try:
            v,s=call(cid,title,plan)
            with lock: open(OUT,'a').write(json.dumps({"crit":cid,"ticket":tid,"verdict":v,"severity":s})+'\n')
            return
        except Exception as e:
            if a==2:
                with lock: open(OUT,'a').write(json.dumps({"ERR":str(e),"crit":cid,"ticket":tid})+'\n')
            time.sleep(2)

if __name__=='__main__':
    jobs=[]
    for s in DSO:
        for cid in EVAL:
            jobs.append((cid, s['id'], s['plan'][:8000]))
            # build_user needs title; pass id as title proxy
    # rebuild with title
    jobs=[(cid, s['id'], f"{s['type']} {s['id']}", s['plan'][:8000]) for s in DSO for cid in EVAL]
    open(OUT,'w').close()
    print(f"Exp A (false-fire on real DSO plans): {len(EVAL)} criteria x {len(DSO)} DSO tickets = {len(jobs)} runs")
    done=0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs=[ex.submit(job,(cid,tid,title,plan)) for (cid,tid,title,plan) in jobs]
        for f in as_completed(futs):
            done+=1
            if done%40==0: print(f"  {done}/{len(jobs)}",flush=True)
    print("DONE")
