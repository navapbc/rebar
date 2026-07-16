#!/usr/bin/env python3
"""E1 inter-rater reliability report (story doctrinal-untruthful-vaquita / e95e).

Computes **Cohen's kappa** on the double-labeled subset of the adjudication corpus —
Rater A (``rater_a``) vs Rater B (``rater_b``) — over the binary {TP, FP} labels,
excluding pairwise any row where either rater said ``ambiguous`` (or left a blank).

Reported OVERALL and STRATIFIED by ``source`` (surfaced vs dropped), because the two lenses
are cognitively different — a dropped finding inverts the TP/FP mapping ("spurious + dropped
= a good drop = TP"), so the two raters agree far less on dropped findings, and a single
blended kappa hides that structure.

Role of this number (operator-approved scope, 2026-07-15): the LLM A/B kappa is REPORTED for
transparency — it is the reproducibility signal, not a ship/discard gate. The corpus's
authoritative labels come from HUMAN ADJUDICATION of the contested findings
(cluster_disputes.py -> operator verdict -> apply_adjudications.py). Pass ``--strict`` to
restore the original hard gate (exit non-zero when overall kappa < the threshold).

Usage:
    python kappa.py runs/adjudication_corpus.jsonl          # report (exit 0)
    python kappa.py runs/adjudication_corpus.jsonl --strict # hard gate on overall kappa
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

KAPPA_GATE = 0.7
_LABELS = ("TP", "FP")


def cohens_kappa(pairs: list[tuple[str, str]]) -> float:
    """Cohen's kappa for two raters over the binary {TP, FP} label set."""
    n = len(pairs)
    if n == 0:
        return float("nan")
    po = sum(1 for a, b in pairs if a == b) / n
    pe = 0.0
    for lab in _LABELS:
        pa = sum(1 for a, _ in pairs if a == lab) / n
        pb = sum(1 for _, b in pairs if b == lab) / n
        pe += pa * pb
    if pe == 1.0:  # both raters unanimous on one label — kappa undefined; treat as 1.0
        return 1.0
    return (po - pe) / (1 - pe)


def gwet_ac1(pairs: list[tuple[str, str]]) -> float:
    """Gwet's AC1 — a chance-corrected agreement coefficient that, unlike Cohen's kappa, is
    ROBUST to prevalence skew (the "kappa paradox": high raw agreement but low kappa when one
    label dominates). Reported alongside kappa because these findings lean heavily TP (the
    gate mostly surfaces real defects and mostly drops genuine noise), which deflates kappa.

    AC1 chance term: pe = (1/(q-1)) * Σ_k π_k(1-π_k), π_k = mean marginal prob of class k.
    """
    n = len(pairs)
    if n == 0:
        return float("nan")
    po = sum(1 for a, b in pairs if a == b) / n
    q = len(_LABELS)
    pe = 0.0
    for lab in _LABELS:
        pa = sum(1 for a, _ in pairs if a == lab) / n
        pb = sum(1 for _, b in pairs if b == lab) / n
        pi = (pa + pb) / 2
        pe += pi * (1 - pi)
    pe /= q - 1
    if pe == 1.0:
        return 1.0
    return (po - pe) / (1 - pe)


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Reliability stats for one row-subset (all / surfaced / dropped)."""
    double = [r for r in rows if r.get("rater_a") and r.get("rater_b")]
    usable = [
        (r["rater_a"], r["rater_b"])
        for r in double
        if r["rater_a"] in _LABELS and r["rater_b"] in _LABELS
    ]
    kappa = cohens_kappa(usable) if usable else float("nan")
    ac1 = gwet_ac1(usable) if usable else float("nan")
    agree = sum(1 for a, b in usable if a == b)
    confusion = {
        f"A={a},B={b}": sum(1 for x, y in usable if x == a and y == b)
        for a in _LABELS
        for b in _LABELS
    }
    return {
        "double_labeled_rows": len(double),
        "usable_pairs_binary": len(usable),
        "excluded_ambiguous_or_blank": len(double) - len(usable),
        "raw_agreement": round(agree / len(usable), 4) if usable else None,
        "cohens_kappa": round(kappa, 4) if usable else None,
        "gwet_ac1": round(ac1, 4) if usable else None,
        "confusion": confusion,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", help="path to adjudication_corpus.jsonl")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="restore the original hard gate: exit non-zero when overall kappa < threshold",
    )
    args = ap.parse_args(argv)

    with open(args.corpus) as fh:
        rows = [json.loads(line) for line in fh]

    overall = _stats(rows)
    surfaced = _stats([r for r in rows if r.get("source") == "surfaced"])
    dropped = _stats([r for r in rows if r.get("source") == "dropped"])
    k = overall["cohens_kappa"]
    print(
        json.dumps(
            {
                "overall": overall,
                "by_source": {"surfaced": surfaced, "dropped": dropped},
                "gate_threshold": KAPPA_GATE,
                "overall_verdict": (
                    "n/a (no usable pairs)"
                    if k is None
                    else ("meets-threshold" if k >= KAPPA_GATE else "below-threshold")
                ),
                "authoritative_labels": "human-adjudication (see apply_adjudications.py); LLM kappa is reported for transparency",
            },
            indent=2,
        )
    )
    if overall["usable_pairs_binary"] < 50:
        print(
            f"WARNING: only {overall['usable_pairs_binary']} usable binary pairs "
            "(< 50-finding floor).",
            file=sys.stderr,
        )
    if args.strict and (k is None or k < KAPPA_GATE):
        print(
            f"STRICT GATE FAILED: overall kappa {k} < {KAPPA_GATE}.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
