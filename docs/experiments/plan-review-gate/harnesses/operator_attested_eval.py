#!/usr/bin/env python3
"""R2 self-gate — deterministic lexicon precision/recall eval for the
operator-attested-evidence-kind advisory DET lint (ticket b080, epic 6982).

The lint (``det_floor._operator_evidence_ac_gaps``, surfaced advisory through
``p6_ac_quality``) flags AC checklist items whose "done" evidence lives OUTSIDE the
codebase (deploy / prod / live-run / IaC / cloud-state / merge-gate / human action /
drill / store-surgery / recorded attestation) but which are NOT tagged
``[operator-attested]`` (ADR-0043). This harness self-gates it with NO LLM anywhere:
it runs the *shipped* detector over a frozen historical AC corpus and checks

    precision >= 70% over a >= 50-item flagged sample,
    overall flag rate <= 5%,
    and both known regression cases (115b, 8c4f) fire.

Precision ground-truth is a frozen per-item TP/FP adjudication committed in
``runs/operator_attested_eval.json`` (a full census of the flagged set); recall is
measured against the human-``[operator-attested]``-tagged items (imperfect but
deterministic). This harness is the reproducibility check: it re-derives the flagged
set + metrics from the shipped detector and the frozen corpus and asserts they match
the committed result. Run ``--rebuild`` to regenerate the result JSON (adjudication
labels are preserved by (ticket_id, ac_text)).

    python docs/experiments/plan-review-gate/harnesses/operator_attested_eval.py
    python docs/experiments/plan-review-gate/harnesses/operator_attested_eval.py --rebuild

Exit 0 iff the gate verdict is PASS.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from rebar.llm.plan_review.det_floor import _operator_evidence_ac_gaps

_RUNS = Path(__file__).resolve().parents[1] / "runs"
CORPUS = _RUNS / "operator_attested_ac_corpus.jsonl"
RESULT = _RUNS / "operator_attested_eval.json"

_TAG_STRIP = re.compile(r"\[operator-attested\]\s*", re.IGNORECASE)
_KNOWN = ("115b", "8c4f")


def _strip_tag(text: str) -> str:
    return _TAG_STRIP.sub("", text, count=1)


def _load_corpus() -> list[dict]:
    return [json.loads(ln) for ln in CORPUS.read_text().splitlines() if ln.strip()]


def _run_detector(rows: list[dict]) -> tuple[list[dict], int, int, int, dict[str, bool]]:
    """Run the shipped detector over the tag-stripped corpus. Returns
    ``(flagged, total, tagged_total, tagged_flagged, known_fired)``."""
    flagged: list[dict] = []
    tagged_total = tagged_flagged = 0
    known = {k: False for k in _KNOWN}
    for r in rows:
        gaps = _operator_evidence_ac_gaps("## Acceptance Criteria\n" + _strip_tag(r["ac_text"]))
        if r["was_tagged"]:
            tagged_total += 1
        if gaps:
            line, markers = gaps[0]
            flagged.append(
                {"ticket_id": r["ticket_id"], "ac_text": _strip_tag(r["ac_text"]), "markers": markers}
            )
            if r["was_tagged"]:
                tagged_flagged += 1
            for k in known:
                if r["ticket_id"].startswith(k):
                    known[k] = True
    return flagged, len(rows), tagged_total, tagged_flagged, known


def _adjudication_index(result: dict) -> dict[tuple[str, str], dict]:
    return {(a["ticket_id"], a["ac_text"]): a for a in result.get("adjudications", [])}


def _compute(rows: list[dict], adj_index: dict[tuple[str, str], dict]) -> dict:
    flagged, total, t_total, t_flagged, known = _run_detector(rows)
    tp = fp = unadjudicated = 0
    for f in flagged:
        a = adj_index.get((f["ticket_id"], f["ac_text"]))
        if a is None:
            unadjudicated += 1
        elif a["label"] == "FP":
            fp += 1
        else:
            tp += 1
    precision = round(100 * tp / len(flagged), 1) if flagged else 0.0
    flag_rate = round(100 * len(flagged) / total, 2) if total else 0.0
    recall = round(100 * t_flagged / t_total, 1) if t_total else 0.0
    verdict = (
        precision >= 70
        and len(flagged) >= 50
        and flag_rate <= 5.0
        and all(known.values())
        and unadjudicated == 0
    )
    return {
        "flagged": flagged,
        "total": total,
        "flag_rate_pct": flag_rate,
        "recall_pct": recall,
        "tagged_total": t_total,
        "tagged_flagged": t_flagged,
        "known": known,
        "tp": tp,
        "fp": fp,
        "unadjudicated": unadjudicated,
        "precision_pct": precision,
        "verdict": "PASS" if verdict else "FAIL",
    }


def _rebuild(rows: list[dict], prior: dict) -> dict:
    """Regenerate the result JSON, preserving frozen TP/FP labels by (ticket_id, ac_text)."""
    adj_index = _adjudication_index(prior)
    c = _compute(rows, adj_index)
    adjudications = []
    for f in c["flagged"]:
        a = adj_index.get((f["ticket_id"], f["ac_text"]))
        adjudications.append({**f, "label": a["label"] if a else "TP", "reason": a["reason"] if a else ""})
    return {
        **{k: prior[k] for k in ("eval_id", "ticket", "description", "detector") if k in prior},
        "corpus": {**prior.get("corpus", {}), "ac_items": c["total"], "tickets": len({r["ticket_id"] for r in rows}), "already_tagged": c["tagged_total"]},
        "flag_rate_pct": c["flag_rate_pct"],
        "recall": {"tagged_total": c["tagged_total"], "tagged_flagged": c["tagged_flagged"], "recall_pct": c["recall_pct"], "note": prior.get("recall", {}).get("note", "")},
        "known_cases": {k: ("FIRES" if v else "MISS") for k, v in c["known"].items()},
        "precision": {"method": prior.get("precision", {}).get("method", ""), "sample_size": len(c["flagged"]), "tp": c["tp"], "fp": c["fp"], "precision_pct": c["precision_pct"]},
        "gate": {"precision_ge_70": c["precision_pct"] >= 70, "sample_ge_50": len(c["flagged"]) >= 50, "flag_rate_le_5": c["flag_rate_pct"] <= 5.0, "known_cases_fire": all(c["known"].values()), "verdict": c["verdict"]},
        "adjudications": adjudications,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true", help="regenerate the committed result JSON")
    args = ap.parse_args()
    rows = _load_corpus()
    prior = json.loads(RESULT.read_text()) if RESULT.exists() else {}

    if args.rebuild:
        result = _rebuild(rows, prior)
        RESULT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        print(f"rebuilt {RESULT}")
        prior = result

    c = _compute(rows, _adjudication_index(prior))
    print(f"corpus: {c['total']} AC items, {c['tagged_total']} tagged")
    print(f"flagged: {len(c['flagged'])} = {c['flag_rate_pct']}%  (gate <= 5%)")
    print(f"precision: {c['tp']}/{len(c['flagged'])} = {c['precision_pct']}%  (gate >= 70%, sample >= 50)")
    print(f"recall: {c['tagged_flagged']}/{c['tagged_total']} = {c['recall_pct']}%  (reported, no threshold)")
    print(f"known cases fire: {c['known']}")
    if c["unadjudicated"]:
        print(f"ERROR: {c['unadjudicated']} flagged item(s) have no frozen adjudication", file=sys.stderr)

    # Cross-check the committed result matches this fresh recomputation (reproducibility).
    if prior and not args.rebuild:
        drift = []
        if prior.get("precision", {}).get("precision_pct") != c["precision_pct"]:
            drift.append("precision")
        if prior.get("flag_rate_pct") != c["flag_rate_pct"]:
            drift.append("flag_rate")
        if prior.get("gate", {}).get("verdict") != c["verdict"]:
            drift.append("verdict")
        if drift:
            print(f"ERROR: committed result drifted from shipped detector: {drift}", file=sys.stderr)
            return 2

    print(f"\nVERDICT: {c['verdict']}")
    return 0 if c["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
