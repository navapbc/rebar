#!/usr/bin/env python3
"""E1 adjudication fold (story doctrinal-untruthful-vaquita / e95e).

Folds the operator's cluster verdicts (``runs/disputes.jsonl``, produced by
``cluster_disputes.py`` and filled in by the human) back into the adjudication corpus as the
authoritative **final gold label** on every finding:

  * a finding in an adjudicated dispute cluster  -> the human's verdict (a per-member
    override wins over the cluster verdict when the plan genuinely differs),
  * a finding both raters AGREED on              -> that agreed label,
  * a finding only Rater A labeled (outside the double-labeled subset) -> Rater A's label,
    flagged single-rater,
  * a finding still un-adjudicated (human_label null) -> left ``pending-human``.

Re-runnable and incremental: run it after each batch of human verdicts; already-decided rows
are simply recomputed identically, and newly-filled clusters flip from pending to gold.

Usage:
    python apply_adjudications.py     # corpus += final_label/label_source (atomic write)
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.abspath(os.path.join(_HERE, "..", "runs"))
CORPUS = os.path.join(RUNS, "adjudication_corpus.jsonl")
DISPUTES = os.path.join(RUNS, "disputes.jsonl")

_DEFINITE = ("TP", "FP")
_VALID = ("TP", "FP", "ambiguous")


def _key(ticket_id: str, finding_id: Any) -> tuple[str, str]:
    return (str(ticket_id), str(finding_id))


def _load_verdicts(path: str) -> dict[tuple[str, str], str | None]:
    """Map every disputed (ticket_id, finding_id) -> its effective human verdict (or None if
    the cluster is not yet adjudicated). Per-member overrides win over the cluster verdict."""
    verdicts: dict[tuple[str, str], str | None] = {}
    if not os.path.exists(path):
        return verdicts
    with open(path) as fh:
        clusters = [json.loads(line) for line in fh]
    for c in clusters:
        cluster_label = c.get("human_label")
        if cluster_label is not None and cluster_label not in _VALID:
            raise SystemExit(f"cluster {c['cluster_id']}: bad human_label {cluster_label!r}")
        overrides = c.get("member_overrides") or {}
        member_ids = {str(m["finding_id"]) for m in c["members"]}
        for fid, lab in overrides.items():
            if str(fid) not in member_ids:
                raise SystemExit(f"cluster {c['cluster_id']}: override for non-member {fid!r}")
            if lab not in _VALID:
                raise SystemExit(f"cluster {c['cluster_id']}: bad override {fid}={lab!r}")
        for m in c["members"]:
            fid = str(m["finding_id"])
            verdicts[_key(m["ticket_id"], fid)] = overrides.get(fid, cluster_label)
    return verdicts


def _final_label(row: dict[str, Any], verdicts: dict[tuple[str, str], str | None]) -> tuple[str | None, str]:
    k = _key(row["ticket_id"], row.get("finding_id"))
    if k in verdicts:  # this finding is contested (hard or soft dispute)
        v = verdicts[k]
        return (v, "human-adjudicated") if v is not None else (None, "pending-human")
    # Normalize any non-label (blank "" from a failed/empty rater call, or junk) to None so a
    # row Rater A labeled but Rater B never reached (b == "") is treated as single-rater-A, not
    # as unlabeled — a blank cell means "not labeled", identical to being outside the subset.
    a = row.get("rater_a") if row.get("rater_a") in _VALID else None
    b = row.get("rater_b") if row.get("rater_b") in _VALID else None
    if a in _VALID and b in _VALID:
        if a == b:
            return a, ("agreed" if a in _DEFINITE else "agreed-ambiguous")
        # a definite/ambiguous mix that isn't in verdicts should not happen (soft => dispute)
        return None, "pending-human"
    if a in _VALID and b is None:  # outside the double-labeled subset — Rater A only
        return a, "single-rater-A"
    return None, "unlabeled"


def _atomic_write(rows: list[dict[str, Any]], path: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main() -> int:
    with open(CORPUS) as fh:
        rows = [json.loads(line) for line in fh]
    verdicts = _load_verdicts(DISPUTES)

    src_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    for r in rows:
        final, source = _final_label(r, verdicts)
        r["final_label"] = final
        r["label_source"] = source
        src_counts[source] += 1
        label_counts[str(final)] += 1

    _atomic_write(rows, CORPUS)

    gold = sum(1 for r in rows if r["final_label"] in _DEFINITE)
    pending = src_counts.get("pending-human", 0)
    print(
        json.dumps(
            {
                "rows": len(rows),
                "disputed_findings": len(verdicts),
                "adjudicated": sum(1 for v in verdicts.values() if v is not None),
                "pending_human": pending,
                "gold_definite_labels": gold,
                "by_label_source": dict(src_counts),
                "by_final_label": dict(label_counts),
            },
            indent=2,
        )
    )
    if pending:
        print(f"NOTE: {pending} finding(s) still pending human adjudication.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
