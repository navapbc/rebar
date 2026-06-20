import json, os, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic, harness as h

TMP = h.TMP
OUT = os.path.join(TMP, 'seedpilot.jsonl')
MODEL = "claude-sonnet-4-6"
client = anthropic.Anthropic()
CRIT = {c['id']: c for c in json.load(open('/Users/joeoakhart/rebar/docs/experiments/plan-review-gate/criteria/criteria_v6.json'))}

# Seeded gold cases: each criterion gets a KNOWN-BAD plan (contains exactly the defect it should catch)
# and a matched KNOWN-GOOD plan. Expect: bad -> FAIL (recall), good -> PASS (precision/no false-fire).
CASES = {
 "G6": ("Add idempotent claim: in claim(), check `if not key_exists(id): write_claim(id)` so duplicate claims are prevented. AC: a second claim of the same ticket is a no-op.",
        "Add idempotent claim via an atomic conditional write (compare-and-swap: write_claim_if_absent(id) in one atomic op / a unique-constraint insert), so concurrent duplicate claims cannot both succeed. AC: under 50 concurrent claims exactly one wins."),
 "T10": ("Provision the export pipeline: a Terraform module creating an S3 bucket and a Lambda. The Lambda role gets `AdministratorAccess`. The third-party API key is passed as a plaintext Terraform variable. State is local (terraform.tfstate in the repo).",
         "Provision the export pipeline in Terraform with remote state + DynamoDB locking; the Lambda role is a scoped policy (s3:PutObject on the one bucket only); the API key comes from SSM Parameter Store; `prevent_destroy` on the bucket; logging/alarms on the Lambda."),
 "T11": ("Migrate: ALTER TABLE users ADD COLUMN tier text NOT NULL DEFAULT 'free', then backfill the computed tier for all 50M rows in the same migration.",
         "Migrate expand-contract: (1) add nullable `tier`; (2) backfill in batches of 5k with checkpointing (resumable/idempotent); (3) dual-write tier on new rows during backfill; (4) once 100% populated, add the NOT NULL constraint. Rollback: drop the column; no data loss on partial failure."),
 "T12": ("Replace the ranking algorithm with the new model and deploy it to 100% of production traffic in the next release.",
         "Ship the new ranking model behind a feature flag; canary to 5% of traffic, monitor the quality metric, ramp to 100% over a day; rollback = flip the flag off (no data cleanup); old and new coexist during the ramp."),
 "E5": ("Testing plan: add test_export() that calls export() and asserts it returns exactly the current output string (snapshot of today's behavior). Also a test that greps the source for the new function name to confirm it was added.",
        "Testing plan: a RED test that fails before the change exercising the failure path (export() on a malformed record raises ExportError); a boundary test (empty input -> empty file, oversized input -> chunked); the change is verified by behavior, not a source-grep or a snapshot of current output."),
 "COH": ("## Approach: add full unit + integration test coverage for every path.\n## Testing strategy: no automated tests are needed for this change; manual verification only.\n## Sequencing: ship step 3 (the consumer) before step 1 (the producer it depends on).",
         "## Approach: add unit + integration coverage for the changed paths.\n## Testing strategy: unit tests for the new validation, one integration test for the end-to-end path.\n## Sequencing: step 1 (producer) ships before step 3 (its consumer); deps declared accordingly."),
 "T9": ("Add a request counter: a module-global `count` incremented as `count = count + 1` on every request across the worker pool; read it for the /metrics endpoint.",
        "Add a request counter using an atomic counter (atomic.AddInt64 / a CAS loop / the metrics library's concurrency-safe counter), safe under the parallel worker pool; /metrics reads it atomically."),
 "A1": ("Introduce a generic PluginRegistry + AbstractHandler base class + a config-driven dispatch table to handle the one new webhook type we need today, so future handlers can plug in.",
        "Add the one webhook handler directly as a function alongside the existing handlers; if a third similar handler appears later, extract a shared helper then (Rule of Three)."),
}

def build_user(c):
    return (f"## Review criterion to apply (one verdict entry for id {c['id']})\n\n- [{c['id']}] {c['name']} — {c['scenario']}\n\n"
            "Call submit_review with exactly one entry for this criterion id.")

def run(cid, label, plan, rep):
    c = CRIT[cid]
    system = [{"type":"text","text":h.SYSTEM},
              {"type":"text","text":f"# Ticket plan under review\nTitle: (seeded case)\n\n## Plan\n{plan}"}]
    r = client.messages.create(model=MODEL, max_tokens=1500, system=system, tools=h.TOOL,
                               tool_choice={"type":"tool","name":"submit_review"},
                               messages=[{"role":"user","content":build_user(c)}])
    f = next((b.input.get("criteria",[]) for b in r.content if b.type=="tool_use"), [])
    f = f[0] if f and isinstance(f[0],dict) else {}
    return {"crit":cid,"label":label,"repeat":rep,"verdict":f.get("verdict"),"severity":f.get("severity"),
            "finding":(f.get("finding") or "")[:160]}

lock=threading.Lock()
def job(args):
    cid,label,plan,rep=args
    for a in range(3):
        try:
            rec=run(cid,label,plan,rep)
            with lock: open(OUT,'a').write(json.dumps(rec)+'\n')
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
    print(f"seeded-defect pilot: {len(CASES)} criteria x BAD/GOOD x 2 repeats = {len(jobs)} runs")
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(as_completed([ex.submit(job,j) for j in jobs]))
    print("DONE")
