#!/usr/bin/env python3
"""E1 dispute clustering for human adjudication (story doctrinal-untruthful-vaquita / e95e).

After ``adjudicate.py`` double-labels the stratified subset, the rows where Rater A and
Rater B DISAGREE are the findings whose TP/FP is genuinely contested — the ones worth a
human's scarce attention. Human time is the constraint, so this script does NOT dump every
raw disagreement: it **clusters** them so the operator adjudicates each *distinct* dispute
once and never re-adjudicates the same recurring finding.

Why clustering matters: the plan-review gate emits the *same boilerplate finding* across
many tickets (e.g. "AC #N has no proving command", "the plan hardcodes a magic number"), so
A and B split on it the same way over and over. Ruling on that finding-kind once and
propagating the verdict — while showing every member so the propagation is auditable — is
the correct use of the resource.

Two dispute pools:
  * HARD  — both raters committed and DISAGREE (A,B in {TP,FP}, A != B). The core contested
            set; the worksheet leads with these.
  * SOFT  — one rater committed (TP/FP) and the other abstained (ambiguous / blank). A weaker
            signal; listed separately, optional to adjudicate.

Clustering (deterministic, transparent, CONSERVATIVE so we never silently merge findings
about different plans):
  1. bucket disputes by ARCHETYPE = (source, criterion) — the lens + the criterion fix the
     *kind* of question; two findings under different criteria are different disputes.
  2. within an archetype, union findings whose normalized finding text has token-set
     Jaccard >= SIMILARITY (high threshold — only true near-duplicates collapse).
  3. each union component is one cluster; the operator gives ONE verdict per cluster, with
     per-member overrides available for the rare case where the per-plan truth differs.

Outputs (both under runs/):
  * dispute_worksheet.md  — human-facing: one card per cluster, all members shown, a blank
    HUMAN VERDICT line per cluster + optional per-member override slots.
  * disputes.jsonl        — machine-readable source of truth that apply_adjudications.py
    reads back after the operator fills ``human_label`` (and any ``member_overrides``).

Usage:
    python cluster_disputes.py            # -> runs/dispute_worksheet.md + runs/disputes.jsonl
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.abspath(os.path.join(_HERE, "..", "runs"))
CORPUS = os.path.join(RUNS, "adjudication_corpus.jsonl")
WORKSHEET = os.path.join(RUNS, "dispute_worksheet.md")
DISPUTES = os.path.join(RUNS, "disputes.jsonl")

_DEFINITE = ("TP", "FP")
_VALID = ("TP", "FP", "ambiguous")
# Only near-identical findings collapse. 0.82 keeps distinct wordings (and thus distinct
# per-plan judgments) apart while folding the gate's templated boilerplate together.
SIMILARITY = 0.82


def _norm_tokens(text: str) -> frozenset[str]:
    """Normalize a finding to a token SET for similarity: lowercase, drop digits (ticket-
    specific magic numbers / ids) and punctuation/paths, split on whitespace."""
    t = (text or "").lower()
    t = re.sub(r"\d+", " ", t)  # 256/512/ids are ticket-specific noise, not identity
    t = re.sub(r"[^a-z\s]", " ", t)
    return frozenset(w for w in t.split() if len(w) > 2)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _atomic_write_text(text: str, path: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _atomic_write_jsonl(rows: list[dict[str, Any]], path: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _classify(row: dict[str, Any]) -> str:
    """'hard' (A!=B, both definite), 'soft' (one definite, one 'ambiguous'), or 'none'.

    A dispute requires BOTH raters to have actually judged the row: a blank ``rater_b`` means
    the finding is simply outside the double-labeled subset (Rater B never looked), NOT that
    the raters disagree — those are 'none'. Only rows where both labels are valid
    (TP/FP/ambiguous) can be disputes.
    """
    a, b = row.get("rater_a"), row.get("rater_b")
    if a not in _VALID or b not in _VALID:
        return "none"  # not double-labeled → not a dispute
    if a in _DEFINITE and b in _DEFINITE:
        return "hard" if a != b else "none"
    if (a in _DEFINITE) ^ (b in _DEFINITE):  # one committed, the other said 'ambiguous'
        return "soft"
    return "none"  # both 'ambiguous' → nothing for a human to break


def _cluster(disputes: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Union-find near-duplicate findings within each (source, criterion) archetype.

    Deterministic: rows are pre-sorted by (source, criterion, finding_id) so the union order
    — and therefore the components and their representatives — are stable across runs.
    """
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for d in disputes:
        buckets.setdefault((d["source"], d.get("criterion") or ""), []).append(d)

    clusters: list[list[dict[str, Any]]] = []
    for key in sorted(buckets):
        members = sorted(buckets[key], key=lambda r: str(r.get("finding_id")))
        toks = [_norm_tokens(m.get("finding") or "") for m in members]
        parent = list(range(len(members)))

        def find(i: int, parent: list[int] = parent) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if _jaccard(toks[i], toks[j]) >= SIMILARITY:
                    parent[find(j)] = find(i)

        comps: dict[int, list[dict[str, Any]]] = {}
        for idx, m in enumerate(members):
            comps.setdefault(find(idx), []).append(m)
        # emit components in a stable order (largest first, then by representative id)
        for comp in sorted(
            comps.values(),
            key=lambda c: (-len(c), str(c[0].get("finding_id"))),
        ):
            clusters.append(comp)
    return clusters


def _representative(members: list[dict[str, Any]]) -> dict[str, Any]:
    """The longest finding text is the most informative; tie-break by finding_id."""
    return max(members, key=lambda m: (len(m.get("finding") or ""), str(m.get("finding_id"))))


def _cluster_records(
    clusters: list[list[dict[str, Any]]], pool: str, start: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for offset, members in enumerate(clusters):
        rep = _representative(members)
        cid = f"{pool[0].upper()}{start + offset:03d}"  # e.g. H001 / S001
        out.append(
            {
                "cluster_id": cid,
                "pool": pool,
                "source": rep["source"],
                "criterion": rep.get("criterion"),
                "n_members": len(members),
                "representative": {
                    k: rep.get(k)
                    for k in ("finding_id", "ticket_id", "finding", "suggested_fix", "location", "severity")
                },
                "rater_a": rep.get("rater_a"),
                "rater_b": rep.get("rater_b"),
                "rationale_a": rep.get("rationale"),
                "members": [
                    {
                        "ticket_id": m["ticket_id"],
                        "finding_id": m["finding_id"],
                        "finding": m.get("finding"),
                        "rater_a": m.get("rater_a"),
                        "rater_b": m.get("rater_b"),
                    }
                    for m in members
                ],
                "human_label": None,  # operator fills: "TP" | "FP" | "ambiguous"
                "member_overrides": {},  # optional {finding_id: label} where a member differs
            }
        )
    return out


def _fmt_finding(text: str | None, width: int = 600) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t if len(t) <= width else t[:width] + " …"


def _worksheet(records: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    L: list[str] = []
    L.append("# Dispute adjudication worksheet — E1 (`doctrinal-untruthful-vaquita` / `e95e`)")
    L.append("")
    L.append(
        "The plan-review gate emits the same finding across many tickets, so Rater A "
        "(opus-4-8) and Rater B (sonnet-5) split on it the same way repeatedly. Findings "
        "below are **clustered** so you rule on each *distinct* dispute once; your verdict "
        "propagates to every member (all shown, so you can audit — and override a member "
        "whose plan genuinely differs)."
    )
    L.append("")
    L.append(
        f"**{stats['hard_findings']} hard disputes → {stats['hard_clusters']} decisions** "
        f"(+ {stats['soft_findings']} soft → {stats['soft_clusters']}). "
        "For each cluster set **HUMAN VERDICT** to `TP`, `FP`, or `ambiguous`."
    )
    L.append("")
    L.append(
        "> TP/FP lens (it INVERTS by `source`). **surfaced:** `TP`=the finding names a real "
        "plan weakness (correct to surface); `FP`=spurious (noise). **dropped:** `TP`=no real "
        "defect, so hiding it was right (a good drop); `FP`=a real defect the gate buried "
        "(an escaped defect)."
    )
    L.append("")

    for pool, title in (("hard", "Hard disputes (TP↔FP)"), ("soft", "Soft disputes (one rater abstained)")):
        pool_recs = [r for r in records if r["pool"] == pool]
        if not pool_recs:
            continue
        L.append(f"## {title}")
        L.append("")
        last_arch: tuple[str, str] | None = None
        for r in pool_recs:
            arch = (r["source"], r.get("criterion") or "")
            if arch != last_arch:
                L.append(f"### `{arch[0]}` · criterion `{arch[1] or '—'}`")
                L.append("")
                last_arch = arch
            rep = r["representative"]
            L.append(f"#### {r['cluster_id']}  ·  covers {r['n_members']} finding(s)")
            L.append("")
            L.append(f"- **A (opus-4-8):** `{r['rater_a']}` — {_fmt_finding(r.get('rationale_a'), 300) or '(no rationale)'}")
            L.append(f"- **B (sonnet-5):** `{r['rater_b']}`")
            L.append(f"- **location:** `{rep.get('location') or '—'}`  ·  **severity:** `{rep.get('severity') or '—'}`")
            L.append(f"- **finding:** {_fmt_finding(rep.get('finding'))}")
            if rep.get("suggested_fix"):
                L.append(f"- **suggested fix:** {_fmt_finding(rep.get('suggested_fix'), 300)}")
            if r["n_members"] > 1:
                L.append(f"- **members ({r['n_members']}):**")
                for m in r["members"]:
                    L.append(
                        f"    - `{m['ticket_id']}` / `{m['finding_id']}` "
                        f"(A=`{m['rater_a']}` B=`{m['rater_b']}`): {_fmt_finding(m.get('finding'), 220)}"
                    )
            L.append("")
            L.append(f"  **HUMAN VERDICT ({r['cluster_id']}):** `____`   "
                     "(optional per-member override: `<finding_id>=TP|FP|ambiguous`)")
            L.append("")
    L.append("---")
    L.append(
        "When done, hand the verdicts back (chat referencing cluster ids, or fill "
        "`human_label` in `runs/disputes.jsonl`); `apply_adjudications.py` folds them into "
        "the corpus as the final gold label."
    )
    L.append("")
    return "\n".join(L)


def main() -> int:
    with open(CORPUS) as fh:
        rows = [json.loads(line) for line in fh]

    hard = [r for r in rows if _classify(r) == "hard"]
    soft = [r for r in rows if _classify(r) == "soft"]

    hard_clusters = _cluster(hard)
    soft_clusters = _cluster(soft)

    records = _cluster_records(hard_clusters, "hard", 1)
    records += _cluster_records(soft_clusters, "soft", 1)

    stats = {
        "hard_findings": len(hard),
        "hard_clusters": len(hard_clusters),
        "soft_findings": len(soft),
        "soft_clusters": len(soft_clusters),
    }

    _atomic_write_jsonl(records, DISPUTES)
    _atomic_write_text(_worksheet(records, stats), WORKSHEET)

    print(json.dumps(stats, indent=2))
    print(
        f"hard: {stats['hard_findings']} findings -> {stats['hard_clusters']} clusters "
        f"({stats['hard_findings'] - stats['hard_clusters']} redundant repeats saved)"
    )
    print(f"soft: {stats['soft_findings']} findings -> {stats['soft_clusters']} clusters")
    print(f"wrote -> {WORKSHEET}")
    print(f"wrote -> {DISPUTES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
