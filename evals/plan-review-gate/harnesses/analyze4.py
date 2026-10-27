import json, os, statistics, itertools, collections
TMP = os.path.expanduser('~/.claude/jobs/3ef65161/tmp')
def load(p):
    return [json.loads(l) for l in open(p) if l.strip() and '"ERROR"' not in l] if os.path.exists(p) else []
def fm(fs): return {f['criterion_id']: f for f in fs if isinstance(f, dict) and 'criterion_id' in f}
def fail(f): return f and f.get('verdict') == 'FAIL'
def jac(a, b):
    if not a and not b: return 1.0
    return len(a & b) / len(a | b) if (a | b) else 1.0
def pct(v, p):
    v = sorted(v)
    if not v: return 0
    k = (len(v)-1)*p; f = int(k); c = min(f+1, len(v)-1); return v[f]+(v[c]-v[f])*(k-f)
SIN = (3.0, 15.0)  # sonnet in/out per M

b2 = load(os.path.join(TMP, 'runs2.jsonl'))            # bare-bones single-turn Sonnet (no cache)
e1 = load(os.path.join(TMP, 'exp1_substance.jsonl'))   # rich single-turn Sonnet (cached)
e2 = load(os.path.join(TMP, 'exp2_agentic.jsonl'))     # agentic tool-using Sonnet (cached)

print("="*78); print("EXP1 — SUBSTANTIVE (fully-specified) single-turn criteria vs bare-bones"); print("="*78)
# consistency vs N
def consistency(recs):
    rev = collections.defaultdict(dict)
    for r in recs:
        if r.get('N', 1) == 1: continue
        k = (r['ticket_key'], r['N'], r.get('partition', 0), r['repeat']); d = fm(r['findings'])
        for cid in r['ids_asked']: rev[k][cid] = fail(d.get(cid))
    out = {}
    for n in sorted({k[1] for k in rev}):
        js, ns = [], []
        for t in set(k[0] for k in rev if k[1] == n):
            sets = [set(c for c, fl in v.items() if fl) for k, v in rev.items() if k[0] == t and k[1] == n]
            pairs = list(itertools.combinations(sets, 2))
            if pairs:
                js.append(statistics.mean([jac(a, b) for a, b in pairs])); ns.append(statistics.pstdev([len(s) for s in sets]))
        out[n] = (statistics.mean(js), statistics.mean(ns))
    return out
cb, ce = consistency(b2), consistency(e1)
print("Consistency (Jaccard / count-stdev) vs N:")
print("  bare-bones: " + "  ".join(f"N={n}: j{cb[n][0]:.2f}/s{cb[n][1]:.1f}" for n in sorted(cb)))
print("  SUBSTANTIVE:" + "  ".join(f"N={n}: j{ce[n][0]:.2f}/s{ce[n][1]:.1f}" for n in sorted(ce)))

# cost vs N WITH caching (effective $/review and $/criterion)
def cost(recs, cached):
    grp = collections.defaultdict(lambda: {'in':0,'out':0,'cr':0,'cw':0})
    for r in recs:
        if r.get('N',1)==1: continue
        k=(r['ticket_key'],r['N'],r.get('partition',0),r['repeat']); g=grp[k]
        g['in']+=r['in_tok']; g['out']+=r['out_tok']; g['cr']+=r.get('cache_read',0); g['cw']+=r.get('cache_write',0)
    byN=collections.defaultdict(list)
    for k,g in grp.items():
        usd = g['in']/1e6*SIN[0] + g['cr']/1e6*SIN[0]*0.1 + g['cw']/1e6*SIN[0]*1.25 + g['out']/1e6*SIN[1]
        rawtok = g['in']+g['cr']+g['cw']+g['out']
        byN[k[1]].append((usd, rawtok))
    return byN
print("\nCost per review WITH prompt caching (substantive criteria, Sonnet):")
print(f"{'N':>3} {'$/review p50':>13} {'$/criterion':>12} {'tot_tok p50':>12} {'cache-hit note'}")
cs = cost(e1, True)
for n in sorted(cs):
    usds=[x[0] for x in cs[n]]; toks=[x[1] for x in cs[n]]
    print(f"{n:>3} {pct(usds,.5):>13.4f} {pct(usds,.5)/12:>12.5f} {pct(toks,.5):>12.0f}")
# vs bare-bones no-cache cost
cbn = cost(b2, False)
print("\nFor reference, bare-bones NO-cache $/review: " + "  ".join(f"N={n}: ${pct([x[0] for x in cbn[n]],.5):.3f}" for n in sorted(cbn)))

print("\n" + "="*78); print("EXP2 — AGENTIC (tool-using) tier: cost / latency / tool-calls per criterion"); print("="*78)
if e2:
    by = collections.defaultdict(list)
    for r in e2: by[r['criterion']].append(r)
    print(f"{'crit':>6} {'runs':>5} {'tool_calls p50':>14} {'iters p50':>10} {'lat_s p50':>10} {'$/run p50':>10} {'verdicts'}")
    for cid, rs in by.items():
        tc=[r['tool_calls'] for r in rs]; it=[r['iters'] for r in rs]; lat=[r['latency_s'] for r in rs]
        usds=[r['in_tok']/1e6*SIN[0]+r.get('cache_read',0)/1e6*SIN[0]*0.1+r.get('cache_write',0)/1e6*SIN[0]*1.25+r['out_tok']/1e6*SIN[1] for r in rs]
        verds=collections.Counter(f.get('verdict') for r in rs for f in r['findings'] if isinstance(f,dict))
        print(f"{cid:>6} {len(rs):>5} {pct(tc,.5):>14.0f} {pct(it,.5):>10.0f} {pct(lat,.5):>10.1f} {pct(usds,.5):>10.4f} {dict(verds)}")
    # contrast: same criteria as single-turn (no tools) cost — from a quick proxy: the rich single-turn N=1 weight
    allusd=[r['in_tok']/1e6*SIN[0]+r.get('cache_read',0)/1e6*SIN[0]*0.1+r.get('cache_write',0)/1e6*SIN[0]*1.25+r['out_tok']/1e6*SIN[1] for r in e2]
    print(f"\n  agentic mean $/criterion-run = ${statistics.mean(allusd):.4f}  (vs single-turn ~$0.001-0.002/criterion)")
    print(f"  -> agentic is ~{statistics.mean(allusd)/0.0015:.0f}x the cost of a single-turn criterion, and one full agent loop per criterion")
    # show a sample grounded finding
    print("\n  Sample grounded findings (tools let these flip AMBIGUOUS->concrete):")
    for r in e2[:6]:
        for f in r['findings']:
            if isinstance(f,dict) and f.get('verdict') in ('FAIL','PASS','AMBIGUOUS'):
                print(f"    [{r['ticket_key']}/{f.get('criterion_id')}] {f.get('verdict')} {f.get('severity','')}: {(f.get('finding') or '')[:150]}")
                break
