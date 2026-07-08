# code-review impact-model calibration fixtures

`code_review_impact_labels.jsonl` — a curated, labeled set of code-review findings used to
calibrate and regression-test `rebar.llm.review_kernel.decide.impact_code` (story
`albite-lazy-barb`). One JSON object per line:

- `label` — `HIGH` (a genuine landmine / block-worthy defect) or `NIT` (a low-consequence
  finding that must NOT reach the block zone).
- `note` — one-line human description of the finding.
- `severity_attributes` — the exact attrs dict `impact_code` consumes: the LLM-emitted
  consequence binaries + `trigger_likelihood` + detection booleans, plus the DET-enriched
  `churn90` / `hard_to_reverse_surface` signals (as `code_review_decide` would inject them).

**Provenance.** Seeded from rebar's own dogfooded code-review corpus and the program's
held-out adjudication of representative HIGH/NIT findings (~31 labeled entries). It grows as
the data-capture child (`limestone-unethical-zebrafinch`) captures real reviews.

`tests/unit/test_impact_code.py` asserts the model SEPARATES the two classes:
`median(impact_code[HIGH]) − median(impact_code[NIT]) > 0.30`, `median(impact_code[NIT]) <
0.30`, and `median(HIGH) > median(NIT)`. The absolute threshold that turns this separation
into block/advisory postures is co-calibrated later (`raptorial-galloping-dragon`).
