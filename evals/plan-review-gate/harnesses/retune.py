import json, os, subprocess
import round4 as r4, harness as h

TMP = h.TMP
REBAR = "/Users/joeoakhart/rebar"
DSO = os.path.expanduser("~/digital-service-orchestra")
CID = r4.CID

# --- LEVEL-AWARE routing: which criteria apply at which altitude ---
LEAF_ONLY = {"E6", "T5a", "T5b", "T5c"}     # leaf/implementation-grain — suppress above task level
# all-level judgment criteria we keep at epic/story altitude:
ALL_LEVEL = ["F1", "F4", "E1", "E2", "E3", "E5", "G5", "T5e", "EXP"]

def is_test_task(plan):
    p = plan.lower()
    return ("testing mode" in p and ("red" in p or "green" in p)) or "red task" in p or p.count("unit test") >= 1 and "test approach" in p

def criteria_for(level, plan):
    if level == "leaf":
        ids = [c['id'] for c in r4.CRIT]                 # full set
    else:  # epic / story
        ids = ALL_LEVEL[:]                               # drop leaf-grain criteria
    # type rule: suppress E5 for test tasks (the task IS the test)
    if is_test_task(plan) and "E5" in ids:
        ids = [i for i in ids if i != "E5"]
    return [CID[i] for i in ids]

def review(title, plan, level):
    crit = criteria_for(level, plan)
    # facet-pack into chunks of 6
    chunks = [crit[i:i+6] for i in range(0, len(crit), 6)]
    allf = []
    for ch in chunks:
        r = r4.single_turn(title, plan, ch)
        allf += r['findings']
    return allf

def nonpass(fs):
    return [f for f in fs if isinstance(f, dict) and f.get('verdict') in ('FAIL', 'AMBIGUOUS')]
def fails(fs):
    return [f for f in fs if isinstance(f, dict) and f.get('verdict') == 'FAIL']

# children are STORIES; re-review with level=story and compare to the round4 (full-leaf) counts
CHILDREN = {"2f3c-682a-2105-4b8f": "registry", "8e3e-50ba-765c-4d2f": "layer1", "2632-5741-090e-46c3": "layer2",
            "6d7b-41ef-f869-40dd": "overlays", "bfa8-aadd-6739-4904": "attestation", "cb28-f531-66f2-49cb": "claim",
            "f20a-865f-6cb3-49e4": "chunking", "fd92-4b4d-b24b-41da": "sidecar", "a473-8af4-a493-4e0e": "docs"}
DSO_STORY = {"908b-11e4-dd14-4ea3": "rename-wf", "b1de-627f": "boundary-hook"}
DSO_TEST = {"7b48-b23e-c825-4853": "RED-differ", "5615-6b6c-d496-4b9d": "RED-clean_label"}

if __name__ == "__main__":
    out = {}
    print("=== LEVEL-AWARE re-run (story altitude: leaf-grain criteria suppressed) ===\n")
    for tid, name in {**CHILDREN}.items():
        t, p = r4.ticket_plan(tid, REBAR)
        fs = review(t, p, "story")
        nf = nonpass(fs)
        out[tid] = {"n_nonpass": len(nf), "fails": len(fails(fs)),
                    "tags": [f"{f['criterion_id']}:{f['verdict'][0]}" for f in nf],
                    "real": [(f['criterion_id'], f.get('severity'), (f.get('finding') or '')[:110]) for f in fails(fs)]}
        print(f"{name:12} ({tid[:9]}): {len(nf)} findings (was ~7-10 at full-leaf)  {out[tid]['tags']}")
    print("\n--- DSO stories (level=story) + DSO test-tasks (level=leaf, E5 type-suppressed) ---")
    for tid, name in {**DSO_STORY}.items():
        t, p = r4.ticket_plan(tid, DSO)
        nf = nonpass(review(t, p, "story"))
        print(f"  {name:14} ({tid[:9]}) story: {len(nf)} findings  {[f['criterion_id']+':'+f['verdict'][0] for f in nf]}")
        out[tid] = {"n_nonpass": len(nf)}
    for tid, name in {**DSO_TEST}.items():
        t, p = r4.ticket_plan(tid, DSO)
        nf = nonpass(review(t, p, "leaf"))
        print(f"  {name:14} ({tid[:9]}) leaf(test): {len(nf)} findings  {[f['criterion_id']+':'+f['verdict'][0] for f in nf]}  (E5 suppressed: {r4.CID and is_test_task(p)})")
        out[tid] = {"n_nonpass": len(nf)}
    json.dump(out, open(os.path.join(TMP, "retune_out.json"), "w"), indent=1)
    print("\nsaved retune_out.json")
