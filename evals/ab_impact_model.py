#!/usr/bin/env python3
"""Diff-grounded A/B: does the NEW code-review impact model separate landmines from nits well
enough to justify lowering a block threshold?  (story raptorial-galloping-dragon, the objective
gate the ADR-0035 rollout note defers to.)

Scores the checked-in labeled/adjudicated set ``tests/unit/fixtures/code_review_impact_labels.jsonl``
(HIGH = block-worthy landmine, NIT = low-consequence) with the NEW model
(``review_kernel.decide.impact_code``) and reports its HIGH↔NIT MEDIAN SEPARATION.

THE GATE is an ABSOLUTE, regression-detecting bar (NOT "beat the old mean" — the old mean
``decide.impact`` cannot even score this fixture, since the rows carry only the new consequence
binaries, so it returns ~0.25 for every row and a "beat the baseline" gate would be trivially
passable and blind to an ``impact_code`` regression). A threshold-down is justified only when the
NEW model achieves the ADR-0035 separation contract:

    median(HIGH) − median(NIT)  >  MIN_SEPARATION (0.30)   AND   median(NIT)  <  NIT_CEILING (0.30)

The old mean's separation is still reported for TRANSPARENCY (to show it cannot discriminate), but
it does NOT gate. Exits 0 when the absolute gate passes, 1 otherwise — so a misconfigured
``impact_code`` (e.g. collapsed tier values) that fails to separate is caught here.

Run:  python docs/experiments/ab_impact_model.py [--fixture PATH]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from rebar.llm.review_kernel.decide import impact, impact_code

_DEFAULT_FIXTURE = "tests/unit/fixtures/code_review_impact_labels.jsonl"

# The ADR-0035 separation contract — the ABSOLUTE bar a threshold-down must clear.
MIN_SEPARATION = 0.30
NIT_CEILING = 0.30


def load_labels(path: str) -> list[dict]:
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def medians(rows: list[dict], score_fn) -> tuple[float, float, float]:
    """(median HIGH, median NIT, separation) for a scoring function over the labeled set."""
    high = [score_fn(r["severity_attributes"]) for r in rows if r["label"] == "HIGH"]
    nit = [score_fn(r["severity_attributes"]) for r in rows if r["label"] == "NIT"]
    if not high or not nit:
        raise SystemExit("fixture must contain both HIGH and NIT rows")
    m_high, m_nit = statistics.median(high), statistics.median(nit)
    return m_high, m_nit, m_high - m_nit


def gate(rows: list[dict], score_fn=impact_code) -> tuple[float, float, float, bool]:
    """Evaluate the ABSOLUTE separation gate for ``score_fn`` over the labeled set.

    Returns (median_high, median_nit, separation, passes) where
    ``passes = separation > MIN_SEPARATION and median_nit < NIT_CEILING`` (strict, per ADR-0035)."""
    m_high, m_nit, sep = medians(rows, score_fn)
    passes = sep > MIN_SEPARATION and m_nit < NIT_CEILING
    return m_high, m_nit, sep, passes


def run_ab(path: str) -> tuple[float, float, bool]:
    """Report the new model's absolute gate + the old mean for transparency.
    Returns (separation_new, median_nit_new, gate_passes)."""
    rows = load_labels(path)
    nh, nn, sep_new, passes = gate(rows, impact_code)
    _, _, sep_base = medians(rows, impact)  # transparency only — does NOT gate
    print(f"corpus: {len(rows)} labeled findings ({path})")
    print(f"  NEW impact_code : median HIGH={nh:.3f}  NIT={nn:.3f}  separation={sep_new:.3f}")
    print(
        f"  (old mean impact, for reference — cannot score this fixture: separation={sep_base:.3f})"
    )
    print(
        f"GATE (absolute): separation {sep_new:.3f} > {MIN_SEPARATION} "
        f"AND median NIT {nn:.3f} < {NIT_CEILING}  ->  "
        + ("PASS — threshold-down justified" if passes else "FAIL — do NOT lower thresholds")
    )
    return sep_new, nn, passes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", default=_DEFAULT_FIXTURE, help="labeled JSONL set to score")
    args = ap.parse_args()
    _, _, passes = run_ab(args.fixture)
    return 0 if passes else 1


if __name__ == "__main__":
    sys.exit(main())
