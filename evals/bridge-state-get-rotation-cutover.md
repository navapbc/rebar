# GET-rotation cutover packed-cost measurement

Ticket: `3592-0d1e-2bd6-4e22` (A2-2).

The corpus is the 40 consecutive real `bindings.json` revisions ending at
`5282e2358a473010aad45576708ba60996a7bf5f`, the last revision before the
`1cb6-d553-6565-49b4` research session recorded the epic baseline.  This pins
the original steady-state measurement window and excludes later one-time A1
and A3 migration discontinuities.

```console
$ .venv/bin/python scripts/measure_get_rotation_cutover.py \
    --repo .tickets-tracker \
    --count 40 \
    --end-ref 5282e2358a473010aad45576708ba60996a7bf5f
```

The driver rebuilds each projection in a clean repository with identical
commit metadata, repacks with `--window=250 --depth=50`, and computes
`(pack_N - pack_1) / (N - 1)` over distinct changed versions.  It applies the
already-landed A1 timestamp removal and A3 scalar normalization to both sides,
then compares A2-1 dual-write state with the A2-2 sidecar-only projection.

| Projection | Distinct versions | First pack | Final pack | Marginal bytes/version |
|---|---:|---:|---:|---:|
| A2-1 dual-write | 40 | 2,083,015 | 2,150,436 | 1,728.74 |
| A2-2 sidecar-only | 40 | 2,078,596 | 2,128,752 | 1,286.05 |

The A2-2/A2-1 marginal ratio is **0.7439**, satisfying the ticket requirement
that A2-2 remain at or below A2-1.  A2-2 is also about **10.1%** of the epic's
12.79 KB pre-change baseline, below the epic threshold of 25%.  Two consecutive
runs produced byte-identical measurements.  Corpus range:
`46a2426a58985fd8b73fe7ab91ccb421c55cf874..5282e2358a473010aad45576708ba60996a7bf5f`.
