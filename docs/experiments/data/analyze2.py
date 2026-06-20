import json, os, statistics, itertools, collections

TMP = os.path.expanduser('~/.claude/jobs/3ef65161/tmp')
CRITERIA = [c['id'] for c in json.load(open(os.path.join(TMP, 'criteria.json')))]
TIER_ORDER = ['trivial', 'moderate', 'small_epic', 'complex_leaf', 'container_epic', 'dogfood_epic']

def load(path):
    out = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if 'ERROR' not in r:
            out.append(r)
    return out

b1 = load(os.path.join(TMP, 'runs.jsonl'))    # batch1 (has chunk_size, repeat)
b2 = load(os.path.join(TMP, 'runs2.jsonl'))   # batch2 (has N, partition, repeat)

def fmap(findings):
    d = {}
    for f in findings:
        if isinstance(f, dict) and 'criterion_id' in f:
            d[f['criterion_id']] = f
    return d

def is_fail(f):
    return f is not None and f.get('verdict') == 'FAIL'

# ---------- SOLO baseline per (ticket, criterion): flag-rate at N=1 ----------
solo = collections.defaultdict(lambda: collections.defaultdict(list))  # ticket -> cid -> [0/1...]
for r in b1:
    if r.get('chunk_size') == 1:
        d = fmap(r['findings'])
        for cid in r['ids_asked']:
            solo[r['ticket_key']][cid].append(1 if is_fail(d.get(cid)) else 0)
for r in b2:
    if r.get('N') == 1:
        d = fmap(r['findings'])
        for cid in r['ids_asked']:
            solo[r['ticket_key']][cid].append(1 if is_fail(d.get(cid)) else 0)

def solo_rate(ticket, cid):
    v = solo[ticket][cid]
    return sum(v) / len(v) if v else None

# ---------- batch2: per-criterion flag-rate at each N (pooled over groupings/partitions/repeats) ----------
# key: (ticket, N, cid) -> list of 0/1
atN = collections.defaultdict(list)
for r in b2:
    if r.get('N') == 1:
        continue
    d = fmap(r['findings'])
    for cid in r['ids_asked']:
        atN[(r['ticket_key'], r['N'], cid)].append(1 if is_fail(d.get(cid)) else 0)

tickets = sorted(solo.keys(), key=lambda t: TIER_ORDER.index(t) if t in TIER_ORDER else 99)
N_VALUES = sorted({r['N'] for r in b2 if r.get('N', 1) > 1})

print(f"batch2 records={len(b2)}  tickets={tickets}  N values={N_VALUES}")

# ---------- DILUTION: pooled per-criterion solo-rate vs rate-at-N (across all tickets) ----------
print("\n" + "=" * 80)
print("PER-CRITERION DILUTION: flag-rate solo (N=1) vs batched at N (pooled across tickets)")
print("  '<' = flagged LESS when batched (lost); '>' = flagged MORE when batched")
print("=" * 80)
header = "crit   solo   " + "  ".join(f"N={n:<4}" for n in N_VALUES)
print(header)
for cid in CRITERIA:
    srates = [solo_rate(t, cid) for t in tickets]
    srates = [x for x in srates if x is not None]
    sbar = statistics.mean(srates) if srates else 0
    cells = []
    for n in N_VALUES:
        vals = []
        for t in tickets:
            v = atN.get((t, n, cid), [])
            if v:
                vals.append(sum(v) / len(v))
        cells.append(statistics.mean(vals) if vals else float('nan'))
    row = f"{cid:5} {sbar:5.2f}   " + "  ".join(f"{c:5.2f} " for c in cells)
    print(row)

# ---------- CONSISTENCY of the full review vs N (assemble all-12 per partition,repeat) ----------
print("\n" + "=" * 80)
print("REVIEW CONSISTENCY vs criteria-per-turn N  (jaccard of FAIL-sets across review instances)")
print("  a 'review instance' = one (partition,repeat) covering all 12 criteria")
print("=" * 80)
# assemble: (ticket, N, partition, repeat) -> {cid: fail?}
rev = collections.defaultdict(dict)
for r in b2:
    if r.get('N') == 1:
        continue
    key = (r['ticket_key'], r['N'], r['partition'], r['repeat'])
    d = fmap(r['findings'])
    for cid in r['ids_asked']:
        rev[key][cid] = is_fail(d.get(cid))

def jac(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if (a | b) else 1.0

pool_by_N = collections.defaultdict(list)  # N -> list of jaccard values (pooled)
pool_nstd_by_N = collections.defaultdict(list)
print(f"{'ticket':>16} " + " ".join(f"N={n:>2}".rjust(13) for n in N_VALUES))
for t in tickets:
    cells = []
    for n in N_VALUES:
        insts = [(k, v) for k, v in rev.items() if k[0] == t and k[1] == n]
        sets = [set(cid for cid, fl in v.items() if fl) for _, v in insts]
        counts = [len(s) for s in sets]
        pairs = list(itertools.combinations(sets, 2))
        mj = statistics.mean([jac(a, b) for a, b in pairs]) if pairs else float('nan')
        nstd = statistics.pstdev(counts) if len(counts) > 1 else 0.0
        if pairs:
            pool_by_N[n].append(mj)
            pool_nstd_by_N[n].append(nstd)
        cells.append(f"j{mj:.2f}/s{nstd:.1f}")
    print(f"{t:>16} " + " ".join(c.rjust(13) for c in cells))
print("-" * 80)
print(f"{'POOLED mean':>16} " + " ".join(
    f"j{statistics.mean(pool_by_N[n]):.2f}/s{statistics.mean(pool_nstd_by_N[n]):.1f}".rjust(13) for n in N_VALUES))

# ---------- AGREEMENT WITH SOLO vs N (does batched verdict match solo majority?) ----------
print("\n" + "=" * 80)
print("AGREEMENT-WITH-SOLO vs N  (fraction of criteria whose batched flag-rate matches solo majority)")
print("=" * 80)
print(f"{'N':>4} {'agree_FAIL_recall':>18} {'agree_PASS':>12} {'overall':>9}")
for n in N_VALUES:
    rec_hits = rec_tot = pass_hits = pass_tot = 0
    for t in tickets:
        for cid in CRITERIA:
            sr = solo_rate(t, cid)
            if sr is None:
                continue
            v = atN.get((t, n, cid), [])
            if not v:
                continue
            br = sum(v) / len(v)
            solo_pos = sr >= 0.5
            bat_pos = br >= 0.5
            if solo_pos:
                rec_tot += 1; rec_hits += (1 if bat_pos else 0)
            else:
                pass_tot += 1; pass_hits += (1 if not bat_pos else 0)
    recall = rec_hits / rec_tot if rec_tot else float('nan')
    spec = pass_hits / pass_tot if pass_tot else float('nan')
    overall = (rec_hits + pass_hits) / (rec_tot + pass_tot)
    print(f"{n:>4} {recall:>18.2f} {spec:>12.2f} {overall:>9.2f}")

# ---------- COST / LATENCY per call and per full-review vs N ----------
print("\n" + "=" * 80)
print("COST / LATENCY vs N")
print("=" * 80)
def pct(vals, p):
    vals = sorted(vals)
    if not vals:
        return 0
    k = (len(vals) - 1) * p
    f = int(k); c = min(f + 1, len(vals) - 1)
    return vals[f] + (vals[c] - vals[f]) * (k - f)
print(f"{'N':>4} {'call_tok_p50':>12} {'call_lat_p50':>12} {'call_lat_p95':>12} {'chunks/review':>14} "
      f"{'review_tok_p50':>15} {'review_lat_par_p50':>18}")
for n in N_VALUES:
    rows = [r for r in b2 if r.get('N') == n]
    ctok = [r['in_tok'] + r['out_tok'] for r in rows]
    clat = [r['latency_s'] for r in rows]
    nch = -(-12 // n)
    # review-level: group by (ticket,partition,repeat)
    grp = collections.defaultdict(lambda: {'tok': 0, 'lat_sum': 0, 'lat_max': 0})
    for r in rows:
        g = grp[(r['ticket_key'], r['partition'], r['repeat'])]
        g['tok'] += r['in_tok'] + r['out_tok']; g['lat_sum'] += r['latency_s']; g['lat_max'] = max(g['lat_max'], r['latency_s'])
    rtok = [g['tok'] for g in grp.values()]
    rpar = [g['lat_max'] for g in grp.values()]
    print(f"{n:>4} {pct(ctok,.5):>12.0f} {pct(clat,.5):>12.1f} {pct(clat,.95):>12.1f} {nch:>14} "
          f"{pct(rtok,.5):>15.0f} {pct(rpar,.5):>18.1f}")

tin = sum(r['in_tok'] for r in b2); tout = sum(r['out_tok'] for r in b2)
print(f"\nbatch2 totals: in={tin} out={tout} approx_cost_sonnet=${tin/1e6*3+tout/1e6*15:.2f}")
