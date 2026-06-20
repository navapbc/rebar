import json, os, collections
TMP = os.path.expanduser('~/.claude/jobs/3ef65161/tmp')
def load(p):
    return [json.loads(l) for l in open(os.path.join(TMP, p)) if l.strip()] if os.path.exists(os.path.join(TMP, p)) else []
def fails(findings):
    return [f for f in findings if isinstance(f, dict) and f.get('verdict') in ('FAIL',)]
def nonpass(findings):
    return [f for f in findings if isinstance(f, dict) and f.get('verdict') in ('FAIL', 'AMBIGUOUS')]

CHILD_NAMES = {"5fd2-a7c2-0aec-48fa": "EPIC", "2f3c-682a-2105-4b8f": "registry", "8e3e-50ba-765c-4d2f": "layer1",
 "2632-5741-090e-46c3": "layer2", "6d7b-41ef-f869-40dd": "overlays", "bfa8-aadd-6739-4904": "attestation",
 "cb28-f531-66f2-49cb": "claim", "f20a-865f-6cb3-49e4": "chunking", "fd92-4b4d-b24b-41da": "sidecar", "a473-8af4-a493-4e0e": "docs"}

print("=" * 80); print("STREAM A — epic + 9 children (single-turn suite). Findings per ticket:"); print("=" * 80)
A = load('r4_A.jsonl')
byt = collections.defaultdict(list)
for r in A:
    byt[r['ticket']] += r['findings']
for tid in ["5fd2-a7c2-0aec-48fa", "2f3c-682a-2105-4b8f", "8e3e-50ba-765c-4d2f", "2632-5741-090e-46c3", "6d7b-41ef-f869-40dd", "bfa8-aadd-6739-4904", "cb28-f531-66f2-49cb", "f20a-865f-6cb3-49e4", "fd92-4b4d-b24b-41da", "a473-8af4-a493-4e0e"]:
    fs = byt.get(tid, [])
    nf = nonpass(fs)
    tags = [f"{f['criterion_id']}:{f['verdict'][0]}{('/' + f.get('severity', '')[:3]) if f.get('severity') not in (None, 'none') else ''}" for f in nf]
    print(f"\n{CHILD_NAMES.get(tid, tid):12} ({tid[:9]}): {len(nf)} findings  {tags}")
    for f in nf:
        if f.get('verdict') == 'FAIL':
            print(f"   [{f['criterion_id']}/{f.get('severity')}] {(f.get('finding') or '')[:160]}")

print("\n" + "=" * 80); print("STREAM B — DSO sample (single-turn suite incl EXP). Findings + EXP behavior:"); print("=" * 80)
B = load('r4_B.jsonl')
bytB = collections.defaultdict(lambda: {'f': [], 'type': '', 'bc': False})
for r in B:
    bytB[r['ticket']]['f'] += r['findings']; bytB[r['ticket']]['type'] = r['type']; bytB[r['ticket']]['bc'] = r['bc']
print(f"{'ticket':22} {'type':6} {'#find':>5}  findings")
for tid, d in bytB.items():
    nf = nonpass(d['f'])
    exp = next((f for f in d['f'] if isinstance(f, dict) and f.get('criterion_id') == 'EXP'), None)
    exptag = f"EXP={exp.get('verdict')}" if exp else "EXP=?"
    tags = [f"{f['criterion_id']}:{f['verdict'][0]}" for f in nf]
    print(f"{tid:22} {d['type']:6} {len(nf):>5}  {exptag:13} {tags}")
# EXP validation
print("\nEXP criterion behavior (should FIRE on complex/novel-without-experiment, SUPPRESS where experimentation present):")
for tid, d in bytB.items():
    exp = next((f for f in d['f'] if isinstance(f, dict) and f.get('criterion_id') == 'EXP'), None)
    if exp and exp.get('verdict') != 'PASS':
        print(f"   FIRES on {tid} ({d['type']}, bc={d['bc']}): {(exp.get('finding') or '')[:170]}")

print("\n" + "=" * 80); print("STREAM C — overlay triggering: deterministic vs LLM router agreement"); print("=" * 80)
C = load('r4_C.jsonl')
OVS = ['T1', 'T5a', 'T5b', 'T5c', 'T5d', 'T5e', 'T6', 'T7', 'T8', 'T9']
print(f"{'overlay':>7} {'det_fires':>9} {'llm_fires':>9} {'agree':>6} {'det-only(FP?)':>13} {'llm-only(miss?)':>15}")
for ov in OVS:
    detF = sum(1 for r in C if r['det'].get(ov))
    llmF = sum(1 for r in C if r['llm'].get(ov))
    agree = sum(1 for r in C if r['det'].get(ov) == r['llm'].get(ov))
    detonly = sum(1 for r in C if r['det'].get(ov) and not r['llm'].get(ov))
    llmonly = sum(1 for r in C if r['llm'].get(ov) and not r['det'].get(ov))
    n = len(C)
    verdict = "DETERMINISTIC-ok" if detonly <= 1 and detF > 0 else ("LLM-route (det noisy)" if detonly >= 3 else "LLM-route")
    print(f"{ov:>7} {detF:>9} {llmF:>9} {agree}/{n:>3} {detonly:>13} {llmonly:>15}   {verdict}")

print("\n" + "=" * 80); print("STREAM D — PIL: our finding volume vs DSO recorded findings"); print("=" * 80)
D = load('r4_D.jsonl')
t8 = json.load(open(os.path.join(TMP, 'r4_T8.json'))) if os.path.exists(os.path.join(TMP, 'r4_T8.json')) else {}
for r in D:
    ours = nonpass(r['our_singleturn'])
    tids = r['ticket']
    t8r = t8.get(tids, {})
    t8f = [f for f in t8r.get('findings', []) if isinstance(f, dict) and f.get('verdict') == 'FAIL']
    print(f"\n{tids}: DSO recorded {r['dso_findings']} findings | OUR single-turn {len(ours)} | OUR T8 probe {'FAIL (structural gaps found)' if t8f else ('PASS' if t8r else 'n/a')}")
    print(f"   our single-turn: {[f['criterion_id'] + ':' + f['verdict'][0] for f in ours]}")
