# Code-review BLOCK threshold — derivation (ticket 9f25)
Deduped code-v2 corpus: **161** findings.
Priority distribution (validity x impact): {50: 0.0, 75: 0.2743, 90: 0.36, 95: 0.4628, 100: 0.6}, max 0.6.
Adjudication (first-pass proxy): {'block-worthy': 13, 'not-block-worthy': 101, 'ambiguous': 47}.

## Precision curve — precision(block-worthy | priority >= t)
| t | precision |
|---|---|
| 0.3 | 1.0 |
| 0.4 | 1.0 |
| 0.5 | 1.0 |
| 0.54 | 1.0 |
| 0.6 | 1.0 |

## Finding: a hard priority CEILING at 0.6

The trustworthy, adjudication-free signal is the priority distribution: p90=0.36, p95=0.4628, **max=0.6**. The 0.60 ceiling the calibration analysis reported is CONFIRMED — no code-v2 finding ever scored priority above 0.6, so a threshold at 0.6 would catch only the rare max-priority findings, and the 0.54 band (the #518 importlib code-execution security findings) sits near p95.

## Provisional threshold: **0.54** (REQUIRES held-out confirmation)

Applying the operator's decision rule to the band structure: 0.54 catches the #518-class security band while staying below the 0.60 ceiling (0.6 would block almost nothing). This is PROVISIONAL. The precision curve above was produced by a deterministic tier/severity proxy that is CIRCULAR (it correlates with priority) and therefore saturates — it is NOT a valid basis for the final threshold. Per the ticket, a content-based held-out (Sonnet-scored / human) adjudication of the apparent block-worthy findings is required to set the final value; **sibling b9c0 must CONFIRM the threshold against that held-out adjudication before flipping the `security` criterion to blocking** (b9c0's AC already records this contingency).

## ADR-0036 A/B gate result (HIGH↔NIT separation vs baseline) — PASS

The ADR-0036 A/B gate (`docs/experiments/ab_impact_model.py`, over the checked-in labeled set
`tests/unit/fixtures/code_review_impact_labels.jsonl`, 32 findings) is the absolute,
regression-detecting bar that must pass before a threshold-down is justified. Re-run 2026-07-13:

```
corpus: 32 labeled findings (tests/unit/fixtures/code_review_impact_labels.jsonl)
  NEW impact_code : median HIGH=0.900  NIT=0.120  separation=0.780
  (old mean impact, for reference — cannot score this fixture: separation=0.000)
GATE (absolute): separation 0.780 > 0.3 AND median NIT 0.120 < 0.3  ->  PASS — threshold-down justified
```

The NEW `impact_code` model achieves a HIGH↔NIT median separation of **0.780** (bar: >0.3) with
median NIT impact **0.120** (bar: <0.3) — the ADR-0036 separation contract PASSES, so lowering the
code-review block threshold is objectively justified. (This gates the impact MODEL; the provisional
0.54 threshold VALUE still needs the held-out adjudication above before b9c0 relies on the block.)

## Corpus schema (AC vocabulary + canonical source fields)

Each `code_review_adjudication.jsonl` row carries the AC-specified closed vocabulary AND the
canonical rebar source fields it projects from, so it satisfies the contract and stays traceable:

- **`finding_id`** (str) — the AC key; equals the finding's `norm_id` (the diff-scoped join key
  rebar stamps on every finding).
- **`criterion`** (str) — the AC key; the finding's `criteria` array joined with `,` (a code-review
  finding carries MULTIPLE criterion ids, e.g. `"correctness,scope-intent,regression"`).
- **`impact`, `validity`, `priority`** (float) — the stored pass-3 decision fields (decide.py:336-338),
  read directly from the sidecar (a straight projection, no reconstruction).
- **`adjudication`** ∈ `{block-worthy, not-block-worthy, ambiguous}` and **`evidence`** (str).
- Also retained for traceability: **`norm_id`** and **`criteria`** (the raw array) — the canonical
  rebar fields `finding_id`/`criterion` are derived from.
