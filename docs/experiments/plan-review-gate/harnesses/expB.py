import json, os, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic, harness as h

TMP=h.TMP; REPO="/Users/joeoakhart/rebar/docs/experiments/plan-review-gate"
OUT=os.path.join(TMP,'expB.jsonl'); MODEL="claude-sonnet-4-6"
client=anthropic.Anthropic()
CRIT={c['id']:c for c in json.load(open(f"{REPO}/criteria/criteria_v6.json"))}

# Recall seeds for criteria not in the first pilot
CASES={
 "T1": ("Implement leader election among our 3 workers by building a custom distributed consensus protocol from scratch (our own Paxos variant).",
        "Use the raft library already in our dependencies for worker leader election; rationale: a custom consensus protocol is unjustified given an existing battle-tested impl (no prior-art gap)."),
 "T3": ("Issue partial refunds by calling PaymentProvider.refundPartial(amount) for the charged order.",
        "PaymentProvider exposes full-refund only (confirmed against their API reference); implement partial refunds as full-refund + re-charge of the remainder, noting the integration constraint."),
 "T4": ("Rename the public API method getUser(id) to fetchUser(id) across the codebase.",
        "Add fetchUser(id); deprecate getUser(id) with a runtime warning; keep both for two releases (expand-contract); document the migration and removal date."),
 "T5d": ("Add a status dashboard where a red dot means unhealthy and a green dot means healthy.",
         "Status dashboard conveys health by color AND an icon AND a text label; keyboard-navigable; WCAG 2.1 AA contrast; screen-reader announces the status text."),
 "T5a": ("Generate the report by calling the summarization LLM once for every row, up to 1,000,000 rows per report.",
         "Summarize rows in bounded batches (a fixed number of LLM calls regardless of row count); cache repeated summaries; the per-report LLM cost is O(1) in batches, not O(rows)."),
 "T5b": ("Before saving each address, call the third-party geocoder API synchronously inline.",
         "Geocode via the geocoder with a 2s timeout + circuit-breaker; on failure save the address as 'ungeocoded' (degraded mode) and emit a geocode_failure metric; the save path never blocks on the geocoder being down."),
 "T5c": ("The worker assumes an IAM role with PowerUserAccess and reads the database password from a committed config.yaml.",
         "The worker assumes a scoped role (read-only on the one table it needs); the DB password is read from the secrets manager at runtime and is never committed."),
}
def build_user(c):
    return (f"## Review criterion to apply (one verdict entry for id {c['id']})\n\n- [{c['id']}] {c['name']} — {c['scenario']}\n\nCall submit_review with exactly one entry for this criterion id.")
def call(cid,plan):
    c=CRIT[cid]
    system=[{"type":"text","text":h.SYSTEM},{"type":"text","text":f"# Ticket plan under review\nTitle: (seeded)\n\n## Plan\n{plan}"}]
    r=client.messages.create(model=MODEL,max_tokens=1200,system=system,tools=h.TOOL,tool_choice={"type":"tool","name":"submit_review"},messages=[{"role":"user","content":build_user(c)}])
    f=next((b.input.get("criteria",[]) for b in r.content if b.type=="tool_use"),[])
    f=f[0] if f and isinstance(f[0],dict) else {}
    return f.get("verdict"),f.get("severity")
lock=threading.Lock()
def job(args):
    cid,label,plan,rep=args
    for a in range(3):
        try:
            v,s=call(cid,plan)
            with lock: open(OUT,'a').write(json.dumps({"crit":cid,"label":label,"repeat":rep,"verdict":v,"severity":s})+'\n')
            return
        except Exception as e:
            if a==2:
                with lock: open(OUT,'a').write(json.dumps({"ERR":str(e),"crit":cid})+'\n')
            time.sleep(2)
if __name__=='__main__':
    jobs=[]
    for cid,(bad,good) in CASES.items():
        for rep in range(2):
            jobs.append((cid,"BAD",bad,rep)); jobs.append((cid,"GOOD",good,rep))
    open(OUT,'w').close()
    print(f"Exp B recall seeds: {len(CASES)} criteria x bad/good x 2 = {len(jobs)} runs")
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(as_completed([ex.submit(job,j) for j in jobs]))
    print("DONE")
