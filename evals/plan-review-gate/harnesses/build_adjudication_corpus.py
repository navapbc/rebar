#!/usr/bin/env python3
"""E1 finding-adjudication corpus builder (story doctrinal-untruthful-vaquita / e95e).

Samples plan-review findings from the persisted REVIEW_RESULT sidecar payloads and
writes an UNLABELED adjudication corpus to ``runs/adjudication_corpus.jsonl`` (the
``tp_fp`` / ``rater_a`` / ``rater_b`` / ``rationale`` fields are placeholders that the
LLM adjudicators — ``adjudicate.py`` with ``adjudicate_rubric_{a,b}.md`` — fill in).

SURFACED vs DROPPED is decided POSITIONALLY, not by the ``decision`` field
--------------------------------------------------------------------------
``decision`` alone cannot classify a finding: an overflow-suppressed advisory and a
surfaced advisory both carry ``decision="advisory"``. The sidecar (sidecar.py:472-478)
concatenates findings in a FIXED segment order and ``coverage.counts`` gives each
segment length:

    findings = blocking ++ advisory_surfaced ++ advisory_overflow ++ indeterminate ++ dropped

So:
    SURFACED = findings[: blocking + advisory_surfaced]            (shown to the agent)
    DROPPED  = advisory_overflow segment ++ dropped segment        (sidecar-only)
             — the indeterminate segment sits BETWEEN them and is EXCLUDED (neither
               surfaced nor a suppressed real defect; it is an abstain).

Verified against the whole store: 0 of ~1900 payloads have
``sum(counts) != len(findings)`` (a payload that mismatches is skipped defensively).

Sampling
--------
Stratified by primary criterion (``criteria[0]``), deterministic (fixed seed), deduped
by (ticket_id, finding_id) keeping the most-recent round. Targets ~300 surfaced +
~100 dropped; floor ~280 / ~90 (the population is ~23k / ~12k, so the floor is met with
room to spare). Every criterion present in the population contributes at least one row
before proportional fill, so no criterion is silently dropped.

Usage:
    python build_adjudication_corpus.py            # write runs/adjudication_corpus.jsonl
    python build_adjudication_corpus.py --dry-run  # print counts, write nothing
"""
from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
from collections import defaultdict
from typing import Any

from rebar.llm.plan_review import sidecar

_HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.abspath(os.path.join(_HERE, "..", "runs"))
OUTCOME_PATH = os.path.join(RUNS, "outcome_corpus.jsonl")
OUT_PATH = os.path.join(RUNS, "adjudication_corpus.jsonl")

TARGET_SURFACED = 300
TARGET_DROPPED = 100
FLOOR_SURFACED = 280
FLOOR_DROPPED = 90
SEED = 1729  # fixed seed — reproducible sampling (E1 advisory: deterministic runs)


def _reviewed_ids() -> list[str]:
    with open(OUTCOME_PATH) as fh:
        return [
            r["ticket_id"]
            for r in (json.loads(line) for line in fh)
            if r.get("had_persisted_review")
        ]


def _primary_criterion(f: dict[str, Any]) -> str:
    crits = f.get("criteria") or []
    return crits[0] if crits else "none"


def _finding_row(
    f: dict[str, Any], ticket_id: str, source: str
) -> dict[str, Any]:
    return {
        "finding_id": f.get("id"),
        "ticket_id": ticket_id,
        "criterion": _primary_criterion(f),
        "criteria": f.get("criteria") or [],
        "source": source,  # "surfaced" | "dropped"
        "decision": f.get("decision") if source == "surfaced" else "dropped",
        "drop_reason": f.get("drop_reason"),
        # The finding's substance (what the adjudicator judges). ``reason`` is a routing
        # tag ("default-advisory"), NOT the finding text — the text is ``finding``.
        "finding": f.get("finding") or "",
        "suggested_fix": f.get("suggested_fix") or "",
        "location": f.get("location") or "",
        "severity": f.get("severity"),
        "norm_id": f.get("norm_id"),
        "impact": f.get("impact"),
        "validity": f.get("validity"),
        "priority": f.get("priority"),
        # filled by the adjudicators:
        "tp_fp": "",
        "rater_a": None,
        "rater_b": None,
        "rationale": "",
    }


def _split_positional(
    findings: list[dict[str, Any]], counts: dict[str, int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    b = counts.get("blocking", 0)
    asf = counts.get("advisory_surfaced", 0)
    aov = counts.get("advisory_overflow", 0)
    ind = counts.get("indeterminate", 0)
    drp = counts.get("dropped", 0)
    if b + asf + aov + ind + drp != len(findings):
        return None  # schema drift — skip this payload defensively
    surfaced = findings[: b + asf]
    overflow_seg = findings[b + asf : b + asf + aov]
    dropped_seg = findings[b + asf + aov + ind : b + asf + aov + ind + drp]
    return surfaced, overflow_seg + dropped_seg


def collect() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """All surfaced and dropped finding rows across reviewed tickets, deduped by
    (ticket_id, finding_id) keeping the most-recent round (payloads are newest-first).

    EXCLUDES findings with no persisted statement text (empty ``finding``). The finding
    prose (``finding`` / ``suggested_fix``) is persisted only since story 4e19/e344; review
    results written before it carry a leaner schema (metadata + ``verification`` only, no
    statement). A finding with no statement is NOT adjudicatable by any rater — judging TP/FP
    from location+criterion alone is guessing — so such rows are dropped from the ground-truth
    population. The per-source exclusion count is returned for the README provenance note.
    """
    seen: set[tuple[str, str]] = set()
    surfaced: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    excluded = {"surfaced": 0, "dropped": 0}
    for tid in _reviewed_ids():
        try:
            payloads = sidecar.all_review_results(tid)
        except Exception:  # noqa: BLE001 — a bad sidecar must not abort the sweep
            continue
        for p in payloads:
            split = _split_positional(
                p.get("findings", []), p.get("coverage", {}).get("counts", {})
            )
            if split is None:
                continue
            surf, drop = split
            for f, bucket, src in (
                *((x, surfaced, "surfaced") for x in surf),
                *((x, dropped, "dropped") for x in drop),
            ):
                if not (f.get("finding") or "").strip():
                    excluded[src] += 1  # no statement to adjudicate (pre-4e19 lean schema)
                    continue
                key = (tid, str(f.get("id")))
                if key in seen:
                    continue
                seen.add(key)
                bucket.append(_finding_row(f, tid, src))
    return surfaced, dropped, excluded


def stratified_sample(
    rows: list[dict[str, Any]], target: int, rng: random.Random
) -> list[dict[str, Any]]:
    """Sample ~target rows stratified by primary criterion: one guaranteed row per
    criterion, then proportional fill. Deterministic under the given rng."""
    if len(rows) <= target:
        return list(rows)
    by_crit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_crit[r["criterion"]].append(r)
    for bucket in by_crit.values():
        rng.shuffle(bucket)

    picked: list[dict[str, Any]] = []
    # 1) one guaranteed row per criterion (coverage floor).
    for crit in sorted(by_crit):
        picked.append(by_crit[crit].pop())
    # 2) proportional fill from the remainder.
    remainder = [r for bucket in by_crit.values() for r in bucket]
    rng.shuffle(remainder)
    need = max(0, target - len(picked))
    picked.extend(remainder[:need])
    rng.shuffle(picked)
    return picked


def _atomic_write_jsonl(rows: list[dict[str, Any]], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def build() -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    surfaced, dropped, _excluded = collect()
    s = stratified_sample(surfaced, TARGET_SURFACED, rng)
    d = stratified_sample(dropped, TARGET_DROPPED, rng)
    if len(s) < FLOOR_SURFACED or len(d) < FLOOR_DROPPED:
        raise SystemExit(
            f"floor not met: {len(s)} surfaced (>= {FLOOR_SURFACED}?), "
            f"{len(d)} dropped (>= {FLOOR_DROPPED}?). population="
            f"{len(surfaced)}/{len(dropped)}"
        )
    return s + d


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print counts, write nothing")
    args = ap.parse_args(argv)

    rng = random.Random(SEED)
    surfaced, dropped, excluded = collect()
    s = stratified_sample(surfaced, TARGET_SURFACED, rng)
    d = stratified_sample(dropped, TARGET_DROPPED, rng)
    rows = s + d

    if args.dry_run:
        from collections import Counter

        print(
            f"[dry-run] population surfaced={len(surfaced)} dropped={len(dropped)} "
            f"(excluded text-less: surfaced={excluded['surfaced']} dropped={excluded['dropped']}); "
            f"sampled surfaced={len(s)} dropped={len(d)}; "
            f"criteria(surfaced)={len(Counter(r['criterion'] for r in s))} "
            f"criteria(dropped)={len(Counter(r['criterion'] for r in d))}"
        )
        return 0

    if len(s) < FLOOR_SURFACED or len(d) < FLOOR_DROPPED:
        raise SystemExit(f"floor not met: {len(s)}/{len(d)}")
    _atomic_write_jsonl(rows, OUT_PATH)
    print(f"wrote {len(rows)} rows ({len(s)} surfaced + {len(d)} dropped) -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
