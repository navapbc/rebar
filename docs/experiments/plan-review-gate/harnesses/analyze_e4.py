#!/usr/bin/env python3
"""Analyze the E4 generalization runs (suite + E5 A/B + trigger) on the non-DSO corpus."""
import json, os
from collections import Counter, defaultdict
import harness as h

TMP = h.TMP
def load(name):
    p = os.path.join(TMP, name)
    return [json.loads(l) for l in open(p) if l.strip()] if os.path.exists(p) else []

suite = [r for r in load("e4_suite.jsonl") if "ERROR" not in r]
e5ab = [r for r in load("e4_e5ab.jsonl") if "ERROR" not in r]
trig = load("e4_trigger.jsonl")
corpus = {t["id"]: t for t in json.load(open(os.path.join(TMP, "corpus_sample.json")))}

print("=" * 78)
print(f"E4 GENERALIZATION — non-DSO corpus ({len(set(r['id'] for r in suite))} tickets, "
      f"{sum(1 for t in corpus.values() if t['repo']=='rebar')} rebar + "
      f"{sum(1 for t in corpus.values() if t['repo']=='snap')} snap)")
print("=" * 78)

# ---- parse-hardening stats (item E) ----
allstatus = Counter()
for r in suite:
    for s in r.get("statuses", []):
        allstatus[s] += 1
print(f"\n[item E] single-turn parse statuses across all chunk calls: {dict(allstatus)}")
print("  (non-'ok' statuses that did not crash = the hardening working)")

# ---- per-criterion verdict distribution across the suite ----
# each suite row has findings[] across the ticket's chunks; aggregate by criterion
crit_v = defaultdict(Counter)          # criterion -> Counter(verdict)
crit_fail_tickets = defaultdict(set)   # criterion -> set(tickets with >=1 FAIL)
crit_seen_tickets = defaultdict(set)
crit_by_level = defaultdict(lambda: defaultdict(Counter))
for r in suite:
    for f in r["findings"]:
        if not isinstance(f, dict):
            continue
        cid = f.get("criterion_id")
        v = f.get("verdict")
        if not cid:
            continue
        crit_v[cid][v] += 1
        crit_seen_tickets[cid].add(r["id"])
        crit_by_level[cid][r["level"]][v] += 1
        if v == "FAIL":
            crit_fail_tickets[cid].add(r["id"])

print("\n[suite] per-criterion verdicts (across all tickets x 2 repeats). "
      "FAIL%=real-finding rate, AMB%=hedge rate:")
print(f"  {'crit':6} {'n':4} {'PASS':5} {'AMB':4} {'FAIL':5} {'FAIL%':6} {'AMB%':6}  fired-on-tickets")
rows = []
for cid in sorted(crit_v, key=lambda c: -(crit_v[c]['FAIL'])):
    c = crit_v[cid]; n = sum(c.values())
    failp = 100 * c['FAIL'] / n if n else 0
    ambp = 100 * c['AMBIGUOUS'] / n if n else 0
    rows.append((cid, n, c['PASS'], c['AMBIGUOUS'], c['FAIL'], failp, ambp, len(crit_fail_tickets[cid]), len(crit_seen_tickets[cid])))
for cid, n, p, a, fl, fp, ap, ft, st in rows:
    print(f"  {cid:6} {n:<4} {p:<5} {a:<4} {fl:<5} {fp:<5.0f} {ap:<5.0f}  {ft}/{st} tickets")

# ---- over-fire watch: criteria firing FAIL on a large fraction of DIVERSE tickets ----
print("\n[over-fire watch] criteria with FAIL on >40% of the tickets they ran on:")
for cid, n, p, a, fl, fp, ap, ft, st in rows:
    if st and ft / st > 0.4:
        print(f"  {cid}: FAIL on {ft}/{st} tickets ({100*ft/st:.0f}%)  — inspect for over-fire")

# ---- E5 v6 vs v7 A/B (item A validation) ----
print("\n" + "=" * 78)
print("[item A] E5 RETUNE A/B — v6 scenario vs v7 retuned scenario (same tickets)")
print("=" * 78)
for ver in ("v6", "v7"):
    vs = [r for r in e5ab if r["version"] == ver]
    vd = Counter(r["verdict"] for r in vs)
    nonpass = sum(1 for r in vs if r["verdict"] in ("FAIL", "AMBIGUOUS"))
    fails = sum(1 for r in vs if r["verdict"] == "FAIL")
    # per-ticket fired (any repeat non-PASS)
    byt = defaultdict(list)
    for r in vs: byt[r["id"]].append(r["verdict"])
    fired_tickets = sum(1 for t, vlist in byt.items() if any(v in ("FAIL", "AMBIGUOUS") for v in vlist))
    fail_tickets = sum(1 for t, vlist in byt.items() if any(v == "FAIL" for v in vlist))
    print(f"  {ver}: {dict(vd)}  | non-PASS runs={nonpass}/{len(vs)}  FAIL runs={fails}/{len(vs)}  "
          f"| tickets-fired={fired_tickets}/{len(byt)}  tickets-FAIL={fail_tickets}/{len(byt)}")
# side-by-side per ticket
print("\n  per-ticket E5 verdict (majority across repeats):  ticket  v6 -> v7")
byt6 = defaultdict(list); byt7 = defaultdict(list)
for r in e5ab:
    (byt6 if r["version"] == "v6" else byt7)[r["id"]].append(r["verdict"])
def maj(vl):
    return Counter(vl).most_common(1)[0][0] if vl else "-"
for tid in sorted(set(byt6) | set(byt7)):
    t = corpus.get(tid, {})
    flip = "  <-- changed" if maj(byt6[tid]) != maj(byt7.get(tid, [])) else ""
    print(f"    {tid[:9]} {t.get('repo',''):5} {t.get('type',''):5}  {maj(byt6[tid]):9} -> {maj(byt7.get(tid,[])):9}{flip}")

# ---- overlay trigger precision (esp T10/T11/T12) ----
print("\n" + "=" * 78)
print("[E3] OVERLAY-TRIGGER precision: deterministic vs LLM router (new overlays T10/T11/T12)")
print("=" * 78)
print("  (rebar 'migration' = bash->python strangler, NOT data-migration: det T11 should false-fire, LLM should not)")
for ov in ("T10", "T11", "T12", "T8", "T9", "T1"):
    det_fire = [r["id"] for r in trig if r.get("det", {}).get(ov)]
    llm_fire = [r["id"] for r in trig if r.get("llm", {}).get(ov)]
    print(f"  {ov}: det fires on {len(det_fire)} tickets, llm fires on {len(llm_fire)} tickets")
    # disagreements
    det_only = set(det_fire) - set(llm_fire)
    if det_only:
        print(f"       det-only (LLM says N/A — candidate false-fires): {[d[:9] for d in det_only]}")

print("\nANALYSIS DONE")
