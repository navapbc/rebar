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
