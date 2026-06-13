# Remediation-plan validation experiments

Reproducible prototypes backing the **Experimental validation** section of
[`../remediation-implementation-plan.md`](../remediation-implementation-plan.md).
Each was run during the proven-art review to validate feasibility and surface
gotchas before committing to an implementation approach. Results are summarized
in the plan's scorecard; the scripts here let you reproduce them.

| Exp | Validates | Key result |
|-----|-----------|-----------|
| EXP1 | P2.1 HLC width / int-vs-string sort | `time_ns()` 19 digits until ~2286; sorts agree at equal width |
| EXP2 | P1.0 canonical bytes vs `jq -S -c` | byte-equal **only** with `ensure_ascii=False` |
| EXP-jq | P1.0/P2.1 jq number precision | jq parses >2^53 as float64; ≤1.6 rounds on parse, 1.7 rounds on arithmetic → keep jq out of the event path |
| EXP4 | P2.1 `next_tick()` concurrency | 2400 concurrent flock'd ticks: all unique, monotonic, 19-digit (`hlc_prototype.py <n>`) |
| EXP4b | P2.1 cache-as-reconstructable | with **no** cache file, `next_tick_for_ticket()` still issues a tick > `max(prefix)`, 19-digit (`hlc_prototype.py witness <dir>`) — validates the git-bug "witness the log; cache is disposable" rule |
| EXP5/5b/5c | P2.3 tag convergence | the chosen **delta-replay model** converges over all merge orders; the OR-Set variant (validated but **rejected** — see plan) also converged, with order-independent tombstone-by-tag and a deterministic `seed:<tag>` |
| EXP6 | P1.4 gc/reflog window | survives `--prune=14.days.ago`, dies at `--prune=now` |
| EXP7 | P1.0 structural guard | AST scan caught all 7 live `.py` event writers; bash needs a regex prong |
| EXP8 | P1.1 query parser | predicates + OR + negation + degrade-to-substring in ~40 LOC |
| EXP-gpg | P2.2 signing | ed25519 detached sign/verify/tamper round-trip over canonical bytes |

> These are throwaway prototypes for design validation, not production code. The
> production implementations live under `src/rebar/` per the plan. `hlc_prototype.py`
> now demonstrates **both** the local-lock fast path (`next_tick`) and the
> reconstructable-cache refinement (`next_tick_for_ticket` witnesses `max(prefix)`
> of the target ticket's events, so the local clock file is disposable).
