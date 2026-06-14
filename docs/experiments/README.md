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

### Real-code de-risking (run against the installed `rebar` + a live `.tickets-tracker`)

| Exp | Validates | Key result |
|-----|-----------|-----------|
| EXP-R1 | P2.3 bug is real | the **real reducer** silently clobbers one of two concurrent whole-field tag EDITs |
| EXP-R5 | P2.3 fix | delta ops on the real reduced base → both concurrent adds survive |
| EXP-R2 | P2.1 sort change | `event_sort_key` ts segment is `str`; int-order == string-order on real filenames; malformed fallback ok |
| EXP-R3 | P1.0 | real `_canonical_bytes` ≠ plain dumps; parsed dicts identical (replay-safe) |
| EXP-R4 | P2.3 wire-compat | real reducer preserve-and-ignores an unknown `TAG` event (no crash) |
| EXP-R6 | P1.4 | gc recipe on the real orphan-branch worktree: 26→0 loose objects; reads survive |
| EXP-R7 | P1.0 | `python3 -m rebar._store.<submodule>` works → bash heredocs can call the canonical helper |
| EXP-R9/R9b | P1.0 guard | `event_write_guard.py` flags exactly 7 py + 7 sh writers, 0 false positives (no semgrep needed) |
| EXP-R10 | P1.1 | confirmed real `search_states` / `apply_ticket_filters` signatures |
| EXP-R11 | test harness | 31 reducer/sort/filter/search tests pass in 2.5 s (needs `[dev]` extra) |
| EXP-R8 | P2.2 / robustness | rebar writes succeed under the env's forced commit signing; ambient signing is a latent portability note |

`event_write_guard.py` is a **committed, reusable artifact** (stdlib-only) — the
reference implementation for P1.0's CI guard. Run `python3 docs/experiments/event_write_guard.py src/rebar`.

> These are throwaway prototypes for design validation, not production code. The
> production implementations live under `src/rebar/` per the plan. `hlc_prototype.py`
> now demonstrates **both** the local-lock fast path (`next_tick`) and the
> reconstructable-cache refinement (`next_tick_for_ticket` witnesses `max(prefix)`
> of the target ticket's events, so the local clock file is disposable).
