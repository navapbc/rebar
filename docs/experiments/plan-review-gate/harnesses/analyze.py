import json, os, statistics, collections, itertools

TMP = os.path.expanduser('~/.claude/jobs/3ef65161/tmp')
CRITERIA = [c['id'] for c in json.load(open(os.path.join(TMP, 'criteria.json')))]
SIZES = [1, 3, 6, 12]
TIER_ORDER = ['trivial', 'moderate', 'complex_leaf', 'container_epic', 'dogfood_epic']

recs = []
errors = []
for line in open(os.path.join(TMP, 'runs.jsonl')):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    if 'ERROR' in r:
        errors.append(r)
    else:
        recs.append(r)

# assemble RUNS: (ticket_key, chunk_size, repeat) -> {criterion_id: finding}
runs = {}
runmeta = {}
for r in recs:
    key = (r['ticket_key'], r['chunk_size'], r['repeat'])
    d = runs.setdefault(key, {})
    m = runmeta.setdefault(key, {'in': 0, 'out': 0, 'lat_sum': 0.0, 'lat_max': 0.0, 'calls': 0, 'type': r['ticket_type']})
    m['in'] += r['in_tok']; m['out'] += r['out_tok']
    m['lat_sum'] += r['latency_s']; m['lat_max'] = max(m['lat_max'], r['latency_s']); m['calls'] += 1
    for f in r['findings']:
        if isinstance(f, dict) and 'criterion_id' in f:
            d[f['criterion_id']] = f

def is_flag(f):
    return f is not None and f.get('verdict') == 'FAIL'

def flagged_set(run):
    return set(cid for cid, f in run.items() if is_flag(f))

def ambig_set(run):
    return set(cid for cid, f in run.items() if f and f.get('verdict') == 'AMBIGUOUS')

print(f"records={len(recs)} errors={len(errors)} assembled-runs={len(runs)}")
tickets = sorted({k[0] for k in runs}, key=lambda t: TIER_ORDER.index(t) if t in TIER_ORDER else 99)

# ---------- per-ticket recall / noise vs baseline (size 1) ----------
print("\n" + "=" * 78)
print("RECALL & NOISE vs per-criterion baseline (chunk size 1)")
print("=" * 78)

def majority_flagged(ticket, size, cid, thr=0.5):
    reps = [runs.get((ticket, size, rp), {}) for rp in range(1, 6)]
    reps = [r for r in reps if r]
    if not reps:
        return None
    rate = sum(1 for r in reps if is_flag(r.get(cid))) / len(reps)
    return rate

summary_rows = []
for ticket in tickets:
    # baseline positives: criteria flagged in >=50% of size-1 repeats
    base_rates = {cid: majority_flagged(ticket, 1, cid) for cid in CRITERIA}
    base_pos = [cid for cid in CRITERIA if (base_rates[cid] or 0) >= 0.5]
    print(f"\n--- {ticket} --- baseline-positive criteria ({len(base_pos)}): {base_pos}")
    for size in SIZES:
        if size == 1:
            continue
        rates = {cid: majority_flagged(ticket, size, cid) for cid in CRITERIA}
        # recall: of baseline-positive, how many also majority-flagged here
        recalled = [cid for cid in base_pos if (rates[cid] or 0) >= 0.5]
        recall = len(recalled) / len(base_pos) if base_pos else 1.0
        # soft recall: mean flag-rate at this size over baseline-positive criteria
        soft = statistics.mean([rates[cid] or 0 for cid in base_pos]) if base_pos else 1.0
        # noise: criteria flagged (>=50%) here that were NOT baseline-positive
        noise = [cid for cid in CRITERIA if (rates[cid] or 0) >= 0.5 and cid not in base_pos]
        missed = [cid for cid in base_pos if cid not in recalled]
        print(f"   size {size:2}: recall {recall:.0%} ({len(recalled)}/{len(base_pos)})  soft {soft:.0%}  "
              f"missed={missed}  noise+={noise}")
        summary_rows.append((ticket, size, recall, soft, len(noise)))

# ---------- consistency across 5 repeats ----------
print("\n" + "=" * 78)
print("CONSISTENCY across the 5 repeats (per ticket x chunk size)")
print("  jaccard = mean pairwise Jaccard of FAIL-criteria sets; nstd = stdev of #findings")
print("=" * 78)
for ticket in tickets:
    print(f"\n--- {ticket} ---")
    for size in SIZES:
        sets = [flagged_set(runs[(ticket, size, rp)]) for rp in range(1, 6) if (ticket, size, rp) in runs]
        counts = [len(s) for s in sets]
        pairs = list(itertools.combinations(sets, 2))
        def jac(a, b):
            if not a and not b:
                return 1.0
            return len(a & b) / len(a | b) if (a | b) else 1.0
        mj = statistics.mean([jac(a, b) for a, b in pairs]) if pairs else 1.0
        nstd = statistics.pstdev(counts) if len(counts) > 1 else 0.0
        print(f"   size {size:2}: findings/run {counts}  mean {statistics.mean(counts):.1f}  jaccard {mj:.2f}  nstd {nstd:.2f}")

# ---------- cost & latency per run, by chunk size and tier ----------
print("\n" + "=" * 78)
print("COST & LATENCY per full review (a 'run' = all 12 criteria over its chunks)")
print("=" * 78)
def pct(vals, p):
    vals = sorted(vals)
    if not vals:
        return 0
    k = (len(vals) - 1) * p
    f = int(k); c = min(f + 1, len(vals) - 1)
    return vals[f] + (vals[c] - vals[f]) * (k - f)

print("\nBy chunk size (across all tickets):")
print(f"{'size':>5} {'tok_p50':>8} {'tok_p95':>8} {'lat_seq_p50':>11} {'lat_seq_p95':>11} {'lat_par_p50':>11} {'lat_par_p95':>11} {'calls/run':>9}")
for size in SIZES:
    ms = [m for k, m in runmeta.items() if k[1] == size]
    toks = [m['in'] + m['out'] for m in ms]
    seq = [m['lat_sum'] for m in ms]
    par = [m['lat_max'] for m in ms]
    callspr = [m['calls'] for m in ms]
    print(f"{size:>5} {pct(toks,.5):>8.0f} {pct(toks,.95):>8.0f} {pct(seq,.5):>11.1f} {pct(seq,.95):>11.1f} "
          f"{pct(par,.5):>11.1f} {pct(par,.95):>11.1f} {statistics.mean(callspr):>9.1f}")

print("\nBy tier (across all chunk sizes):")
print(f"{'tier':>16} {'tok_p50':>8} {'tok_p95':>8} {'lat_seq_p50':>11} {'lat_seq_p95':>11}")
for ticket in tickets:
    ms = [m for k, m in runmeta.items() if k[0] == ticket]
    toks = [m['in'] + m['out'] for m in ms]
    seq = [m['lat_sum'] for m in ms]
    print(f"{ticket:>16} {pct(toks,.5):>8.0f} {pct(toks,.95):>8.0f} {pct(seq,.5):>11.1f} {pct(seq,.95):>11.1f}")

# overall token totals for cost
tot_in = sum(r['in_tok'] for r in recs)
tot_out = sum(r['out_tok'] for r in recs)
# sonnet 4.6 assumed pricing $3/$15 per M
cost = tot_in/1e6*3 + tot_out/1e6*15
print(f"\nTOTAL across experiment: in={tot_in} out={tot_out} approx_cost_sonnet=${cost:.2f}")

# ---------- dogfood: aggregate findings on our epic ----------
print("\n" + "=" * 78)
print("DOGFOOD: aggregated FAIL/AMBIGUOUS findings on epic 5fd2 (size-1 runs, union)")
print("=" * 78)
agg = collections.defaultdict(list)
for rp in range(1, 6):
    run = runs.get(('dogfood_epic', 1, rp), {})
    for cid, f in run.items():
        if f.get('verdict') in ('FAIL', 'AMBIGUOUS'):
            agg[cid].append((rp, f))
for cid in CRITERIA:
    items = agg.get(cid, [])
    if not items:
        continue
    nfail = sum(1 for _, f in items if f['verdict'] == 'FAIL')
    print(f"\n[{cid}] flagged in {len(items)}/5 repeats (FAIL x{nfail}):")
    # show the highest-confidence example
    best = max(items, key=lambda x: x[1].get('confidence', 0))[1]
    print(f"   sev={best['severity']} conf={best.get('confidence')} loc={best.get('location','')[:90]}")
    print(f"   {best.get('finding','')[:300]}")
