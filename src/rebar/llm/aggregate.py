"""Multi-reviewer finding aggregation: cluster → consensus → rank.

When several reviewers review the same change, they surface overlapping findings.
This module merges per-reviewer results into one ranked list: findings about the
same place/dimension are clustered, the cluster's **agreement** (how many reviewers
raised it) is recorded, citations are unioned, and the result is ranked by
severity × agreement. Deterministic and stdlib-only (no embeddings) — clustering
is by file-location bucket + dimension, falling back to a normalized detail prefix.
The aggregated findings carry two extra (schema-allowed) fields: ``agreement``
(int) and ``reviewers`` (the reviewer ids that raised it).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
_LINE_BUCKET = 10  # cluster findings within ~10 lines of each other


def _severity_rank(finding: dict) -> int:
    return _SEVERITY_RANK.get(str(finding.get("severity", "info")).lower(), 0)


def _cluster_key(finding: dict):
    """A deterministic key grouping 'the same' finding across reviewers: by first
    file citation (path + line bucket) + dimension, else dimension + detail prefix."""
    dim = str(finding.get("dimension", "")).strip().lower()
    for cit in finding.get("citations", []):
        if cit.get("kind") == "file" and cit.get("path"):
            ls = cit.get("line_start")
            bucket = ls // _LINE_BUCKET if isinstance(ls, int) else -1
            return ("loc", cit["path"], bucket, dim)
    detail = " ".join(str(finding.get("detail", "")).lower().split())[:80]
    return ("txt", dim, detail)


def _iter_reviewer_findings(per_reviewer: Iterable) -> Iterator[tuple[str | None, list[dict]]]:
    """Accept either review_result dicts or (reviewer_id, findings) pairs."""
    for item in per_reviewer:
        if isinstance(item, dict) and "findings" in item:
            rid = (item.get("reviewers") or [None])[0]
            yield rid, item.get("findings") or []
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            yield item[0], list(item[1])


def aggregate_findings(per_reviewer: Iterable) -> list[dict]:
    """Merge per-reviewer findings → one ranked, de-duplicated list.

    ``per_reviewer`` is a list of review_result dicts (one per reviewer) or of
    ``(reviewer_id, findings)`` pairs. Returns findings sorted by severity then
    agreement (descending), each with merged citations + ``agreement``/``reviewers``.
    """
    clusters: dict = {}
    order: list = []
    for reviewer_id, findings in _iter_reviewer_findings(per_reviewer):
        for finding in findings:
            key = _cluster_key(finding)
            if key not in clusters:
                clusters[key] = {"items": [], "reviewers": set()}
                order.append(key)
            clusters[key]["items"].append(finding)
            if reviewer_id:
                clusters[key]["reviewers"].add(reviewer_id)

    merged: list[dict] = []
    for key in order:
        cluster = clusters[key]
        # Representative = highest severity, then longest detail (most specific).
        rep = max(cluster["items"],
                  key=lambda f: (_severity_rank(f), len(str(f.get("detail", "")))))
        out = dict(rep)
        seen, cits = set(), []
        for finding in cluster["items"]:
            for cit in finding.get("citations", []):
                ckey = json.dumps(cit, sort_keys=True)
                if ckey not in seen:
                    seen.add(ckey)
                    cits.append(cit)
        out["citations"] = cits
        reviewers = sorted(cluster["reviewers"])
        out["reviewers"] = reviewers
        out["agreement"] = len(reviewers) if reviewers else len(cluster["items"])
        merged.append(out)

    merged.sort(key=lambda f: (_severity_rank(f), f.get("agreement", 1)), reverse=True)
    return merged
