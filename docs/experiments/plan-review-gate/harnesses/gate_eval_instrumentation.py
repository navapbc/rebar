#!/usr/bin/env python3
"""R7 (epic 6982): standing gate-eval instrumentation.

A re-runnable job that classifies each reviewed ticket's outcome
(MISSED / CAUGHT-BUT-IGNORED / UNKNOWABLE / N/A) and emits per-criterion trailing FP-proxy
metrics for the plan-review gate's dogfood monitoring. See the sibling
``../README.md`` ("Gate-eval instrumentation") and epic 6982 story R7 (b1a4).

METHODOLOGICAL FINDING (load-bearing): the MISSED<->CAUGHT split is carried by the
outcome-corpus fields ``post_claim_edit_class`` + ``review_round_count``, NOT by the sidecar
findings — a classifier over the sidecar findings *alone* reaches only 4/8 on the frozen §5.2
cases (the CAUGHT cases have finding profiles at or below the MISSED cases, because
"caught-but-ignored" is a fact about the author's post-claim edit, not about what the gate saw).
The sidecar contributes only the MISSED<->UNKNOWABLE tiebreak via ``has_strong_finding``. This
core cascade reproduces the human labels at 7/8.

USAGE
    python gate_eval_instrumentation.py --verify-repro     # 8-case reproduction, asserts >=6/8
    python gate_eval_instrumentation.py --emit             # writes ../runs/gate_eval_metrics.json
    python gate_eval_instrumentation.py --emit --window 200 --no-refresh

``--no-refresh`` reads the committed corpus without re-mining (offline / CI). Otherwise the job
refreshes ``../runs/outcome_corpus.jsonl`` by invoking ``mine_outcome_corpus.py`` as a
SUBPROCESS (that entry ``sys.exit()``s on a floor-check failure, so it must not be imported).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RUNS = HERE.parent / "runs"
OUTCOME_CORPUS = RUNS / "outcome_corpus.jsonl"
METRICS_OUT = RUNS / "gate_eval_metrics.json"
MINER = HERE / "mine_outcome_corpus.py"

# The "substantive advisory / strong finding" predicate. 0.5 is the codebase's own
# RECALL_MIN_PRIORITY (src/rebar/llm/plan_review/sidecar.py); the severity conjunct excludes
# high-priority-but-procedural (P6/P9) findings.
STRONG_PRIORITY_FLOOR = 0.5
STRONG_SEVERITIES = frozenset({"critical", "major"})

# An "adverse post-claim edit" = the substantive edit classes (operator-attested-retag is handled
# by the classifier's C1 arm; cosmetic/none are not adverse).
ADVERSE_EDIT_CLASSES = frozenset(
    {"plan-authored-post-claim", "ac-strengthened", "substantive-unclassified"}
)
DEFAULT_WINDOW = 200

# §5.2 frozen labels (mine_outcome_corpus.py S5_PUBLISHED; ../runs/README.md:99-102). Keyed by the
# short (two-quad) id; matched against corpus ticket_ids by prefix.
FROZEN_LABELS: dict[str, str] = {
    "dc58-af7b": "MISSED",
    "db7b-c8fd": "MISSED",
    "5886-d028": "MISSED",
    "c8cc-68b8": "CAUGHT-BUT-IGNORED",
    "f5df-0069": "CAUGHT-BUT-IGNORED",
    "115b-ceea": "CAUGHT-BUT-IGNORED",
    "8c4f-b81c": "CAUGHT-BUT-IGNORED",
    "3006-e198": "UNKNOWABLE",
}
REPRO_FLOOR = 6  # >=6/8 agreement required (the R7 acceptance gate)


# ── pure functions (unit-tested in tests/unit/test_gate_eval_classifier.py) ──────────────────


def classify(
    post_claim_edit_class: str,
    review_round_count: int,
    has_strong: bool,
    had_persisted_review: bool = True,
) -> str:
    """The core deterministic cascade (first match wins). Only tickets with a post-claim signal
    — an adverse edit (``ADVERSE_EDIT_CLASSES``), an ``operator-attested-retag``, or >=2 review
    rounds — are classified; the rest (``none``/``cosmetic`` with <2 rounds) are ``N/A``.
    Reproduces the frozen §5.2 labels at 7/8 (only c8cc, a lone ``substantive-unclassified`` case,
    misclassifies as MISSED — a ``substantive-unclassified -> CAUGHT`` rule would reach 8/8 but
    rests on that single case, so it is deliberately excluded as overfitting)."""
    signal = (
        post_claim_edit_class in ADVERSE_EDIT_CLASSES
        or post_claim_edit_class == "operator-attested-retag"
        or review_round_count >= 2
    )
    if not had_persisted_review or not signal:
        return "N/A"
    if post_claim_edit_class == "operator-attested-retag":
        return "CAUGHT-BUT-IGNORED"
    if review_round_count >= 2:
        return "CAUGHT-BUT-IGNORED"
    if has_strong:
        return "UNKNOWABLE"
    return "MISSED"


def _finding_key(f: dict[str, Any]) -> tuple[tuple[str, ...], str]:
    return (tuple(sorted(f.get("criteria") or [])), f.get("finding") or "")


def dedup_union(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The deduplicated union of findings across all retained review rounds, keyed by
    ``(sorted(criteria), finding text)`` — a finding that persists across rounds counts once (no
    denominator inflation), a finding cleared in a later round is still counted (no omission)."""
    seen: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
    for payload in payloads:
        for f in payload.get("findings") or []:
            seen.setdefault(_finding_key(f), f)
    return list(seen.values())


def is_strong(f: dict[str, Any]) -> bool:
    return (
        f.get("decision") in ("block", "advisory")
        and float(f.get("priority") or 0.0) >= STRONG_PRIORITY_FLOOR
        and (f.get("severity") or "") in STRONG_SEVERITIES
    )


def has_strong_finding(findings: list[dict[str, Any]]) -> bool:
    return any(is_strong(f) for f in findings)


def compute_metrics(
    rows: list[dict[str, Any]], findings_by_ticket: dict[str, list[dict[str, Any]]]
) -> dict[str, dict[str, Any]]:
    """Per-criterion FINDING-level trailing metrics over ``rows`` (the windowed outcome corpus)
    joined to each ticket's deduplicated finding union by ``ticket_id``. Pure — the golden test
    feeds a fixture. For each criterion C:

    * ``blocking_fp_proxy`` = (# of C's blocking findings whose ticket was force-closed or
      reopened) / (# of C's blocking findings) — the conservative FP lower bound.
    * ``advisory_application_rate`` = (# of C's advisory findings whose ticket later shows an
      adverse post-claim edit) / (# of C's advisory findings).
    * ``sample_counts`` = the denominators + distinct-ticket count.
    """
    blk_total: dict[str, int] = defaultdict(int)
    blk_fp: dict[str, int] = defaultdict(int)
    adv_total: dict[str, int] = defaultdict(int)
    adv_applied: dict[str, int] = defaultdict(int)
    crit_tickets: dict[str, set[str]] = defaultdict(set)
    outcome = {r["ticket_id"]: r for r in rows}

    for tid, findings in findings_by_ticket.items():
        row = outcome.get(tid)
        if row is None:
            continue
        adverse = row.get("post_claim_edit_class") in ADVERSE_EDIT_CLASSES
        fp_ticket = bool(row.get("force_close")) or (row.get("reopen_count") or 0) > 0
        for f in findings:
            decision = f.get("decision")
            for c in f.get("criteria") or []:
                crit_tickets[c].add(tid)
                if decision == "block":
                    blk_total[c] += 1
                    if fp_ticket:
                        blk_fp[c] += 1
                elif decision == "advisory":
                    adv_total[c] += 1
                    if adverse:
                        adv_applied[c] += 1

    out: dict[str, dict[str, Any]] = {}
    for c in set(blk_total) | set(adv_total):
        bt, at = blk_total[c], adv_total[c]
        out[c] = {
            "blocking_fp_proxy": (blk_fp[c] / bt) if bt else None,
            "advisory_application_rate": (adv_applied[c] / at) if at else None,
            "sample_counts": {
                "blocking_findings": bt,
                "advisory_findings": at,
                "tickets": len(crit_tickets[c]),
            },
        }
    return out


# ── I/O layer ────────────────────────────────────────────────────────────────────────────────


def refresh_corpus() -> None:
    """Re-mine ``outcome_corpus.jsonl`` by invoking the miner as a subprocess (it ``sys.exit()``s
    on a floor-check failure, so importing + calling it would terminate this process)."""
    subprocess.run([sys.executable, str(MINER)], check=True, cwd=str(HERE))


def load_corpus(window: int | None = None) -> list[dict[str, Any]]:
    if not OUTCOME_CORPUS.exists():
        raise SystemExit(f"outcome corpus not found: {OUTCOME_CORPUS} (run without --no-refresh)")
    rows = [json.loads(ln) for ln in OUTCOME_CORPUS.read_text().splitlines() if ln.strip()]
    if window is not None and window > 0:
        rows = rows[-window:]  # last N rows in the miner's deterministic emission order
    return rows


def ticket_findings(ticket_id: str, repo_root: Any = None) -> list[dict[str, Any]]:
    from rebar.llm.plan_review import sidecar

    return dedup_union(sidecar.all_review_results(ticket_id, repo_root=repo_root))


def verify_repro(rows: list[dict[str, Any]], repo_root: Any = None) -> tuple[int, list[tuple]]:
    by_prefix: dict[str, dict[str, Any]] = {}
    for r in rows:
        for short in FROZEN_LABELS:
            if str(r.get("ticket_id", "")).startswith(short):
                by_prefix[short] = r
    agree, results = 0, []
    for short, gold in FROZEN_LABELS.items():
        row = by_prefix.get(short)
        if row is None:
            results.append((short, gold, "UNRESOLVED", False))
            continue
        strong = has_strong_finding(ticket_findings(row["ticket_id"], repo_root))
        pred = classify(
            row.get("post_claim_edit_class", ""),
            int(row.get("review_round_count", 0) or 0),
            strong,
            bool(row.get("had_persisted_review", True)),
        )
        ok = pred == gold
        agree += int(ok)
        results.append((short, gold, pred, ok))
    return agree, results


def emit(rows: list[dict[str, Any]], repo_root: Any = None) -> dict[str, dict[str, Any]]:
    if not rows:
        raise SystemExit("window resolved to zero tickets — nothing to emit")
    fbt = {r["ticket_id"]: ticket_findings(r["ticket_id"], repo_root) for r in rows}
    return compute_metrics(rows, fbt)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--verify-repro", action="store_true", help="8-case reproduction (asserts >=6/8)"
    )
    ap.add_argument("--emit", action="store_true", help="write per-criterion metrics JSON")
    ap.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW, help="trailing window size (rows)"
    )
    ap.add_argument(
        "--no-refresh", action="store_true", help="read committed corpus, do not re-mine"
    )
    args = ap.parse_args(argv)
    if not (args.verify_repro or args.emit):
        ap.error("pass --verify-repro and/or --emit")

    if not args.no_refresh:
        refresh_corpus()

    if args.verify_repro:
        rows = load_corpus(window=None)  # the 8 cases may be anywhere in history
        agree, results = verify_repro(rows)
        print("gate-eval §5.2 reproduction (short  gold -> predicted):")
        for short, gold, pred, ok in results:
            print(f"  {'OK ' if ok else 'XX '} {short}  {gold:18} -> {pred}")
        print(f"agreement: {agree}/{len(FROZEN_LABELS)} (floor {REPRO_FLOOR})")
        if agree < REPRO_FLOOR:
            print("REPRODUCTION FAILED", file=sys.stderr)
            return 1

    if args.emit:
        rows = load_corpus(window=args.window)
        metrics = emit(rows)
        METRICS_OUT.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
        print(f"wrote {len(metrics)} criteria -> {METRICS_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
