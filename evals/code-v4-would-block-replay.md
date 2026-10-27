# code-v4 would-block replay (ticket 7f9f — `obese-dihedral-ermine`)

Offline replay of the code-review impact model over the stored **code-v3 REVIEW_RESULT sidecar
corpus**, re-scoring every finding under the new `impact_code` (code-v4) to measure the
would-block delta. No sidecar is rewritten — this is offline analysis, per AC1 ("stored code-v3
sidecars not rewritten; the 42→99 replay is offline analysis, not a migration").

Script: [`replay_code_v4_would_block.py`](replay_code_v4_would_block.py).

## How to reproduce

The corpus (1881 sidecars / 1243 changes) lives in the tracker's sidecar store, not this repo
(the same residency as the inputs to `calibrate_code_review_thresholds.py`). Point `--tracker`
at a checkout that holds it:

```
python docs/experiments/replay_code_v4_would_block.py --tracker /path/to/.tickets-tracker
```

With no corpus the script runs a **deterministic, corpus-free self-check** (no model, no
network, no CI provider — runs anywhere) that pins the two invariants AC7 requires:

```
python docs/experiments/replay_code_v4_would_block.py
```

```
self-check (corpus-free, deterministic):
  contract-contradiction fire case : impact=0.9000 block=True
  debt-only + hard_to_reverse      : impact=0.2400 block=False
  PASS
```

## Recorded corpus results

Corpus: 1881 code-v3 sidecars / 1243 changes / 3384 surfaced findings (1664 `tests` findings).
Replay harness reproduces stored v3 priorities with **0/3384 mismatch**.

Blocking set (v4): `secret-detection`, `high-critical-security`, `security`, `api-compat`@0.51,
`deletion-impact`@0.60, `regression`@0.54, `error-handling`@0.50, **`tests`@0.54** (new).

| model | findings block | changes block | rate |
|-------|---------------:|--------------:|-----:|
| v3 (stored) | 61 | 42 | 3.4% |
| v4 (`impact_code`) | **163** | **99** | 8.0% |

- **Demotions of currently-blocking findings: 0.** No finding that blocked under v3 stops
  blocking under v4.
- **Docs/cosmetic entries in the new set: 0.**
- **`prod=none/low` internal-edge mass stays advisory** (~0.27–0.30) — the 1120 correctly-advisory
  `tests` findings do not cross the threshold.
- **Debt lane is churn amplifier-only** (`1.0 + 0.5·min(churn90,30)/30 ∈ [1.0,1.5]`; per the
  operator, churn may only increase impact): debt binaries are minor (0.3), capped at 0.45 <
  every blocking threshold, so debt friction is unchanged at 8.0%.
- **Reversibility floor is consequence-lane-gated** (floor 0.6 requires `consequence_base > 0`
  AND `hard_to_reverse_surface`): debt-only findings never floor. The **4** existing
  debt-only + hard_to_reverse corpus findings stay advisory (self-check pins this at 0.24).
- **No new validity floor** — the 0.85–0.95 validity band was hand-read as true positives; the
  `validity < 0.5` drop stands.

The 8.0% would-block rate (163/99) is operator-accepted friction. See the ticket's evidence
comments for the full v3→v4 replay tables, the threshold sweep, and the 1664-finding
`prod_impact` crosstab that grounds the maintainability-lane re-scoping.
