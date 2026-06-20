import json, os, statistics, itertools, collections

TMP = os.path.expanduser('~/.claude/jobs/3ef65161/tmp')
CRITERIA = [c['id'] for c in json.load(open(os.path.join(TMP, 'criteria.json')))]
PRICE = {'claude-opus-4-8': (5.0, 25.0), 'claude-sonnet-4-6': (3.0, 15.0)}
TIER = ['trivial', 'moderate', 'small_epic', 'complex_leaf', 'container_epic', 'dogfood_epic']

def load(p):
    out = []
    if not os.path.exists(p):
        return out
    for line in open(p):
        line = line.strip()
        if line and '"ERROR"' not in line:
            out.append(json.loads(line))
    return out

b2 = load(os.path.join(TMP, 'runs2.jsonl'))       # Sonnet, random groupings, N>1 (+N=1 small_epic)
b3 = load(os.path.join(TMP, 'runs3_opus.jsonl'))  # Opus, random groupings
b4 = load(os.path.join(TMP, 'runs4_group.jsonl')) # Sonnet, coherent/anti groupings

def fmap(fs):
    return {f['criterion_id']: f for f in fs if isinstance(f, dict) and 'criterion_id' in f}
def is_fail(f):
    return f is not None and f.get('verdict') == 'FAIL'
def jac(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if (a | b) else 1.0
def pct(v, p):
    v = sorted(v)
    if not v:
        return 0
    k = (len(v) - 1) * p; f = int(k); c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)

# ============ 1. CONSISTENCY vs N: Opus vs Sonnet ============
def consistency_by_N(recs, key_fields):
    # assemble review instances -> {cid: fail}
    rev = collections.defaultdict(dict)
    for r in recs:
        if r.get('N', 1) == 1:
            continue
        key = tuple(r[k] for k in key_fields)
        d = fmap(r['findings'])
        for cid in r['ids_asked']:
            rev[key][cid] = is_fail(d.get(cid))
    # pool by N (key_fields[1] is N)
    byN = collections.defaultdict(list)
    for key, v in rev.items():
        byN[key[1]].append(set(c for c, fl in v.items() if fl))
    return rev, byN

print("=" * 80)
print("1. REVIEW CONSISTENCY vs criteria-per-turn N — OPUS vs SONNET")
print("   pooled mean pairwise Jaccard of FAIL-sets across same-ticket review instances")
print("=" * 80)
for label, recs in [('SONNET (b2)', b2), ('OPUS   (b3)', b3)]:
    rev = collections.defaultdict(dict)
    for r in recs:
        if r.get('N', 1) == 1:
            continue
        key = (r['ticket_key'], r['N'], r.get('partition', 0), r['repeat'])
        d = fmap(r['findings'])
        for cid in r['ids_asked']:
            rev[key][cid] = is_fail(d.get(cid))
    Ns = sorted({k[1] for k in rev})
    cells = []
    for n in Ns:
        # group instances per ticket, jaccard within ticket, pool
        js, nstds = [], []
        for t in set(k[0] for k in rev if k[1] == n):
            sets = [set(c for c, fl in v.items() if fl) for k, v in rev.items() if k[0] == t and k[1] == n]
            counts = [len(s) for s in sets]
            pairs = list(itertools.combinations(sets, 2))
            if pairs:
                js.append(statistics.mean([jac(a, b) for a, b in pairs]))
                nstds.append(statistics.pstdev(counts))
        cells.append((n, statistics.mean(js), statistics.mean(nstds)))
    print(f"\n{label}:  " + "   ".join(f"N={n}: j{j:.2f}/s{s:.1f}" for n, j, s in cells))

# ============ 2. COST PER CRITERION vs N: Opus vs Sonnet ============
print("\n" + "=" * 80)
print("2. COST & $/CRITERION vs N  (real token usage x model pricing)")
print("=" * 80)
def review_costs(recs, model):
    pin, pout = PRICE[model]
    grp = collections.defaultdict(lambda: {'in': 0, 'out': 0})
    for r in recs:
        if r.get('N', 1) == 1:
            continue
        key = (r['ticket_key'], r['N'], r.get('partition', 0), r['repeat'])
        grp[key]['in'] += r['in_tok']; grp[key]['out'] += r['out_tok']
    byN = collections.defaultdict(list)
    for key, g in grp.items():
        usd = g['in'] / 1e6 * pin + g['out'] / 1e6 * pout
        byN[key[1]].append((g['in'] + g['out'], usd))
    return byN
print(f"{'model':>8} {'N':>3} {'review_tok_p50':>14} {'$/review_p50':>13} {'$/criterion':>12}  {'$/crit_p95':>11}")
for model, recs in [('sonnet', b2), ('opus', b3)]:
    full = 'claude-' + ('sonnet-4-6' if model == 'sonnet' else 'opus-4-8')
    byN = review_costs(recs, full)
    for n in sorted(byN):
        toks = [x[0] for x in byN[n]]; usds = [x[1] for x in byN[n]]
        cpc50 = pct(usds, .5) / 12; cpc95 = pct(usds, .95) / 12
        print(f"{model:>8} {n:>3} {pct(toks,.5):>14.0f} {pct(usds,.5):>13.4f} {cpc50:>12.5f}  {cpc95:>11.5f}")

# ============ 3. GROUPING: coherent vs random vs anti (Sonnet) ============
print("\n" + "=" * 80)
print("3. INTENTIONAL GROUPING (Sonnet): coherent vs random(b2) vs anti — consistency at N=4,6")
print("   jaccard across repeats; higher = more reliable. Hypothesis: coherent > random > anti")
print("=" * 80)
def assemble(recs, strat_key=None):
    rev = collections.defaultdict(dict)
    for r in recs:
        if r.get('N', 1) == 1:
            continue
        if strat_key and r.get('strategy') != strat_key:
            continue
        # instance id: random uses (partition,repeat); coherent/anti use (repeat) only
        inst = (r['ticket_key'], r['N'], r.get('strategy', 'random'), r.get('partition', 0), r['repeat'])
        d = fmap(r['findings'])
        for cid in r['ids_asked']:
            rev[inst][cid] = is_fail(d.get(cid))
    return rev

def pooled_jac(rev, n):
    js, nstds = [], []
    tickets = set(k[0] for k in rev if k[1] == n)
    for t in tickets:
        sets = [set(c for c, fl in v.items() if fl) for k, v in rev.items() if k[0] == t and k[1] == n]
        counts = [len(s) for s in sets]
        pairs = list(itertools.combinations(sets, 2))
        if pairs:
            js.append(statistics.mean([jac(a, b) for a, b in pairs]))
            nstds.append(statistics.pstdev(counts))
    return (statistics.mean(js) if js else float('nan'),
            statistics.mean(nstds) if nstds else float('nan'))

rev_rand = assemble(b2)
rev_coh = assemble(b4, 'coherent')
rev_anti = assemble(b4, 'anti')
print(f"{'N':>3} {'coherent':>16} {'random(b2)':>16} {'anti':>16}")
for n in [4, 6]:
    jc, sc = pooled_jac(rev_coh, n)
    jr, sr = pooled_jac(rev_rand, n)
    ja, sa = pooled_jac(rev_anti, n)
    print(f"{n:>3} {f'j{jc:.2f}/s{sc:.1f}':>16} {f'j{jr:.2f}/s{sr:.1f}':>16} {f'j{ja:.2f}/s{sa:.1f}':>16}")
# reference: random at N=2,3? we have N=2 random
j2, s2 = pooled_jac(rev_rand, 2)
print(f"\n  reference random N=2: j{j2:.2f}/s{s2:.1f}  (does coherent@6 reach random@2-4 reliability?)")

# per-criterion FAIL-rate spread: do coherent groupings reduce over-flagging vs anti?
def flagrate(rev, n):
    cnt = collections.Counter(); tot = collections.Counter()
    for k, v in rev.items():
        if k[1] != n:
            continue
        for c, fl in v.items():
            tot[c] += 1; cnt[c] += 1 if fl else 0
    return {c: cnt[c] / tot[c] for c in tot if tot[c]}
print("\n  Mean FAIL-rate across all 12 criteria (proxy for over-flagging; lower+stable = less noise):")
for n in [4, 6]:
    for name, rev in [('coherent', rev_coh), ('random', rev_rand), ('anti', rev_anti)]:
        fr = flagrate(rev, n)
        m = statistics.mean(fr.values()) if fr else float('nan')
        print(f"    N={n} {name:>9}: mean flag-rate {m:.2f}")

tot = lambda recs, m: (sum(r['in_tok'] for r in recs) / 1e6 * PRICE[m][0] + sum(r['out_tok'] for r in recs) / 1e6 * PRICE[m][1])
print(f"\nSpend: opus(b3) ${tot(b3,'claude-opus-4-8'):.2f}  grouping(b4) ${tot(b4,'claude-sonnet-4-6'):.2f}")
